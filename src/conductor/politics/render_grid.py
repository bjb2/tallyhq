"""Render activity-grid rows as SVG (web) or ANSI (terminal).

GitHub-contribution-style: 7 rows (days of week), N columns (weeks),
month labels above, weekday labels left.
"""
from __future__ import annotations

from datetime import date, timedelta

from conductor.aggregations.activity_grid import GridRow

# Activity-grid palettes
PALETTE_DARK  = ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"]
# Light: warmer, less saturated — fits editorial register
PALETTE_LIGHT = ["#ece8de", "#c7e4c8", "#85c69a", "#3e9d6f", "#1a7f37"]

ANSI_BLOCKS = ["·", "░", "▒", "▓", "█"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def render_ansi(row: GridRow) -> str:
    cells_by_day = {c.day: c for c in row.cells}
    start = row.start - timedelta(days=(row.start.weekday() + 1) % 7)
    end = row.end + timedelta(days=(5 - row.end.weekday()) % 7)
    rows: list[list[str]] = [[] for _ in range(7)]
    cur = start
    while cur <= end:
        for dow in range(7):
            d = cur + timedelta(days=dow)
            cell = cells_by_day.get(d)
            if cell is None or d < row.start or d > row.end:
                rows[dow].append(" ")
            else:
                rows[dow].append(ANSI_BLOCKS[cell.band])
        cur += timedelta(days=7)
    return "\n".join("".join(r) for r in rows)


RECESS_BG = "#dfd9c8"   # muted cream, distinguishable from empty white but not loud
HOUSE_COLOR = "#cd6f4f"
SENATE_COLOR = "#5588a3"


def render_svg(
    row: GridRow,
    *,
    cell_size: int = 12,
    cell_gap: int = 3,
    palette: list[str] | None = None,
    label_dow: bool = True,
    label_months: bool = True,
    text_color: str = "#5e5a52",
    interactive: bool = False,
    session_mask: dict | None = None,
    session_mode: str = "none",     # "none" | "recess-bg" | "stripe"
) -> str:
    """SVG contribution graph. Light palette by default.

    When ``interactive`` is True, cells with band > 0 carry ``class="day-cell"``
    and ``cursor:pointer`` so a JS handler bound on the SVG host can drill
    into the day's contributing events via ``data-day``.

    Session marker modes:
    - "none"       (default): no session info rendered.
    - "recess-bg" : days where BOTH chambers are in recess get a muted bg
                    on otherwise-empty cells. Active days unchanged.
    - "stripe"    : two thin rows under the grid showing House (top) and
                    Senate (bottom) in-session days as colored bars.
                    Heatmap cells unchanged.
    """
    palette = palette or PALETTE_LIGHT
    cells_by_day = {c.day: c for c in row.cells}
    mask = session_mask or {}

    start = row.start - timedelta(days=(row.start.weekday() + 1) % 7)
    end = row.end + timedelta(days=(5 - row.end.weekday()) % 7)
    weeks = (end - start).days // 7 + 1

    pitch = cell_size + cell_gap
    margin_left = 28 if label_dow else 4
    margin_top = 18 if label_months else 4
    stripe_height = 0
    if session_mode == "stripe":
        # 2 mini-rows × 4px tall + 2px gap
        stripe_height = 12
    width = margin_left + weeks * pitch + 4
    height = margin_top + 7 * pitch + 4 + stripe_height

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="10">'
    )

    if label_dow:
        for i, label in enumerate(("Mon", "Wed", "Fri")):
            y = margin_top + (i * 2 + 1) * pitch + cell_size - 2
            parts.append(f'<text x="2" y="{y}" fill="{text_color}">{label}</text>')

    if label_months:
        seen: set[int] = set()
        cur = start
        for col in range(weeks):
            d = cur + timedelta(days=6)
            month = d.month
            if month not in seen and d.day <= 7:
                x = margin_left + col * pitch
                parts.append(f'<text x="{x}" y="12" fill="{text_color}">{MONTHS[month - 1]}</text>')
                seen.add(month)
            cur += timedelta(days=7)

    cur = start
    for col in range(weeks):
        for dow in range(7):
            d = cur + timedelta(days=dow)
            x = margin_left + col * pitch
            y = margin_top + dow * pitch
            if d < row.start or d > row.end:
                continue
            cell = cells_by_day.get(d)
            band = cell.band if cell else 0

            # Recess-bg mode: empty cells on full-recess days get muted bg
            color = palette[band]
            if session_mode == "recess-bg" and band == 0:
                house_in, senate_in = mask.get(d, (False, False))
                if not house_in and not senate_in and d.weekday() not in (5, 6):
                    # Only mark weekday recess days; weekends already read as quiet
                    color = RECESS_BG

            tip = (
                f"{d.isoformat()} · intensity {cell.intensity:.1f} · {cell.count} events"
                if cell else d.isoformat()
            )
            extra = ""
            if interactive and cell and cell.band > 0:
                extra = ' class="day-cell" style="cursor:pointer"'
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell_size}" height="{cell_size}" '
                f'rx="2" ry="2" fill="{color}" data-day="{d.isoformat()}"{extra}>'
                f'<title>{tip}</title></rect>'
            )
        cur += timedelta(days=7)

    if session_mode == "stripe":
        # Aggregate per-week: was any of that week in session for each chamber?
        stripe_y0 = margin_top + 7 * pitch + 4
        cur = start
        for col in range(weeks):
            x = margin_left + col * pitch
            week_h = False
            week_s = False
            for dow in range(7):
                d = cur + timedelta(days=dow)
                if d < row.start or d > row.end:
                    continue
                h, s = mask.get(d, (False, False))
                week_h = week_h or h
                week_s = week_s or s
            if week_h:
                parts.append(
                    f'<rect x="{x}" y="{stripe_y0}" width="{cell_size}" '
                    f'height="3" rx="1" ry="1" fill="{HOUSE_COLOR}"><title>House in session this week</title></rect>'
                )
            if week_s:
                parts.append(
                    f'<rect x="{x}" y="{stripe_y0 + 5}" width="{cell_size}" '
                    f'height="3" rx="1" ry="1" fill="{SENATE_COLOR}"><title>Senate in session this week</title></rect>'
                )
            cur += timedelta(days=7)

    parts.append("</svg>")
    return "".join(parts)
