# src/agents/news_sentiment_agent.py
# "The Wire" — polls Alpaca news + Trump Truth Social + X (Twitter), scores via LLM
#
# Three parallel loops:
#   1. Alpaca news feed — every 30s, per watchlist ticker
#   2. Trump Truth Social monitor — every 2min, via Mastodon-compatible API
#   3. X (Twitter) cashtag search + account monitor — every 60s
#
# New items are sent to the LLM for classification and published to CH.CATALYSTS.

import asyncio
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any

import yaml
from dotenv import load_dotenv

from alpaca.data.historical import NewsClient
from alpaca.data.requests import NewsRequest

from src.shared.base_agent import BaseAgent
from src.shared.constants import AGENTS, CH, SK

load_dotenv()

# ── News prompts ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a financial news analyst. "
    "Classify the following news headline for a stock. "
    "Return JSON only."
)

_USER_TEMPLATE = (
    "Ticker: {ticker}\n"
    "Headline: {headline}\n"
    "Summary: {summary}\n\n"
    "Classify this news. Return JSON with fields: "
    "catalyst_type (one of: earnings_beat, earnings_miss, upgrade, downgrade, "
    "fda_approval, fda_rejection, macro_print, social_spike, sec_filing, none), "
    "sentiment (positive/negative/neutral), "
    "sentiment_score (0.0–1.0), "
    "confidence (0.0–1.0), "
    "time_sensitivity (high/medium/low)"
)

_POLL_INTERVAL_SECONDS = 30
_NEWS_LIMIT_PER_TICKER = 5

# ── Trump Truth Social constants ──────────────────────────────────────────────

_TRUTH_ACCOUNT_ID = "107780257626128497"   # @realDonaldTrump — verified via /api/v1/accounts/lookup
_TRUTH_API_URL    = "https://truthsocial.com/api/v1/accounts/{id}/statuses?limit=10"
_TRUTH_POLL_SECS  = 120   # poll every 2 minutes

_TRUTH_SYSTEM = (
    "You are a financial market analyst monitoring the U.S. President's social media "
    "for market-moving statements. Analyze the following post and determine if it has "
    "any potential impact on financial markets. Return JSON only. "
    "If the post has NO market relevance, return {\"market_relevant\": false}."
)

_TRUTH_USER = (
    "Post from @realDonaldTrump on Truth Social:\n\n\"{text}\"\n\n"
    "Posted at: {posted_at}\n\n"
    "Analyze for market impact. Return JSON with fields:\n"
    "market_relevant (bool),\n"
    "affected_tickers (list of stock tickers mentioned or clearly implied, e.g. [\"TSLA\", \"BA\"]),\n"
    "affected_sectors (list, e.g. [\"defense\", \"energy\", \"crypto\", \"tech\", \"banks\", \"retail\"]),\n"
    "catalyst_type (one of: tariff, trade_deal, sanction, fed_comment, crypto_mention, "
    "company_callout, geopolitical, policy_change, none),\n"
    "sentiment (positive/negative/neutral — from a MARKET perspective, not political),\n"
    "sentiment_score (0.0–1.0),\n"
    "confidence (0.0–1.0),\n"
    "time_sensitivity (immediate/high/medium/low),\n"
    "summary (one sentence describing the market implication)"
)


# ── X (Twitter) constants ────────────────────────────────────────────────────

_X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
_X_POLL_SECS  = 60

_X_SYSTEM = (
    "You are a financial market analyst monitoring social media for trade signals. "
    "Analyze the following tweet about a stock. Return JSON only."
)

_X_USER = (
    "Tweet from @{author}:\n\"{text}\"\n\n"
    "Ticker context: {ticker}\n\n"
    "Classify this tweet. Return JSON with fields:\n"
    "catalyst_type (one of: earnings_beat, earnings_miss, upgrade, downgrade, "
    "insider_trade, short_squeeze, social_spike, sector_rotation, none),\n"
    "sentiment (positive/negative/neutral),\n"
    "sentiment_score (0.0–1.0),\n"
    "confidence (0.0–1.0),\n"
    "time_sensitivity (immediate/high/medium/low)"
)


