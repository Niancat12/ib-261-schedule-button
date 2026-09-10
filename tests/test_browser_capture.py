from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import Locator

import ib261_schedule.browser_capture as browser_capture
from ib261_schedule.browser_capture import capture_live

HTML = """<!doctype html><html><head><meta charset='utf-8'><style>
#schedule-container { width: 700px; background: white; color: black; padding: 10px; }
td { border: 1px solid black; padding: 8px; }
</style></head><body>
<div id='todayDate'>Сегодня 08.09.2026, знаменатель</div>
<select id='gruppa'><option value='ИБ-261' selected>ИБ-261</option><option value='1321 Б'>1321 Б</option></select>
<select id='prepodavatel'><option value=''>Выберите преподавателя</option></select>
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


def test_capture_live_selects_only_late_group_once_and_records_target_ajax(tmp_path: Path, monkeypatch):
    options = "".join(f"<option value='G-{index}'>G-{index}</option>" for index in range(4200))
    html = f"""<!doctype html><html><body>
    <div id='todayDate'>Сегодня 08.09.2026, знаменатель</div>
    <select id='gruppa'>{options}<option value='ИБ-261'>ИБ-261</option></select>
    <select id='prepodavatel'><option value=''>Выберите преподавателя</option></select>
    <span class='select2-selection__rendered'></span>
    <p id='prompt'>Выберете преподавателя или группу</p>
    <div id='schedule-container' style='display:none'><h2>Расписание на знаменатель</h2><table>
    <tr><td><b>Вт.</b></td><td>08:30 - 10:05</td><td>430</td><td></td><td>Лабораторные занятия<br><b>Физика</b><br>Иванов</td></tr>
    </table></div>
    <script>
      const group = document.querySelector('#gruppa');
      const rendered = document.querySelector('.select2-selection__rendered');
      group.addEventListener('change', () => {{
        rendered.textContent = group.value;
        if (group.value === 'ИБ-261') fetch('/schedule?group=' + encodeURIComponent(group.value)).then(() => {{
          document.querySelector('#prompt').remove();
          document.querySelector('#schedule-container').style.display='block';
        }});
      }});
    </script>
    </body></html>"""
    requests: list[str] = []

    class GroupHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers(); self.wfile.write(html.encode())
        def log_message(self, *args):
            pass
    selected_values: list[str] = []
    original_select_option = Locator.select_option

    def recording_select_option(locator, *args, **kwargs):
        selected_values.append(kwargs.get("value") or (args[0] if args else ""))
        return original_select_option(locator, *args, **kwargs)

    monkeypatch.setattr(Locator, "select_option", recording_select_option)
    server = ThreadingHTTPServer(('127.0.0.1', 0), GroupHandler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        schedule, _ = capture_live(date(2026, 9, 8), 'ИБ-261', tmp_path / 'schedule.png', url_builder=lambda *_: f'http://127.0.0.1:{server.server_port}/schedule')
    finally:
        server.shutdown(); thread.join()
    assert schedule.group == 'ИБ-261'
    assert schedule.lessons
    assert selected_values == ["ИБ-261"]
    target_requests = [path for path in requests if "group=" in path]
    assert target_requests == ["/schedule?group=%D0%98%D0%91-261"]


def test_capture_live_writes_diagnostics_when_group_selection_fails(tmp_path: Path, monkeypatch):
    diagnostics = tmp_path / "diagnostics"
    monkeypatch.setenv("SCHEDULE_DIAGNOSTIC_DIR", str(diagnostics))
    monkeypatch.setattr(
        browser_capture,
        "_select_group",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            browser_capture.ScheduleParseError("group missing")
        ),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        try:
            capture_live(date(2026, 9, 8), "ИБ-261", tmp_path / "schedule.png", url_builder=lambda *_: f"http://127.0.0.1:{server.server_port}/schedule")
        except browser_capture.ScheduleParseError:
            pass
        else:
            raise AssertionError("expected group selection failure")
    finally:
        server.shutdown(); thread.join()
    assert (diagnostics / "page.png").is_file()
    assert (diagnostics / "page.html").is_file()
    metadata = (diagnostics / "metadata.json").read_text(encoding="utf-8")
    assert '"url"' in metadata and '"selects"' in metadata
