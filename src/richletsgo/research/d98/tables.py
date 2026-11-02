"""D98 tables: freeze only after metadata review and Claude review."""

from richletsgo.marketdata.universe import EXCLUDED_BASES

TABLES_FROZEN = False
NUMERIC_EXCEPTIONS = frozenset({"0G", "1CAT", "1INCH", "2Z", "4", "4STOCK"})
ALIASES = {
    "MATIC": "POL",
    "RNDR": "RENDER",
    "FTM": "S",
    "EOS": "A",
    "MKR": "SKY",
    "KLAY": "KAIA",
    "TOMO": "VIC",
    "BNX": "FORM",
    "RON": "RONIN",
    "RAY": "RAYDIUM",
    "MC": "BEAM",
    "BIT": "MNT",
    "GAL": "G",
    "COCOS": "COMBO",
    "LUNA": "LUNC",
    "UST": "USTC",
}
# Evidence for additions reviewed without consulting price history. These links are
# part of the frozen table snapshot; see D98-alias-evidence.md for metadata context.
ALIAS_SOURCES = {
    "EOS": "https://www.vaulta.com/resources/vaulta-token-swap-a-begins-may-14",
    "MKR": "https://sky.money/blog/understanding-the-sky-token",
    "KLAY": "https://docs.kaia.io/misc/kaia-transition/faq-chain-transition/",
    "TOMO": "https://www.bybit.com/en/learn/web3/what-is-viction-crypto-vic",
    "BNX": "https://www.bybit.com/en/learn/web3/binaryx-crypto-to-four-meme",
    "RON": "https://blog.roninchain.com/p/breaking-news-ron-tradeable-on-binance",
    "RAY": "https://www.bybit.com/en/learn/daily-bits/raydium-launches-new-features-with-creator-rewards",
    "MC": "https://medium.com/@onbeam/beam-token-migration-update-december-2023-acfbe22b00f7",
    "BIT": "https://www.bybit.com/en/learn/altcoins/what-is-mnt-token",
    "GAL": "https://www.galxe.com/blog/gal-g",
    "COCOS": (
        "https://medium.com/combonetwork/combo-rebranding-introducing-updated-erc-20-"
        "and-bep-20-smart-contracts-2dc447ea6e5b"
    ),
    "LUNA": "https://docs.terra.money/migration/exchange-migration/",
    "UST": "https://docs.terra.money/migration/exchange-migration/",
}
EXCLUDED = EXCLUDED_BASES | {"USDD", "RLUSD", "ETHBTC"}
OBSERVED = frozenset({"BTC", "ETH", "SOL", "ZEC", "XRP", "NEAR", "NFT", "LADYS"})


def snapshot() -> dict[str, object]:
    return {
        "numeric_exceptions": sorted(NUMERIC_EXCEPTIONS),
        "aliases": dict(ALIASES),
        "alias_sources": dict(ALIAS_SOURCES),
        "excluded": sorted(EXCLUDED),
        "observed": sorted(OBSERVED),
    }
