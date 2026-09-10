from datetime import date

import pytest

from ib261_schedule.schedule import ScheduleParseError, parse_schedule_html

HTML = """
<html><body>
<div id="todayDate">Сегодня 08.09.2026, знаменатель</div>
<select id="gruppa"><option value="Все">Все</option><option value="ИБ-261" selected>ИБ-261</option></select>
<div id="schedule-container">
<section class="schedule-container visible">
<h2 class="schedule-date">Расписание на знаменатель</h2>
<table>
<tr><td rowspan="3"><b>Вт.</b></td><td>08:30 - 10:05</td><td>Ауд. 430/3</td><td></td><td>Лекционные занятия<br><b>Физическая культура и спорт</b><br>Вялых Надежда Николаевна</td></tr>
<tr><td>10:15 - 11:50</td><td>Ауд.</td><td></td><td>Практические занятия<br><b>Элективные дисциплины по физической культуре и спорту</b><br>Вялых Надежда Николаевна</td></tr>
<tr><td>13:30 - 15:05</td><td>Ауд. 327/1</td><td></td><td>Лекционные занятия<br><b>История России</b><br>Золотарев Антон Юрьевич</td></tr>
<tr><td rowspan="2"><b>Ср.</b></td><td>15:15 - 16:50</td><td>Ауд.</td><td>1 п/г</td><td>Лабораторные занятия<br><b>Физика</b><br>Тураева Татьяна Леонидовна</td></tr>
<tr><td>15:15 - 16:50</td><td>Ауд. 320/1</td><td>2 п/г</td><td>Лабораторные занятия<br><b>Физика</b><br>Ремизова Оксана Ивановна</td></tr>
</table>
</section>
</div>
</body></html>
"""


def test_parse_selected_day_extracts_lessons_without_inventing_blank_fields():
    schedule = parse_schedule_html(HTML, date(2026, 9, 8), "ИБ-261")

    assert schedule.group == "ИБ-261"
    assert schedule.schedule_date == date(2026, 9, 8)
    assert schedule.parity == "знаменатель"
    assert len(schedule.lessons) == 3
    assert schedule.lessons[0].time == "08:30–10:05"
    assert schedule.lessons[0].subject == "Физическая культура и спорт"
    assert schedule.lessons[0].room == "430/3"
    assert schedule.lessons[0].teacher == "Вялых Надежда Николаевна"
    assert schedule.lessons[1].room is None
    assert schedule.lessons[1].subgroup is None


def test_parse_day_preserves_two_subgroups_at_same_time():
    schedule = parse_schedule_html(
        HTML.replace("08.09.2026", "09.09.2026"), date(2026, 9, 9), "ИБ-261"
    )
    assert [lesson.subgroup for lesson in schedule.lessons] == ["1 п/г", "2 п/г"]


def test_parse_fails_closed_when_selected_group_does_not_match():
    with pytest.raises(ScheduleParseError, match="групп"):
        parse_schedule_html(HTML, date(2026, 9, 8), "КБ-261")


def test_parse_empty_day_reports_no_lessons():
    html = """
    <div id="todayDate">Сегодня 08.09.2026, знаменатель</div>
    <select id="gruppa"><option value="ИБ-261" selected>ИБ-261</option></select>
    <div id="schedule-container"><h2>Расписание на знаменатель</h2><table>
    <tr><td><b>Вт.</b></td><td>Нет занятий</td></tr>
    </table></div>
    """
    schedule = parse_schedule_html(html, date(2026, 9, 8), "ИБ-261")
    assert schedule.lessons == ()


def test_parse_fails_closed_for_unrecognized_selected_day_row():
    html = HTML.replace("08:30 - 10:05", "время уточняется")
    with pytest.raises(ScheduleParseError, match="строк"):
        parse_schedule_html(html, date(2026, 9, 8), "ИБ-261")


def test_parse_fails_closed_when_selected_day_has_neither_lesson_nor_explicit_empty_marker():
    html = (
        HTML.replace(
            '<tr><td rowspan="3"><b>Вт.</b></td><td>08:30 - 10:05</td><td>Ауд. 430/3</td><td></td><td>Лекционные занятия<br><b>Физическая культура и спорт</b><br>Вялых Надежда Николаевна</td></tr>',
            "<tr><td><b>Вт.</b></td><td></td></tr>",
        )
        .replace(
            "<tr><td>10:15 - 11:50</td><td>Ауд.</td><td></td><td>Практические занятия<br><b>Элективные дисциплины по физической культуре и спорту</b><br>Вялых Надежда Николаевна</td></tr>",
            "",
        )
        .replace(
            "<tr><td>13:30 - 15:05</td><td>Ауд. 327/1</td><td></td><td>Лекционные занятия<br><b>История России</b><br>Золотарев Антон Юрьевич</td></tr>",
            "",
        )
    )
    with pytest.raises(ScheduleParseError, match="выбранного дня"):
        parse_schedule_html(html, date(2026, 9, 8), "ИБ-261")


def test_parse_marks_cancelled_lesson():
    html = HTML.replace(
        "Лекционные занятия<br><b>История России</b><br>Золотарев Антон Юрьевич",
        "Отменено<br>Лекционные занятия<br><b>История России</b><br>Золотарев Антон Юрьевич",
    )
    schedule = parse_schedule_html(html, date(2026, 9, 8), "ИБ-261")
    history = next(lesson for lesson in schedule.lessons if lesson.subject == "История России")
    assert history.cancelled is True
