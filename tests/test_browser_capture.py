from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import ib261_schedule.browser_capture as browser_capture
from ib261_schedule.browser_capture import capture_live

HTML = """<!doctype html><html><head><meta charset='utf-8'><style>
#schedule-container { width: 700px; background: white; color: black; padding: 10px; }
td { border: 1px solid black; padding: 8px; }
</style></head><body>
<div id='todayDate'>Сегодня 08.09.2026, знаменатель</div>
<select id='gruppa'><option selected>ИБ-261</option></select>
<div id='schedule-container'><h2>Расписание на знаменатель</h2><table>
<tr><td rowspan='2'><b>Вт.</b></td><td>08:30 - 10:05</td><td>Ауд. 430/3</td><td>1 п/г</td><td>Лабораторные занятия<br><b>Физика</b><br>Иванов Иван Иванович</td></tr>
<tr><td>10:15 - 11:50</td><td>Ауд. 327/1</td><td>2 п/г</td><td>Практические занятия<br><b>История России</b><br>Петров Пётр Петрович</td></tr>
<tr><td><b>Ср.</b></td><td>09:45 - 11:20</td><td>Ауд. 302/2</td><td></td><td>Практические занятия<br><b>Русский язык</b><br>Сидоров Сидор Сидорович</td></tr>
</table></div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode("utf-8"))

    def log_message(self, *args):
        pass


def test_capture_live_freezes_then_revalidates_filtered_dom_before_screenshot(
    tmp_path: Path, monkeypatch
):
    calls: list[str] = []
    real_parse = browser_capture.parse_schedule_html

    def recording_parse(source_html, target, group):
        calls.append(source_html)
        return real_parse(source_html, target, group)

    monkeypatch.setattr(browser_capture, "parse_schedule_html", recording_parse)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output = tmp_path / "schedule.png"
        capture_live(
            date(2026, 9, 8),
            "ИБ-261",
            output,
            url_builder=lambda *_: f"http://127.0.0.1:{server.server_port}/schedule",
        )
    finally:
        server.shutdown()
        thread.join()

    assert len(calls) == 2
    assert "Русский язык" in calls[0]
    assert "Русский язык" not in calls[1]
    assert "Физика" in calls[1] and "История России" in calls[1]


def test_capture_live_parses_and_screenshots_the_same_dom(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output = tmp_path / "schedule.png"
        schedule, checked_at = capture_live(
            date(2026, 9, 8),
            "ИБ-261",
            output,
            url_builder=lambda *_: f"http://127.0.0.1:{server.server_port}/schedule",
        )
    finally:
        server.shutdown()
        thread.join()

    assert schedule.schedule_date == date(2026, 9, 8)
    assert [lesson.subject for lesson in schedule.lessons] == ["Физика", "История России"]
    assert checked_at.tzinfo is not None
    assert output.is_file() and output.stat().st_size > 1000
