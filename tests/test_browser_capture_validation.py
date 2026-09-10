from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

import ib261_schedule.browser_capture as browser_capture
from ib261_schedule.browser_capture import SourceIntegrityError, capture_live


def test_validate_canonical_url_compares_decoded_path_segments():
    requested = (
        "https://cchgeu.ru/studentu/onlayn-raspisanie/"
        "%D0%98%D0%91-261/2026-09-10/%D0%B7%D0%BD%D0%B0%D0%BC%D0%B5%D0%BD%D0%B0%D1%82%D0%B5%D0%BB%D1%8C"
    )
    accepted = requested + "/"
    assert browser_capture._validate_canonical_url(
        accepted, requested, date(2026, 9, 10), "ИБ-261", "знаменатель"
    )[-3:] == ["ИБ-261", "2026-09-10", "знаменатель"]
    wrong_urls = (
        accepted.replace("%D0%98%D0%91-261", "%D0%98%D0%91-262"),
        accepted.replace("2026-09-10", "2026-09-11"),
        accepted.replace(
            "%D0%B7%D0%BD%D0%B0%D0%BC%D0%B5%D0%BD%D0%B0%D1%82%D0%B5%D0%BB%D1%8C",
            "%D1%87%D0%B8%D1%81%D0%BB%D0%B8%D1%82%D0%B5%D0%BB%D1%8C",
        ),
    )
    for wrong in wrong_urls:
        with pytest.raises(SourceIntegrityError):
            browser_capture._validate_canonical_url(
                wrong, requested, date(2026, 9, 10), "ИБ-261", "знаменатель"
            )


def test_canonical_hidden_schedule_is_valid_without_selected_group(tmp_path: Path):
    html = """<!doctype html><html><head><style>
      #schedule-container { display: none; width: 720px; color: #111; background: #fff; }
      td { border: 1px solid #111; padding: 12px; }
    </style></head><body>
      <div id="todayDate">Сегодня 10.9.2026</div>
      <div id="weekParity"><button class="active">знаменатель</button></div>
      <select id="gruppa"><option value="ИБ-261">ИБ-261</option><option value="ИБ-262">ИБ-262</option></select>
      <select id="prepodavatel"><option value="">Выберите преподавателя</option></select>
      <div id="schedule-container"><h2>Расписание на знаменатель</h2><table>
        <tr><td><b>Чт.</b></td><td>08:30 - 10:05</td><td>Ауд. 430/3</td><td>1 п/г</td>
          <td><span class="lesson-name">Математика</span><br>Практические занятия<br>Иванов</td></tr>
      </table></div>
    </body></html>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        schedule, _checked_at = capture_live(
            date(2026, 9, 10),
            "ИБ-261",
            tmp_path / "schedule.png",
            url_builder=lambda *_: f"http://127.0.0.1:{server.server_port}/schedule",
        )
    finally:
        server.shutdown()
        thread.join()

    assert schedule.group == "ИБ-261"
    assert len(schedule.lessons) == 1
    image = (tmp_path / "schedule.png").read_bytes()
    assert browser_capture._png_stats(image)["distinct_colors"] > 1


def test_transient_capture_errors_are_retried_at_most_three_times(monkeypatch, tmp_path: Path):
    calls = []

    def flaky(*_args, **_kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise browser_capture.SourceUnavailable("официальный источник HTTP 500")
        return object(), object()

    monkeypatch.setattr(browser_capture, "_capture_live_once", flaky)
    result = browser_capture.capture_live(date(2026, 9, 10), "ИБ-261", tmp_path / "x.png")
    assert result[0] is not None
    assert len(calls) == 3


def test_http_403_is_not_retried(monkeypatch, tmp_path: Path):
    calls = []

    def blocked(*_args, **_kwargs):
        calls.append(1)
        raise browser_capture.SourceUnavailable("официальный источник HTTP 403")

    monkeypatch.setattr(browser_capture, "_capture_live_once", blocked)
    with pytest.raises(browser_capture.SourceUnavailable):
        browser_capture.capture_live(date(2026, 9, 10), "ИБ-261", tmp_path / "x.png")
    assert len(calls) == 1


def test_corrupt_png_is_rejected():
    with pytest.raises(SourceIntegrityError):
        browser_capture._png_stats(b"not-a-png")
