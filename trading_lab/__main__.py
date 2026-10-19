from decimal import Decimal as D

from .core import Intent, Limits, Market, validate


def main() -> None:
    print("OFFLINE VALIDATION ONLY - fixed sample prices, no API, no fills")
    examples = [
        Intent(Market.KR, "005930", "KRW", "buy", D("1"), D("70000")),
        Intent(Market.US, "AAPL", "USD", "buy", D("1"), D("200")),
        Intent(Market.FUTURES, "BTCUSDT", "USDT", "sell", D("0.001"), D("60000")),
    ]
    caps = {
        Market.KR: Limits(D("100000"), D("200000"), D("1")),
        Market.US: Limits(D("300"), D("600"), D("1")),
        Market.FUTURES: Limits(D("100"), D("200"), D("1")),
    }
    for intent in examples:
        result = validate(intent, caps[intent.market])
        print(f"{intent.market.value} {intent.symbol}: valid; hypothetical position={result}; {intent.currency}")


if __name__ == "__main__":
    main()
