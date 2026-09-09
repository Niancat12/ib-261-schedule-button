from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .schedule import DaySchedule, Lesson
from .source import SOURCE_URL

_MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def _lesson_lines(lesson: Lesson) -> list[str]:
    heading = f"{lesson.time}"
    if lesson.subgroup:
        heading += f" · {lesson.subgroup}"
    if lesson.cancelled:
        heading += " · ОТМЕНЕНО"
    lines = [heading, f"  {lesson.subject}"]
    if lesson.lesson_type:
        lines.append(f"  Тип: {lesson.lesson_type}")
    if lesson.room:
        lines.append(f"  Аудитория: {lesson.room}")
    if lesson.teacher:
        lines.append(f"  Преподаватель: {lesson.teacher}")
    lines.extend(f"  {item}" for item in lesson.extra)
    return lines


def format_schedule(schedule: DaySchedule, checked_at: datetime, *, stale: bool) -> str:
    day = schedule.schedule_date
    lines: list[str] = []
    if stale:
        lines.extend(("⚠️ РАНЕЕ ПОЛУЧЕННЫЕ ДАННЫЕ", "Источник сейчас недоступен.", ""))
    lines.extend(
        (
            f"📅 ИБ-261 — {day.day} {_MONTHS[day.month]} {day.year}, {_WEEKDAYS[day.weekday()]}",
            f"Неделя: {schedule.parity}",
            "",
        )
    )
    if schedule.lessons:
        for index, lesson in enumerate(schedule.lessons):
            if index:
                lines.append("")
            lines.extend(_lesson_lines(lesson))
    else:
        lines.append("Занятий нет.")
    lines.extend(
        (
            "",
            f"Источник: {SOURCE_URL}",
            f"Проверено: {checked_at.astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')} MSK",
        )
    )
    return "\n".join(lines)
