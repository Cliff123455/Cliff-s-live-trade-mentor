# src/shared/sim_clock.py
# Shared simulated clock for backtest mode.
# In live mode the Redis key won't exist, so all functions fall back to real time.

import time

SIM_CLOCK_KEY = "state:sim-clock"


async def get_sim_time_ms(redis_client) -> int:
    """Return simulated timestamp (ms) from Redis, or real wall clock if not set."""
    val = await redis_client.get(SIM_CLOCK_KEY)
    if val is not None:
        return int(float(val))
    return int(time.time() * 1000)


async def set_sim_time_ms(redis_client, ts_ms: int):
    """Set the simulated clock. Called by ReplayScanner on each bar."""
    await redis_client.set(SIM_CLOCK_KEY, str(ts_ms))


def get_sim_time_ms_sync(redis_client) -> int:
    """Synchronous variant for Flask / non-async code."""
    val = redis_client.get(SIM_CLOCK_KEY)
    if val is not None:
        return int(float(val))
    return int(time.time() * 1000)
