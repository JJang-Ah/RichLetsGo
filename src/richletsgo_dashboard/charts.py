"""Server-rendered SVG line charts (no CDN). Every text and attribute value is escaped.

Single series per chart, so the title names it and no legend box is drawn (dataviz
skill). Two measures never share an axis: equity and drawdown are separate charts.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from markupsafe import Markup, escape

from .fmt import kst, money, pct

WIDTH, HEIGHT = 720, 220
LEFT, RIGHT, TOP, BOTTOM = 64, 16, 12, 28


@dataclass(frozen=True)
class Series:
    title: str
    points: Sequence[tuple[datetime, Decimal]]
    kind: str  # "money" | "pct"


def _label(value: Decimal, kind: str) -> str:
    return pct(value) if kind == "pct" else money(value)


def line_chart(series: Series) -> Markup:
    pts = list(series.points)
    title = escape(series.title)
    if len(pts) < 2:
        return Markup('<p class="empty">{} 데이터가 아직 없습니다.</p>').format(title)
    t0, t1 = pts[0][0].timestamp(), pts[-1][0].timestamp()
    values = [v for _, v in pts]
    lo, hi = min(values), max(values)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM

    def x(t: datetime) -> float:
        return LEFT + (t.timestamp() - t0) / (t1 - t0 or 1) * plot_w

    def y(v: Decimal) -> float:
        return TOP + float((hi - v) / (hi - lo)) * plot_h

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(v):.1f}" for i, (t, v) in enumerate(pts)
    )
    parts: list[Markup] = []
    for k in range(5):
        v = lo + (hi - lo) * Decimal(k) / 4
        yy = y(v)
        parts.append(
            Markup(
                '<line class="grid" x1="{l}" x2="{r}" y1="{y:.1f}" y2="{y:.1f}"/>'
                '<text class="tick" x="{tx}" y="{ty:.1f}" text-anchor="end">{label}</text>'
            ).format(
                l=LEFT, r=WIDTH - RIGHT, y=yy, tx=LEFT - 8, ty=yy + 4, label=_label(v, series.kind)
            )
        )
    for k in range(4):
        t = pts[round(k * (len(pts) - 1) / 3)][0]
        anchor = "start" if k == 0 else "end" if k == 3 else "middle"
        parts.append(
            Markup(
                '<text class="tick" x="{x:.1f}" y="{y}" text-anchor="{a}">{label}</text>'
            ).format(x=x(t), y=HEIGHT - 8, a=anchor, label=kst(t, "%m-%d %H:%M"))
        )
    # Hover data: numbers and KST labels only; rendered as text by app.js.
    data = ";".join(
        f"{x(t):.1f}|{y(v):.1f}|{kst(t, '%m-%d %H:%M')}|{_label(v, series.kind)}" for t, v in pts
    )
    return Markup(
        '<figure class="chart" data-points="{data}">'
        "<figcaption>{title}</figcaption>"
        '<svg viewBox="0 0 {w} {h}" role="img" aria-label="{title}" preserveAspectRatio="none">'
        '<line class="axis" x1="{l}" x2="{r}" y1="{b}" y2="{b}"/>'
        "{parts}"
        '<path class="series" d="{path}"/>'
        '<line class="crosshair" x1="0" x2="0" y1="{t}" y2="{b}" hidden/>'
        '<circle class="marker" r="4" cx="0" cy="0" hidden/>'
        "</svg>"
        '<div class="tooltip" hidden></div>'
        "</figure>"
    ).format(
        data=data,
        title=title,
        w=WIDTH,
        h=HEIGHT,
        l=LEFT,
        r=WIDTH - RIGHT,
        t=TOP,
        b=HEIGHT - BOTTOM,
        parts=Markup("").join(parts),
        path=path,
    )


SPARK_W, SPARK_H = 600, 96


def sparkline(points: Sequence[tuple[datetime, Decimal]], label: str) -> Markup:
    """Axis-free equity trace for the overview band; the full chart lives on /equity."""
    pts = list(points)
    if len(pts) < 2:
        return Markup("")
    t0, t1 = pts[0][0].timestamp(), pts[-1][0].timestamp()
    values = [v for _, v in pts]
    lo, hi = min(values), max(values)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    pad = 6

    def x(t: datetime) -> float:
        return (t.timestamp() - t0) / (t1 - t0 or 1) * (SPARK_W - pad)  # keep the end dot inside

    def y(v: Decimal) -> float:
        return pad + float((hi - v) / (hi - lo)) * (SPARK_H - 2 * pad)

    line = " ".join(
        f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(v):.1f}" for i, (t, v) in enumerate(pts)
    )
    area = f"{line} L{SPARK_W},{SPARK_H} L0,{SPARK_H} Z"
    end_x, end_y = x(pts[-1][0]), y(pts[-1][1])
    return Markup(
        '<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
        'aria-label="{label}">'
        '<path class="spark-area" d="{area}"/>'
        '<path class="spark-line" d="{line}"/>'
        '<circle class="spark-end" cx="{ex:.1f}" cy="{ey:.1f}" r="3.5"/>'
        "</svg>"
    ).format(w=SPARK_W, h=SPARK_H, label=label, area=area, line=line, ex=end_x, ey=end_y)
