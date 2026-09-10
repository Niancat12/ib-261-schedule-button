from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from ib261_schedule.browser_capture import _png_stats, capture_live
from ib261_schedule.schedule import parse_week_schedule_html


def _weekly_html() -> str:
    labels = ("Пн.", "Вт.", "Ср.", "Чт.", "Пт.", "Сб.", "Вс.")
    rows = []
    for day_index, label in enumerate(labels):
        for lesson_index in range(3):
            rows.append(
                "<tr>"
                f"<td><b>{label}</b></td>"
                f"<td>{8 + lesson_index:02d}:30 - {10 + lesson_index:02d}:05</td>"
                f"<td>Ауд. {400 + day_index}</td><td>{lesson_index + 1} п/г</td>"
                f"<td>Практические занятия<br><span class='lesson-name'>Предмет {day_index}-{lesson_index}</span><br>Преподаватель</td>"
                "</tr>"
            )
    return """<!doctype html><html><head><title>Официальное расписание</title><style>
      #schedule-container { display: none; width: 900px; background: white; color: black; padding: 12px; }
      td { border: 1px solid #111; padding: 8px; }
    </style></head><body>
      <div id='todayDate'>Сегодня 10.9.2026</div>
      <div id='weekParity'><button class='active'>знаменатель</button></div>
      <select id='gruppa'><option value=''>Все</option></select>
      <select id='prepodavatel'><option value=''>Выберите преподавателя</option></select>
      <div id='schedule-container'><h2>Расписание на знаменатель</h2><table>
        """ + "".join(rows) + """
      </table></div>
      <script>
        // The real site performs this destructive reset after the document loads.
        document.querySelector('#gruppa').value = '';
        document.querySelector('#schedule-container').innerHTML = 'Выберете преподавателя или группу';
      </script>
    </body></html>"""


class _Handler(BaseHTTPRequestHandler):
    body = _weekly_html().encode("utf-8")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        pass


def test_thursday_target_uses_monday_anchored_week_and_ignores_live_reset(tmp_path: Path, monkeypatch):
    hashes = []
    import ib261_schedule.browser_capture as browser_capture

    real_fragment = browser_capture._schedule_fragment

    def recording_fragment(page):
        fragment = real_fragment(page)
        hashes.append(browser_capture._sha256_text(fragment.get("tableHtml", "")))
        return fragment

    monkeypatch.setattr(browser_capture, "_schedule_fragment", recording_fragment)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output = tmp_path / "schedule.png"
        schedule, _checked_at = capture_live(
            date(2026, 9, 10),
            "ИБ-261",
            output,
            url_builder=lambda *_: f"http://127.0.0.1:{server.server_port}/schedule",
        )
    finally:
        server.shutdown()
        thread.join()

    assert schedule.schedule_date == date(2026, 9, 10)
    assert len(schedule.lessons) == 3
    assert schedule.lessons[0].subject == "Предмет 3-0"
    assert output.is_file()
    assert _png_stats(output.read_bytes())["distinct_colors"] > 1
    assert hashes[0] == hashes[-1]


def test_fixture_contains_all_21_lessons_and_calendar_week_dates():
    html = _weekly_html()
    week = parse_week_schedule_html(
        html, date(2026, 9, 10), "ИБ-261", group_confirmed_by_url=True
    )
    assert sum(len(day.lessons) for day in week.values()) == 21
    assert sorted(week) == [date(2026, 9, day) for day in range(7, 14)]
    assert week[date(2026, 9, 10)].lessons[0].subject == "Предмет 3-0"


def test_week_parser_handles_month_and_year_boundaries():
    html = _weekly_html().replace("10.9.2026", "01.01.2027")
    week = parse_week_schedule_html(
        html, date(2027, 1, 1), "ИБ-261", group_confirmed_by_url=True
    )
    assert sorted(week) == [date(2026, 12, 28), date(2026, 12, 29), date(2026, 12, 30), date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2), date(2027, 1, 3)]
