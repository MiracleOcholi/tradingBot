"""Key-rotation schedule math: +3.5 months must reproduce the agreed chain."""
from datetime import date

from app.services.reminders import RESEND_GAPS, add_three_and_half_months


def test_seeded_chain_from_2026_08_05():
    d = date(2026, 8, 5)
    chain = []
    for _ in range(5):
        d = add_three_and_half_months(d)
        chain.append(d)
    assert chain == [
        date(2026, 11, 20),
        date(2027, 3, 5),
        date(2027, 6, 20),
        date(2027, 10, 5),
        date(2028, 1, 20),
    ]


def test_year_rollover():
    assert add_three_and_half_months(date(2027, 10, 5)) == date(2028, 1, 20)
    assert add_three_and_half_months(date(2026, 11, 20)) == date(2027, 3, 5)


def test_resend_cadence_is_1_2_4_days_max_three():
    assert [g.days for g in RESEND_GAPS] == [1, 2, 4]
