from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Action:
    kind: str
    target: date | None = None
    mode: str = "day"


class ButtonController:
    def __init__(self) -> None:
        self._last_date: dict[tuple[str, str, str | None], date] = {}
        self._last_mode: dict[tuple[str, str, str | None], str] = {}
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
            return Action("schedule", target, "day")

        if value == "/ib261" or value.startswith("/ib261@"):
            return Action("keyboard")
        if value == "/schedule" or value.startswith("/schedule@"):
            # /schedule is the actual schedule request.  The response itself
            # installs the inline keyboard, so it must follow the same path as
            # the Today button instead of returning a menu-only message.
            return Action("week", today, "week")

        if value == "📅 Расписание ИБ-261":
            return Action("week", today, "week")
        if value == "Сегодня":
            return Action("schedule", today, "day")
        if value in {"Завтра", "Завтра ➡️"}:
            return Action("schedule", today + timedelta(days=1), "day")
        if value == "Неделя":
            return Action("week", self._last_date.get(key, today), "week")
        if value in {"Обновить", "🔄 Обновить"}:
            return Action("refresh", self._last_date.get(key, today), self._last_mode.get(key, "day"))
        if value in {"📆 Другая дата", "📅 Другая дата"}:
            self._awaiting_date.add(key)
            return Action("ask_date")
        if value in {"⬅️ Вчера", "➡️ Завтра"}:
            delta = -1 if value.startswith("⬅️") else 1
            target = self._last_date.get(key, today) + timedelta(days=delta)
            return Action("schedule", target, "day")
        return None

    def mark_displayed(self, key: tuple[str, str, str | None], target: date, mode: str = "day") -> None:
        self._last_date[key] = target
        self._last_mode[key] = mode

    @staticmethod
    def _parse_date(value: str) -> date:
        try:
            if "." in value:
                day, month, year = (int(part) for part in value.split("."))
                return date(year, month, day)
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError("invalid date") from None
