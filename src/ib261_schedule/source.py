from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

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
