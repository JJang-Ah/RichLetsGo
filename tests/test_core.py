import unittest
from dataclasses import replace
from decimal import Decimal as D

from trading_lab.core import Intent, Limits, Market, Rejected, validate


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.order = Intent(Market.FUTURES, "BTCUSDT", "USDT", "buy", D("1"), D("100"))
        self.limits = Limits(D("1000"), D("1500"), D("3"))

    def test_long_and_short(self):
        self.assertEqual(validate(self.order, self.limits), D("1"))
        self.assertEqual(validate(replace(self.order, side="sell"), self.limits), D("-1"))

    def test_notional_does_not_shrink_with_leverage(self):
        with self.assertRaises(Rejected):
            validate(replace(self.order, quantity=D("11"), leverage=D("3")), self.limits)

    def test_aggregate_position(self):
        with self.assertRaises(Rejected):
            validate(self.order, self.limits, position=D("15"))

    def test_reduce_only_closes_both_directions(self):
        self.assertEqual(
            validate(replace(self.order, reduce_only=True), self.limits, position=D("-1")), D("0")
        )
        self.assertEqual(
            validate(
                replace(self.order, side="sell", reduce_only=True), self.limits, position=D("1")
            ),
            D("0"),
        )

    def test_reduce_only_rejects_flat_increase_and_reversal(self):
        for position in (D("0"), D("1"), D("-0.5")):
            with self.subTest(position=position), self.assertRaises(Rejected):
                validate(replace(self.order, reduce_only=True), self.limits, position=position)

    def test_oversized_position_can_be_reduced(self):
        result = validate(
            replace(self.order, side="sell", reduce_only=True), self.limits, position=D("20")
        )
        self.assertEqual(result, D("19"))

    def test_invalid_numbers(self):
        for value in (D("NaN"), D("Infinity"), D("0"), D("-1"), 1.0):
            for field in ("quantity", "reference_price", "leverage"):
                with self.subTest(value=value, field=field), self.assertRaises(Rejected):
                    validate(replace(self.order, **{field: value}), self.limits)

    def test_bad_currency_flags_and_leverage(self):
        for changes in (
            {"currency": "USD"},
            {"reduce_only": "false"},
            {"leverage": D("4")},
            {"leverage": D("0.5")},
        ):
            with self.subTest(changes=changes), self.assertRaises(Rejected):
                validate(replace(self.order, **changes), self.limits)
        with self.assertRaises(Rejected):
            validate(self.order, self.limits, halted=True)

    def test_stock_whole_shares_and_no_short(self):
        for market, currency in ((Market.KR, "KRW"), (Market.US, "USD")):
            stock = replace(self.order, market=market, currency=currency)
            self.assertEqual(validate(stock, self.limits), D("1"))
            for changes in ({"quantity": D("0.5")}, {"side": "sell"}, {"leverage": D("2")}):
                with self.subTest(market=market, changes=changes), self.assertRaises(Rejected):
                    validate(replace(stock, **changes), self.limits)


if __name__ == "__main__":
    unittest.main()
