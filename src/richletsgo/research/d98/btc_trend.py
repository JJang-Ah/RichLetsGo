"""BTC reference state, seeded FLAT then replayed through the full warm-up."""

from .boundaries import BTC_START, DAY, END, epoch
from .models import RawBar

SEED = epoch("2021-01-01")


class BTCTrend:
    def __init__(self, bars: list[RawBar]) -> None:
        source = {b.start: b for b in bars}
        if len(source) != len(bars) or any(t not in source for t in range(BTC_START, END, DAY)):
            raise ValueError("BTC_INCOMPLETE")
        self.states = {SEED: 0}
        state = 0
        for at in range(SEED + DAY, END + 1, DAY):
            current = source[at - DAY]
            previous = [source[t] for t in range(at - 56 * DAY, at - DAY, DAY)]
            if current.close > max(b.high for b in previous):
                state = 1
            elif current.close < min(b.low for b in previous):
                state = -1
            self.states[at] = state

    def at(self, t: int) -> int:
        return self.states[t // DAY * DAY]
