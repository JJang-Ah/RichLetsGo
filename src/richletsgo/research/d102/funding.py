"""D102 window funding scores, including historical settlement-period changes."""

from bisect import bisect_right
from decimal import Decimal

from richletsgo.research.d98.boundaries import DAY, HOUR
from richletsgo.research.d98.models import D, FundingPoint, calculated


class FundingSeries:
    def __init__(self, points: list[FundingPoint]) -> None:
        self.points = sorted(points, key=lambda point: point.time)
        self.times = [point.time for point in self.points]
        if len(set(self.times)) != len(self.times):
            raise ValueError("duplicate funding timestamp")

    def has_input(self, at: int) -> bool:
        """Stage 3 only: at least one settlement in (t-7d, t]."""
        return bisect_right(self.times, at) > bisect_right(self.times, at - 7 * DAY)

    @calculated
    def score(self, at: int, days: int) -> tuple[Decimal | None, str | None]:
        if days not in (1, 7):
            raise ValueError("D102 funding window must be 1 or 7 days")
        begin = bisect_right(self.times, at - days * DAY)
        end = bisect_right(self.times, at)
        if begin == 0 or begin == end or at - self.times[end - 1] > 8 * HOUR:
            return None, "funding_coverage"
        total = D(0)
        for index in range(begin, end):
            interval = self.times[index] - self.times[index - 1]
            if interval not in (HOUR, 2 * HOUR, 4 * HOUR, 8 * HOUR):
                return None, "funding_coverage"
            total += self.points[index].rate * (8 * HOUR) / interval
        return total / (end - begin), None

    def period_points(self, start: int, end: int) -> list[FundingPoint] | None:
        """Return (start,end] settlements, or None on an uncovered endpoint/gap."""
        if end < start:
            raise ValueError("period end precedes start")
        first = bisect_right(self.times, start)
        stop = bisect_right(self.times, end)
        if first == 0:
            return None
        previous = self.times[first - 1]
        if start - previous > 8 * HOUR:
            return None
        for index in range(first, stop):
            if self.times[index] - previous > 8 * HOUR:
                return None
            previous = self.times[index]
        if end - previous > 8 * HOUR:
            return None
        return self.points[first:stop]