def _load_x_config() -> tuple[list[str], int]:
    """Load X accounts and poll interval from indicators.yaml."""
    try:
        with open("config/indicators.yaml", "r") as fh:
            cfg = yaml.safe_load(fh)
        accounts = cfg.get("x_accounts", []) or []
        interval = int(cfg.get("x_poll_interval_s", 60))
        return [str(a) for a in accounts], interval
    except Exception:
        return [], 60


def _load_watchlist() -> list[str]:
    with open("config/indicators.yaml", "r") as fh:
        cfg = yaml.safe_load(fh)
    # Try fallback_watchlist first (new key), fall back to legacy 'watchlist'
    tickers = cfg.get("fallback_watchlist", cfg.get("watchlist", []))
    return [str(t).upper() for t in tickers]


class NewsSentimentAgent(BaseAgent):
    """The Wire — Alpaca news poller + LLM catalyst classifier."""

    def __init__(self):
        super().__init__(AGENTS.WIRE)
        self.watchlist: list[str] = _load_watchlist()
        self.seen_articles: set[str] = set()

        self._news_client = NewsClient(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        )

        # X (Twitter) config
        self._x_bearer = os.getenv("X_BEARER_TOKEN", "")
        self._x_accounts, self._x_poll_secs = _load_x_config()
        self._x_since_id: str | None = None  # track last seen tweet ID

    # ── News fetch ────────────────────────────────────────────────────────────

    async def _fetch_news(self, ticker: str) -> list[dict]:
        """Fetch up to _NEWS_LIMIT_PER_TICKER recent articles for a ticker."""
        # Look back 24 hours so we catch anything published since last session
        start_dt = datetime.now(timezone.utc) - timedelta(hours=24)

        try:
            request = NewsRequest(
                symbols=ticker,
                limit=_NEWS_LIMIT_PER_TICKER,
                start=start_dt,
            )
            # NewsClient.get_news returns a NewsSet (directly iterable)
            news_set = self._news_client.get_news(request)

            articles = []
            for item in news_set:  # type: ignore[union-attr]
                articles.append({
                    "id": str(item.id),
                    "headline": item.headline or "",
                    "summary": item.summary or "",
                    "created_at": item.created_at,
                })
            return articles

        except Exception as exc:
            self.log.warning("news_fetch_error", ticker=ticker, error=str(exc))
            return []

    # ── LLM classification ────────────────────────────────────────────────────

    async def _classify_article(self, ticker: str, article: dict) -> dict | None:
        """Score a single article via the LLM.  Returns the classification dict
        or None if the LLM call fails or returns an empty response."""
        user_prompt = _USER_TEMPLATE.format(
            ticker=ticker,
            headline=article["headline"],
            summary=article["summary"],
        )

        result = await self.llm_json(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            temperature=0.1,
        )

        if not result:
            self.log.warning(
                "llm_empty_response",
                ticker=ticker,
                article_id=article["id"],
            )
            return None

        return result

    # ── Publish helpers ───────────────────────────────────────────────────────

    async def _publish_catalyst(
        self,
        ticker: str,
        article: dict,
        classification: dict,
    ) -> None:
        payload = {
            "ticker": ticker,
            "timestamp_ms": int(time.time() * 1000),
            "catalyst_type": classification.get("catalyst_type", "none"),
            "headline": article["headline"],
            "sentiment": classification.get("sentiment", "neutral"),
            "sentiment_score": float(classification.get("sentiment_score", 0.5)),
            "confidence": float(classification.get("confidence", 0.5)),
            "source": "alpaca_news",
            "time_sensitivity": classification.get("time_sensitivity", "low"),
            "article_id": article["id"],
        }

        await self.publish(CH.CATALYSTS, payload)
        await self.state_hset(
            SK.CATALYST.format(ticker),
            "latest",
            payload,
        )

        self.log.info(
            "catalyst_published",
            ticker=ticker,
            catalyst_type=payload["catalyst_type"],
            sentiment=payload["sentiment"],
            score=payload["sentiment_score"],
            article_id=article["id"],
        )

    # ── Alpaca poll loop ──────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        """Single pass over every ticker — fetch news, classify new articles."""
        for ticker in self.watchlist:
            articles = await self._fetch_news(ticker)

            for article in articles:
                article_id = article["id"]

                if article_id in self.seen_articles:
                    continue

                self.seen_articles.add(article_id)

                if not article["headline"]:
                    continue

                classification = await self._classify_article(ticker, article)
                if classification is None:
                    continue

                await self._publish_catalyst(ticker, article, classification)

    async def _news_loop(self) -> None:
        self.log.info("alpaca_news_loop_started", interval=_POLL_INTERVAL_SECONDS)
        while self.running:
            try:
                await self._poll_once()
            except Exception as exc:
                self.log.error("news_poll_error", error=str(exc))
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    # ── Trump Truth Social monitor ────────────────────────────────────────────

    def _fetch_truth_posts(self) -> "list[dict[str, Any]]":
        """Fetch recent Truth Social posts — synchronous, run via to_thread."""
        url = _TRUTH_API_URL.format(id=_TRUTH_ACCOUNT_ID)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())

    @staticmethod
    def _strip_html(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text).strip()

    async def _process_truth_post(self, post: dict) -> None:
        """Classify a single Truth Social post and publish if market-relevant."""
        raw_text: str = self._strip_html(str(post.get("content", "")))
        if not raw_text:
            return

        text_for_llm: str = raw_text[:1000]  # type: ignore[index]
        text_for_headline: str = raw_text[:120]  # type: ignore[index]

        result = await self.llm_json(
            system=_TRUTH_SYSTEM,
            user=_TRUTH_USER.format(
                text=text_for_llm,
                posted_at=post.get("created_at", ""),
            ),
            temperature=0.1,
        )

        if not result or not result.get("market_relevant"):
            self.log.debug("truth_not_market_relevant", post_id=post["id"])
            return

        # Publish one catalyst event per affected ticker (or "MACRO" if none named)
        tickers = result.get("affected_tickers") or ["MACRO"]
        for ticker in tickers:
            payload = {
                "ticker": ticker,
                "timestamp_ms": int(time.time() * 1000),
                "catalyst_type": "trump_" + str(result.get("catalyst_type", "social")),
                "headline": "[TRUMP] " + text_for_headline,
                "sentiment": result.get("sentiment", "neutral"),
                "sentiment_score": float(result.get("sentiment_score", 0.5)),
                "confidence": float(result.get("confidence", 0.5)),
                "source": "truth_social",
                "time_sensitivity": result.get("time_sensitivity", "high"),
                "affected_sectors": result.get("affected_sectors", []),
                "summary": result.get("summary", ""),
                "article_id": f"truth_{post['id']}",
                "post_url": f"https://truthsocial.com/@realDonaldTrump/{post['id']}",
            }
            await self.publish(CH.CATALYSTS, payload)
            await self.state_hset(SK.CATALYST.format(ticker), "trump_latest", payload)

        self.log.info(
            "trump_catalyst_published",
            tickers=tickers,
            catalyst=result.get("catalyst_type"),
            sentiment=result.get("sentiment"),
            time_sensitivity=result.get("time_sensitivity"),
            summary=result.get("summary", "")[:80],
        )

    async def _truth_loop(self) -> None:
        """Poll Trump's Truth Social every 2 minutes."""
        self.log.info("truth_social_monitor_started", account="realDonaldTrump",
                      interval=_TRUTH_POLL_SECS)
        while self.running:
            try:
                posts: list[dict[str, Any]] = await asyncio.to_thread(lambda: self._fetch_truth_posts())  # type: ignore[arg-type]
                new_count = 0
                for post in posts:
                    post_id = f"truth_{post['id']}"
                    if post_id in self.seen_articles:
                        continue
                    self.seen_articles.add(post_id)
                    new_count += 1
                    await self._process_truth_post(post)
                if new_count:
                    self.log.info("truth_new_posts", count=new_count)
            except Exception as exc:
                self.log.error("truth_poll_error", error=str(exc))
            await asyncio.sleep(_TRUTH_POLL_SECS)

    # ── X (Twitter) search ────────────────────────────────────────────────────

    def _search_x(self, query: str) -> list[dict]:
        """Search recent tweets — synchronous, run via to_thread."""
        if not self._x_bearer:
            return []

        params = f"query={urllib.parse.quote(query)}&max_results=10&tweet.fields=created_at,author_id,text"
        if self._x_since_id:
            params += f"&since_id={self._x_since_id}"

        url = f"{_X_SEARCH_URL}?{params}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._x_bearer}",
            "User-Agent": "ScalpBot/1.0",
        })
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("data", [])

    async def _x_loop(self) -> None:
        """Poll X for cashtag mentions of watchlist tickers + specific accounts."""
        if not self._x_bearer:
            self.log.warning("x_skipped_no_bearer_token")
            return

        self.log.info("x_monitor_started",
                      accounts=self._x_accounts,
                      interval=self._x_poll_secs,
                      watchlist_count=len(self.watchlist))

        while self.running:
            try:
                # Build query: cashtags for watchlist + account mentions
                cashtags = " OR ".join(f"${t}" for t in self.watchlist[:10])  # API limits query length
                account_part = ""
                if self._x_accounts:
                    account_part = " OR " + " OR ".join(f"from:{a}" for a in self._x_accounts[:5])
                query = f"({cashtags}{account_part}) -is:retweet lang:en"

                tweets = await asyncio.to_thread(lambda: self._search_x(query))  # type: ignore[arg-type]

                new_count = 0
                for tweet in tweets:
                    tweet_id = f"x_{tweet['id']}"
                    if tweet_id in self.seen_articles:
                        continue
                    self.seen_articles.add(tweet_id)
                    new_count += 1

                    # Update since_id for next poll
                    if self._x_since_id is None or tweet["id"] > self._x_since_id:
                        self._x_since_id = tweet["id"]

                    text = tweet.get("text", "")
                    author = tweet.get("author_id", "unknown")

                    # Detect which ticker(s) this tweet mentions
                    mentioned_tickers = [t for t in self.watchlist if f"${t}" in text.upper() or t in text.upper()]
                    if not mentioned_tickers:
                        mentioned_tickers = ["MACRO"]

                    for ticker in mentioned_tickers:
                        result = await self.llm_json(
                            system=_X_SYSTEM,
                            user=_X_USER.format(author=author, text=text[:800], ticker=ticker),
                            temperature=0.1,
                        )
                        if not result:
                            continue

                        payload = {
                            "ticker": ticker,
                            "timestamp_ms": int(time.time() * 1000),
                            "catalyst_type": result.get("catalyst_type", "social_spike"),
                            "headline": f"[X] {text[:120]}",
                            "sentiment": result.get("sentiment", "neutral"),
                            "sentiment_score": float(result.get("sentiment_score", 0.5)),
                            "confidence": float(result.get("confidence", 0.5)),
                            "source": "x_twitter",
                            "time_sensitivity": result.get("time_sensitivity", "medium"),
                            "article_id": tweet_id,
                        }
                        await self.publish(CH.CATALYSTS, payload)
                        self.log.info("x_catalyst_published", ticker=ticker,
                                      sentiment=payload["sentiment"], tweet_id=tweet["id"])

                if new_count:
                    self.log.info("x_new_tweets", count=new_count)
            except Exception as exc:
                self.log.error("x_poll_error", error=str(exc))
            await asyncio.sleep(self._x_poll_secs)

    # ── BaseAgent.run() ───────────────────────────────────────────────────────

    async def run(self) -> None:
        self.log.info(
            "wire_starting",
            watchlist=self.watchlist,
            news_interval=_POLL_INTERVAL_SECONDS,
            truth_interval=_TRUTH_POLL_SECS,
            x_interval=self._x_poll_secs,
            x_accounts=self._x_accounts,
        )
        # Run all three polling loops concurrently
        await asyncio.gather(
            self._news_loop(),
            self._truth_loop(),
            self._x_loop(),
            return_exceptions=True,
        )


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = NewsSentimentAgent()
    asyncio.run(agent.start())
