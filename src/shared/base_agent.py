# src/shared/base_agent.py
# Abstract base class all 8 agents extend.
# Provides: Redis connection, pub/sub helpers, state store helpers,
# OpenRouter LLM call, heartbeat, and graceful shutdown.

import asyncio
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
import structlog
from openai import AsyncOpenAI

from src.shared.constants import SK, AGENTS, DEFAULT_MODELS
from src.shared.sim_clock import get_sim_time_ms

logger = structlog.get_logger()


class BaseAgent(ABC):
    def __init__(self, name: str):
        self.name = name
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        self.redis: aioredis.Redis | None = None
        self.pubsub: aioredis.client.PubSub | None = None
        self.running = False
        self.log = structlog.get_logger(agent=name)

        # OpenRouter client — shared across all agents
        self._llm = AsyncOpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
        self.pubsub = self.redis.pubsub()
        self.running = True
        await self._set_status("running")
        self.log.info("started")
        await asyncio.gather(self._heartbeat_loop(), self.run())

    async def stop(self):
        self.running = False
        await self._set_status("stopped")
        if self.pubsub:
            await self.pubsub.close()
        if self.redis:
            await self.redis.aclose()
        self.log.info("stopped")

    @abstractmethod
    async def run(self):
        """Main agent loop. Must be implemented by each agent."""

    # ── Pub/Sub ───────────────────────────────────────────────────────────────

    async def now_ms(self) -> int:
        """Current time in ms — simulated during replay, real otherwise."""
        return await get_sim_time_ms(self.redis)

    async def now_et(self) -> datetime:
        """Current time as Eastern-timezone datetime — simulated during replay."""
        ts_ms = await self.now_ms()
        return datetime.fromtimestamp(ts_ms / 1000, tz=ZoneInfo("America/New_York"))

    async def publish(self, channel: str, payload: dict):
        payload.setdefault("agent", self.name)
        payload.setdefault("timestamp_ms", await self.now_ms())
        await self.redis.publish(channel, json.dumps(payload))

    async def subscribe(self, *channels: str):
        await self.pubsub.subscribe(*channels)

    async def listen(self):
        """Async generator — yields decoded message dicts from subscribed channels."""
        async for message in self.pubsub.listen():
            if message["type"] == "message":
                try:
                    yield json.loads(message["data"])
                except json.JSONDecodeError:
                    self.log.warning("bad_json", raw=message["data"])

    # ── State store ───────────────────────────────────────────────────────────

    async def state_hset(self, key: str, field: str, value: Any):
        v = json.dumps(value) if not isinstance(value, str) else value
        await self.redis.hset(key, field, v)

    async def state_hget(self, key: str, field: str) -> Any:
        val = await self.redis.hget(key, field)
        if val is None:
            return None
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val

    async def state_hgetall(self, key: str) -> dict:
        raw = await self.redis.hgetall(key)
        result = {}
        for k, v in raw.items():
            try:
                result[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                result[k] = v
        return result

    async def state_set(self, key: str, value: Any):
        await self.redis.set(key, json.dumps(value))

    async def state_get(self, key: str) -> Any:
        val = await self.redis.get(key)
        return json.loads(val) if val else None

    async def state_incrbyfloat(self, key: str, amount: float):
        await self.redis.incrbyfloat(key, amount)

    async def stream_append(self, stream_key: str, fields: dict):
        str_fields = {k: json.dumps(v) if not isinstance(v, str) else v
                      for k, v in fields.items()}
        await self.redis.xadd(stream_key, str_fields)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def is_halted(self) -> bool:
        val = await self.state_hget(SK.RISK_PARAMS, "halted")
        return bool(val)

    async def get_model(self) -> str:
        """Returns the currently configured OpenRouter model for this agent."""
        model = await self.state_hget(SK.MODELS, self.name)
        return model or DEFAULT_MODELS.get(self.name, "google/gemini-2.0-flash-001")

    def make_id(self) -> str:
        return str(uuid.uuid4())

    # ── LLM call ─────────────────────────────────────────────────────────────

    async def llm(self, system: str, user: str, temperature: float = 0.2) -> str:
        model = await self.get_model()
        try:
            response = await self._llm.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            self.log.error("llm_error", model=model, error=str(e))
            return ""

    async def llm_json(self, system: str, user: str, temperature: float = 0.1) -> dict:
        """LLM call that parses the response as JSON. Returns {} on failure."""
        raw = await self.llm(system, user + "\n\nRespond with valid JSON only.", temperature)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Sanitize to ASCII for Windows console (cp1252 can't handle some Unicode)
            safe_raw = raw[:200].encode("ascii", errors="replace").decode("ascii")
            try:
                self.log.warning("llm_json_parse_failed", raw=safe_raw)
            except Exception:
                pass  # Never crash on logging
            return {}

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        while self.running:
            await self._set_status("running")
            await asyncio.sleep(5)

    async def _set_status(self, status: str):
        if self.redis:
            await self.redis.hset(
                SK.AGENT_STATUS.format(self.name),
                mapping={
                    "status": status,
                    "ts": await self.now_ms(),
                    "name": self.name,
                }
            )
