from datetime import date

import pytest

from ib261_schedule.core import move_date, parse_date_input


def test_parse_date_input_accepts_russian_and_iso_formats():
    assert parse_date_input("08.09.2026") == date(2026, 9, 8)
    assert parse_date_input("2026-09-08") == date(2026, 9, 8)


def test_parse_date_input_rejects_invalid_calendar_date():
    with pytest.raises(ValueError, match="ДД.ММ.ГГГГ"):
        parse_date_input("31.02.2026")


def test_move_date_is_relative_to_last_displayed_date():
    assert move_date(date(2026, 9, 8), -1) == date(2026, 9, 7)
    assert move_date(date(2026, 9, 8), 1) == date(2026, 9, 9)
