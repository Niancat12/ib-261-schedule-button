from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Action:
    kind: str
    target: date | None = None


class ButtonController:
    def __init__(self) -> None:
        self._last_date: dict[tuple[str, str, str | None], date] = {}
        self._awaiting_date: set[tuple[str, str, str | None]] = set()

    def handle(
        self,
        text: str,
        key: tuple[str, str, str | None],
        today: date,
    ) -> Action | None:
        value = (text or "").strip()
        if key in self._awaiting_date:
            try:
                target = self._parse_date(value)
            except ValueError:
                return Action("invalid_date")
            self._awaiting_date.remove(key)
            return Action("schedule", target)

        if value == "/ib261" or value.startswith("/ib261@"):
            return Action("keyboard")
        if value == "/schedule" or value.startswith("/schedule@"):
            return Action("menu")

        if value in {"📅 Расписание ИБ-261", "Сегодня"}:
            return Action("schedule", today)
        if value == "Завтра":
            return Action("schedule", today + timedelta(days=1))
        if value == "Неделя":
            return Action("week", self._last_date.get(key, today))
        if value == "Обновить":
            return Action("refresh", self._last_date.get(key, today))
        if value == "📆 Другая дата":
            self._awaiting_date.add(key)
            return Action("ask_date")
        if value in {"⬅️ Вчера", "➡️ Завтра"}:
            delta = -1 if value.startswith("⬅️") else 1
            target = self._last_date.get(key, today) + timedelta(days=delta)
            return Action("schedule", target)
        return None

    def mark_displayed(self, key: tuple[str, str, str | None], target: date) -> None:
        self._last_date[key] = target

    @staticmethod
    def _parse_date(value: str) -> date:
        try:
            if "." in value:
                day, month, year = (int(part) for part in value.split("."))
                return date(year, month, day)
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError("invalid date") from None
