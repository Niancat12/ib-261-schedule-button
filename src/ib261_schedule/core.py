from __future__ import annotations

from datetime import date, timedelta

_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d")


def parse_date_input(value: str) -> date:
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return date.fromisoformat(text) if fmt == "%Y-%m-%d" else _parse_ru(text)
        except ValueError:
            continue
    raise ValueError("Введите дату в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД")


def _parse_ru(value: str) -> date:
    day, month, year = (int(part) for part in value.split("."))
    return date(year, month, day)


def move_date(current: date, days: int) -> date:
    return current + timedelta(days=days)
