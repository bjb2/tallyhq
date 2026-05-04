"""Synthetic House/Senate session calendar — prototype only.

Returns a dict[date, (house_in_session, senate_in_session)] over a window.
Real data should come from congress.gov/days-in-session/119th-congress;
swap this for a scraped+cached dataset before shipping.
"""
from __future__ import annotations

import random
from datetime import date, timedelta


# Approximate 2025 recess windows (House — Senate is similar but slightly
# fewer recess days). Source: congressional calendars, simplified.
RECESS_BLOCKS_2025 = [
    (date(2025, 1, 1),  date(2025, 1, 2)),    # post-NY
    (date(2025, 2, 17), date(2025, 2, 21)),   # Presidents' Day week
    (date(2025, 4, 14), date(2025, 4, 25)),   # Easter / spring
    (date(2025, 5, 26), date(2025, 5, 30)),   # Memorial Day week
    (date(2025, 6, 30), date(2025, 7, 4)),    # July 4 week
    (date(2025, 8, 4),  date(2025, 9, 5)),    # August recess
    (date(2025, 10, 13), date(2025, 10, 17)), # Columbus week
    (date(2025, 11, 24), date(2025, 11, 28)), # Thanksgiving
    (date(2025, 12, 22), date(2025, 12, 31)), # Holiday recess
]
RECESS_BLOCKS_2026 = [
    (date(2026, 1, 1),  date(2026, 1, 5)),
    (date(2026, 2, 16), date(2026, 2, 20)),
    (date(2026, 4, 6),  date(2026, 4, 17)),
    (date(2026, 5, 25), date(2026, 5, 29)),
    (date(2026, 6, 29), date(2026, 7, 3)),
    (date(2026, 8, 3),  date(2026, 9, 4)),
]
RECESS_BLOCKS = RECESS_BLOCKS_2025 + RECESS_BLOCKS_2026


def _in_recess(d: date) -> bool:
    for start, end in RECESS_BLOCKS:
        if start <= d <= end:
            return True
    return False


def _in_session(d: date, *, chamber: str) -> bool:
    """Heuristic: weekdays Tue–Thu always; Mon/Fri ~50% in non-recess weeks.
    Senate slightly more active than House. Weekends never.
    """
    if d.weekday() in (5, 6):  # Sat, Sun
        return False
    if _in_recess(d):
        return False
    rng = random.Random(int(d.toordinal()))
    if d.weekday() == 1 or d.weekday() == 2 or d.weekday() == 3:  # Tue, Wed, Thu
        return True
    if d.weekday() == 0:  # Mon
        return rng.random() < (0.55 if chamber == "senate" else 0.40)
    if d.weekday() == 4:  # Fri
        return rng.random() < (0.40 if chamber == "senate" else 0.30)
    return False


def session_mask(start: date, end: date) -> dict[date, tuple[bool, bool]]:
    """Build (house_in_session, senate_in_session) for each day in range."""
    out: dict[date, tuple[bool, bool]] = {}
    cur = start
    while cur <= end:
        out[cur] = (_in_session(cur, chamber="house"),
                    _in_session(cur, chamber="senate"))
        cur += timedelta(days=1)
    return out
