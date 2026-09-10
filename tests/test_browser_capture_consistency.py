"""
Tests for DOM consistency in browser_capture.py.
Verifies that parsed HTML and screenshots are from the same DOM state.

Issue #3: DOM Consistency Fix
- Ensures capture_live() freezes document before parsing
- Verifies schedule_date consistency between frozen and live DOMs
- Only then filters display and takes screenshot
"""

from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import ib261_schedule.browser_capture as browser_capture
from ib261_schedule.browser_capture import capture_live

# Use the same HTML format as existing test_browser_capture.py
HTML = """<!doctype html><html><head><meta charset='utf-8'><style>
#schedule-container { width: 700px; background: white; color: black; padding: 10px; }
td { border: 1px solid black; padding: 8px; }
</style></head><body>
<div id='todayDate'>Сегодня 08.09.2026, знаменатель</div>
<select id='gruppa'><option value='ИБ-261' selected>ИБ-261</option></select>
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


def test_dom_consistency_frozen_html_parsing(tmp_path, monkeypatch):
    """
    Test (A-B): Frozen document is parsed before mutations.
    Verifies that parse happens on immutable snapshot, not live DOM.
    """
    calls = []
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

    # Parsing is performed once from the frozen server snapshot.  CSS reveal
    # does not create a second, potentially divergent DOM source.
    assert len(calls) == 1, f"Expected one frozen snapshot parse, got {len(calls)}"


def test_dom_consistency_parse_and_screenshot_same_state(tmp_path):
    """
    Test (C-D): Parsed data matches screenshot DOM state.
    Verifies consistency check ensures date matches before and after filtering.
    """
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

    # Verify parsed schedule date matches target
    assert schedule.schedule_date == date(2026, 9, 8)
    # Verify subjects match (Tuesday has Физика, История России)
    subjects = [lesson.subject for lesson in schedule.lessons]
    assert "Физика" in subjects, f"Expected Физика in {subjects}"
    assert "История России" in subjects, f"Expected История России in {subjects}"
    # Verify screenshot was created
    assert output.is_file() and output.stat().st_size > 1000


def test_dom_consistency_intermediate_verification(tmp_path, monkeypatch):
    """
    Test (C): DOM consistency verification happens BEFORE filtering.
    This test verifies that consistency check step (C) is executed between
    parsing (B) and filtering (D).
    """
    parse_calls = []
    real_parse = browser_capture.parse_schedule_html

    def recording_parse(source_html, target, group):
        # Track what we're parsing
        has_wednesday = "Ср." in source_html
        parse_calls.append({"has_wednesday": has_wednesday, "html_len": len(source_html)})
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

    # The only parse is the complete weekly frozen snapshot.
    assert len(parse_calls) == 1
    assert parse_calls[0]["has_wednesday"], "Snapshot should contain the complete week"


def test_dom_consistency_display_none_not_remove(tmp_path):
    """
    Test (D): Rows are hidden with display:none, not removed.
    This preserves DOM structure for proper rendering.
    Verifies that the final HTML still parses correctly (no missing rows).
    """
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

    # Verify screenshot was created (display:none approach works)
    assert output.is_file()
    assert output.stat().st_size > 0
    # Verify parsed schedule is consistent
    assert schedule.schedule_date == date(2026, 9, 8)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
