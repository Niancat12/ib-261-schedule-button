from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .schedule import DaySchedule, Lesson, week_monday
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


def format_day_heading(day: date, *, today: date | None = None) -> str:
    reference = today or datetime.now(ZoneInfo("Europe/Moscow")).date()
    relation = {reference: "Сегодня", reference + timedelta(days=1): "Завтра", reference - timedelta(days=1): "Вчера"}.get(day)
    weekday = _WEEKDAYS[day.weekday()]
    if relation:
        return f"{relation}, {weekday}, {day.day} {_MONTHS[day.month]} {day.year}"
    return f"{weekday.capitalize()}, {day.day} {_MONTHS[day.month]} {day.year}"


def format_schedule(schedule: DaySchedule, checked_at: datetime, *, stale: bool) -> str:
    day = schedule.schedule_date
    lines: list[str] = []
    if stale:
        lines.extend(("⚠️ РАНЕЕ ПОЛУЧЕННЫЕ ДАННЫЕ", "Источник сейчас недоступен.", ""))
    lines.extend(
        (
            f"📅 ИБ-261 — {format_day_heading(day)}",
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


def format_week_schedule(
    schedules: dict, checked_at: datetime, *, parity: str, stale: bool = False,
) -> str:
    """Render one complete weekly snapshot in calendar order."""
    if not schedules:
        raise ValueError("Недельное расписание пусто")
    dates = sorted(schedules)
    monday = week_monday(dates[0])
    sunday = monday + timedelta(days=6)
    lines: list[str] = []
    if stale:
        lines.extend(("⚠️ РАНЕЕ ПОЛУЧЕННЫЕ ДАННЫЕ", "Источник сейчас недоступен.", ""))
    if monday.month == sunday.month and monday.year == sunday.year:
        span = f"с {monday.day} по {sunday.day} {_MONTHS[sunday.month]} {sunday.year}"
    else:
        span = (f"с {monday.day} {_MONTHS[monday.month]} {monday.year} по "
                f"{sunday.day} {_MONTHS[sunday.month]} {sunday.year}")
    lines.extend((
        f"📅 Расписание ИБ-261 на неделю {span}",
        f"Неделя: {parity}",
        "",
    ))
    for index, day in enumerate(dates):
        if index:
            lines.append("\n" + "─" * 20)
        schedule = schedules[day]
        lines.append(f"{day.day} {_MONTHS[day.month]} {day.year}, {_WEEKDAYS[day.weekday()].capitalize()}")
        if schedule.lessons:
            for lesson in schedule.lessons:
                lines.extend(("", *_lesson_lines(lesson)))
        else:
            lines.append("Нет занятий")
    lines.extend(("", f"Проверено: {checked_at.astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M:%S')} MSK"))
    return "\n".join(lines)
