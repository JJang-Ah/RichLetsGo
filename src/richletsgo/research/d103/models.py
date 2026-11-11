"""Fixed D103 stage-0 boundaries (M2-10). Changing any value is a new plan."""

from datetime import UTC, datetime

HOUR = 3_600_000
MINUTE = 60_000
DAY = 24 * HOUR


def epoch(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


# Exclusive end of every historical request; [B, T0) stays unobserved (PR #60 P2-1/P2-2).
BOUNDARY = epoch("2026-09-01T00:00:00")
TYPES = ("stock", "ETF", "commodity", "forex")
SNAPSHOT_DIR = "docs/reports/D103-instruments-2026-11-10"
SNAPSHOT_FILES = {t: f"{t}.json" for t in TYPES}
HOST = "api.bybit.com"
HISTORY_INTERVALS = {"60": HOUR, "5": 5 * MINUTE}
PRICE_KLINE_PATHS = ("/v5/market/index-price-kline", "/v5/market/mark-price-kline")
BOOK_LIMITS = ("1", "50")
TOP_EVERY = 5 * MINUTE
DEPTH_EVERY = HOUR
LIVE_SPAN = 14 * DAY
NOTIONALS = (1_000, 5_000, 20_000)
# Documented orderbook result keys; anything else stops live sampling.
BOOK_KEYS = frozenset({"s", "a", "b", "ts", "u", "seq", "cts"})
# Reopen approximation window around a session start R (5m bars).
BEFORE = (-5 * MINUTE, 0)
AFTER = (10 * MINUTE, 15 * MINUTE)
# A book whose ts is more than this from its request start is stale: excluded and counted.
STALE_AFTER = MINUTE
