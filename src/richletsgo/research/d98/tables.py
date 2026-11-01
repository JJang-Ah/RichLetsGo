"""D98 tables: freeze only after metadata review and Claude review."""

from richletsgo.marketdata.universe import EXCLUDED_BASES

TABLES_FROZEN = False
NUMERIC_EXCEPTIONS: frozenset[str] = frozenset()
ALIASES = {"MATIC": "POL", "RNDR": "RENDER", "FTM": "S"}
EXCLUDED = EXCLUDED_BASES | {"USDD"}
OBSERVED = frozenset({"BTC", "ETH", "SOL", "ZEC", "XRP", "NEAR", "NFT", "LADYS"})


def snapshot() -> dict[str, object]:
    return {
        "numeric_exceptions": sorted(NUMERIC_EXCEPTIONS),
        "aliases": dict(ALIASES),
        "excluded": sorted(EXCLUDED),
        "observed": sorted(OBSERVED),
    }
