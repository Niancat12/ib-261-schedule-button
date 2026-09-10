from __future__ import annotations

from datetime import date
from urllib.parse import quote, urlencode

SOURCE_URL = "https://cchgeu.ru/studentu/onlayn-raspisanie/"


def build_source_url(schedule_date: date, group: str) -> str:
    query = urlencode(
        {
            "date": schedule_date.isoformat(),
            "gruppa": group,
            "prepodavatel": "",
        }
    )
    return f"{SOURCE_URL}?{query}"


def build_canonical_url(schedule_date: date, group: str, parity: str) -> str:
    """Build the official path URL that identifies one schedule state."""
    if not group or not parity:
        raise ValueError("group and parity are required")
    return (
        f"{SOURCE_URL}{quote(group, safe='')}/"
        f"{schedule_date.isoformat()}/{quote(parity, safe='')}"
    )
