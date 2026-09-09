from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from lxml import html


class ScheduleParseError(ValueError):
    """The source page cannot be proven to match the requested schedule."""


@dataclass(frozen=True)
class Lesson:
    time: str
    subject: str
    room: str | None
    teacher: str | None
    subgroup: str | None
    lesson_type: str | None
    extra: tuple[str, ...] = ()
    cancelled: bool = False


@dataclass(frozen=True)
class DaySchedule:
    group: str
    schedule_date: date
    parity: str
    lessons: tuple[Lesson, ...]
    empty_confirmed: bool = False


_WEEKDAYS = {
    0: "Пн",
    1: "Вт",
    2: "Ср",
    3: "Чт",
    4: "Пт",
    5: "Сб",
    6: "Вс",
}
_TIME_RE = re.compile(r"^(\d{2}:\d{2})\s*[-–—]\s*(\d{2}:\d{2})$")
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split()).strip()


def _lines(element) -> list[str]:
    values: list[str] = []
    for raw in element.itertext():
        value = _clean(raw)
        if value and (not values or values[-1] != value):
            values.append(value)
    return values


def _selected_group(tree) -> str | None:
    selected = tree.xpath("//select[@id='gruppa']//option[@selected]")
    if selected:
        return _clean(" ".join(selected[0].itertext()))
    values = tree.xpath("//select[@id='gruppa']/@value")
    return _clean(values[0]) if values and _clean(values[0]) else None


def _source_date(tree) -> date:
    candidates = tree.xpath("//*[@id='todayDate']//text()")
    text = _clean(" ".join(candidates))
    match = _DATE_RE.search(text)
    if not match:
        raise ScheduleParseError("Источник не подтвердил выбранную дату")
    day, month, year = (int(part) for part in match.groups())
    return date(year, month, day)


def _parity(tree) -> str:
    text = _clean(" ".join(tree.xpath("//*[@id='schedule-container']//h2//text()"))).lower()
    for value in ("числитель", "знаменатель"):
        if value in text:
            return value
    raise ScheduleParseError("Источник не подтвердил числитель/знаменатель")


def _weekday_from_cell(text: str) -> str | None:
    normalized = _clean(text).rstrip(".")
    return normalized if normalized in _WEEKDAYS.values() else None


def _lesson_from_cells(cells: list) -> Lesson | None:
    if not cells:
        return None
    cell_text = [_clean(" ".join(cell.itertext())) for cell in cells]
    time_match = _TIME_RE.match(cell_text[0])
    if not time_match:
        return None

    time_text = f"{time_match.group(1)}–{time_match.group(2)}"
    room_text = (
        re.sub(r"^Ауд\.\s*", "", cell_text[1], flags=re.IGNORECASE).strip()
        if len(cells) > 1
        else ""
    )
    subgroup = cell_text[2].strip() if len(cells) > 2 else ""
    details = cells[3] if len(cells) > 3 else cells[-1]
    detail_lines = _lines(details)
    subjects = details.xpath(".//b//text() | .//strong//text()")
    subject = _clean(" ".join(subjects))
    if not subject:
        raise ScheduleParseError("Не удалось определить предмет в строке занятия")

    cancelled = any("отмен" in line.casefold() for line in detail_lines)
    remaining = [
        line for line in detail_lines if line != subject and "отмен" not in line.casefold()
    ]
    lesson_type = remaining[0] if remaining else None
    teacher = remaining[-1] if len(remaining) > 1 else None
    middle = tuple(remaining[1:-1]) if len(remaining) > 2 else ()
    return Lesson(
        time=time_text,
        subject=subject,
        room=room_text or None,
        teacher=teacher,
        subgroup=subgroup or None,
        lesson_type=lesson_type,
        extra=middle,
        cancelled=cancelled,
    )


def parse_schedule_html(source_html: str, requested_date: date, group: str) -> DaySchedule:
    try:
        tree = html.fromstring(source_html)
    except (ValueError, TypeError) as exc:
        raise ScheduleParseError("Источник вернул некорректный HTML") from exc

    containers = tree.xpath("//*[@id='schedule-container']")
    if not containers:
        raise ScheduleParseError("Источник не содержит расписание")

    source_date = _source_date(tree)
    if source_date != requested_date:
        raise ScheduleParseError("Дата источника не совпадает с запрошенной")

    selected_group = _selected_group(tree)
    if selected_group != group:
        raise ScheduleParseError("Источник не подтвердил выбранную группу")

    target_weekday = _WEEKDAYS[requested_date.weekday()]
    active_weekday: str | None = None
    lessons: list[Lesson] = []
    target_seen = False
    explicit_no_lessons = False

    for row in containers[0].xpath(".//tr[not(contains(@style, 'display:none'))]"):
        cells = row.xpath("./th | ./td")
        if not cells:
            continue
        if all(cell.tag.lower() == "th" for cell in cells):
            continue
        first_text = _clean(" ".join(cells[0].itertext()))
        row_weekday = _weekday_from_cell(first_text)
        if row_weekday:
            active_weekday = row_weekday
            cells = cells[1:]
        if active_weekday != target_weekday:
            continue
        target_seen = True
        joined = _clean(" ".join(" ".join(cell.itertext()) for cell in cells))
        if "Нет занятий" in joined:
            explicit_no_lessons = True
            continue
        lesson = _lesson_from_cells(cells)
        if lesson is None:
            raise ScheduleParseError("Не удалось распознать строку выбранного дня")
        lessons.append(lesson)

    if not target_seen:
        raise ScheduleParseError("Источник не содержит выбранный день недели")
    if not lessons and not explicit_no_lessons:
        raise ScheduleParseError("Источник не подтвердил наличие или отсутствие занятий")

    return DaySchedule(
        group=group,
        schedule_date=requested_date,
        parity=_parity(tree),
        lessons=tuple(lessons),
        empty_confirmed=explicit_no_lessons,
    )
