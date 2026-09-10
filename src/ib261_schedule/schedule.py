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
_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


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
    if values and _clean(values[0]):
        return _clean(values[0])
    return None


def _group_option_exists(tree, group: str) -> bool:
    values = tree.xpath("//select[@id='gruppa']/option/@value")
    texts = [" ".join(option.itertext()) for option in tree.xpath("//select[@id='gruppa']/option")]
    return group in {_clean(value) for value in values} or group in {_clean(value) for value in texts}


def _source_date(tree) -> date:
    candidates = tree.xpath("//*[@id='todayDate']//text()")
    text = _clean(" ".join(candidates))
    match = _DATE_RE.search(text)
    if not match:
        raise ScheduleParseError("Источник не подтвердил выбранную дату")
    day, month, year = (int(part) for part in match.groups())
    return date(year, month, day)


def _parity(tree) -> str:
    nodes = tree.xpath(
        "//*[@id='weekParity']//text() | //*[@id='schedule-container']//h1//text() | "
        "//*[@id='schedule-container']//h2//text() | "
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' active ')]//text()"
    )
    text = _clean(" ".join(nodes)).lower()
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
    time_match = next((_TIME_RE.search(value) for value in cell_text), None)
    if not time_match:
        return None

    time_text = f"{time_match.group(1)}–{time_match.group(2)}"
    lesson_nodes = []
    for cell in cells:
        lesson_nodes.extend(
            cell.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), ' lesson-name ')]"
            )
        )
    room_nodes = []
    subgroup_nodes = []
    type_nodes = []
    teacher_nodes = []
    for cell in cells:
        room_nodes.extend(
            cell.xpath(
                ".//*[contains(@class, 'room') or contains(@class, 'aud') or contains(@class, 'cabinet')]"
            )
        )
        subgroup_nodes.extend(
            cell.xpath(
                ".//*[contains(@class, 'subgroup') or contains(@class, 'podgroup') or contains(@class, 'group')]"
            )
        )
        type_nodes.extend(
            cell.xpath(".//*[contains(@class, 'lesson-type') or contains(@class, 'type-zanyatiya')]")
        )
        teacher_nodes.extend(
            cell.xpath(".//*[contains(@class, 'teacher') or contains(@class, 'prepod')]")
        )
    fallback_room = next(
        (value for value in cell_text if re.search(r"(?:Ауд\.|ауд\.|каб\.)", value)),
        cell_text[1] if len(cell_text) > 1 else "",
    )
    room_text = _clean(" ".join(room_nodes[0].itertext())) if room_nodes else fallback_room
    room_text = re.sub(r"^(?:Ауд\.|ауд\.|каб\.)\s*", "", room_text).strip()
    subgroup = _clean(" ".join(subgroup_nodes[0].itertext())) if subgroup_nodes else (cell_text[2].strip() if len(cells) > 2 else "")
    details = lesson_nodes[0] if lesson_nodes else (cells[3] if len(cells) > 3 else cells[-1])
    detail_lines = _lines(details)
    subject = _clean(" ".join(details.itertext())) if lesson_nodes else ""
    if not subject:
        subjects = details.xpath(".//b//text() | .//strong//text()")
        subject = _clean(" ".join(subjects))
    if not subject:
        raise ScheduleParseError("Не удалось определить предмет в строке занятия")

    cancelled = any("отмен" in line.casefold() for line in detail_lines)
    remaining = [
        line for line in detail_lines if line != subject and "отмен" not in line.casefold()
    ]
    lesson_type = (
        _clean(" ".join(type_nodes[0].itertext())) if type_nodes else (remaining[0] if remaining else None)
    )
    teacher = (
        _clean(" ".join(teacher_nodes[0].itertext()))
        if teacher_nodes
        else (remaining[-1] if len(remaining) > 1 else None)
    )
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
    if selected_group is not None and selected_group != group:
        raise ScheduleParseError("Источник не подтвердил выбранную группу")
    if selected_group is None and not _group_option_exists(tree, group):
        raise ScheduleParseError("Источник не содержит требуемую группу")

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
