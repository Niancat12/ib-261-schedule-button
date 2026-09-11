import hashlib
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ib261_schedule.schedule import DaySchedule
from ib261_schedule.service import AcquisitionResult
from ib261_schedule.worker import build_payload

TARGET = date(2026, 9, 8)
SCHEDULE = DaySchedule("ИБ-261", TARGET, "знаменатель", ())
CHECKED = datetime(2026, 9, 8, 9, 1, tzinfo=ZoneInfo("Europe/Moscow"))
EXPECTED_KEYS = {
    "status",
    "text",
    "requested_date",
    "group",
    "cache_version",
    "screenshot",
    "screenshot_sha256",
    "parity",
    "capture_id",
    "checked_at",
    "source_url",
    "json_sha256",
    "crop_warning",
}


def result_with_photo(status: str, photo: Path) -> AcquisitionResult:
    return AcquisitionResult(
        status,
        SCHEDULE,
        CHECKED,
        photo,
        cache_version="version-1",
        screenshot_sha256=hashlib.sha256(photo.read_bytes()).hexdigest(),
    )


def test_worker_payload_fresh_has_strict_identity_and_photo_schema(tmp_path: Path):
    photo = tmp_path / "schedule.png"
    photo.write_bytes(b"png")
    payload = build_payload(result_with_photo("fresh", photo), TARGET)

    assert set(payload) == EXPECTED_KEYS
    assert payload["status"] == "fresh"
    assert payload["requested_date"] == "2026-09-08"
    assert payload["group"] == "ИБ-261"
    assert payload["cache_version"] == "version-1"
    assert payload["screenshot"] == str(photo)
    assert payload["screenshot_sha256"] == hashlib.sha256(b"png").hexdigest()
    assert payload["parity"] == "знаменатель"
    assert payload["capture_id"] == "version-1"
    assert payload["checked_at"] == CHECKED.isoformat()
    assert "РАНЕЕ ПОЛУЧЕННЫЕ" not in payload["text"]


def test_worker_payload_stale_marks_previously_received_data(tmp_path: Path):
    photo = tmp_path / "schedule.png"
    photo.write_bytes(b"png")
    payload = build_payload(result_with_photo("stale", photo), TARGET)
    assert payload["status"] == "stale"
    assert payload["text"].startswith("⚠️ РАНЕЕ ПОЛУЧЕННЫЕ ДАННЫЕ")


def test_worker_payload_unavailable_forbids_photo_and_cache_identity():
    payload = build_payload(
        AcquisitionResult("unavailable", None, None, None, "Источник расписания сейчас недоступен"),
        TARGET,
    )
    assert set(payload) == EXPECTED_KEYS
    assert payload["status"] == "unavailable"
    assert payload["requested_date"] == "2026-09-08"
    assert payload["group"] == "ИБ-261"
    assert payload["cache_version"] is None
    assert payload["screenshot"] is None
    assert payload["screenshot_sha256"] is None
    assert payload["parity"] is None
    assert payload["capture_id"] is None
    assert payload["json_sha256"] is None
    assert payload["crop_warning"] is False
    assert "Источник расписания сейчас недоступен" in payload["text"]
    assert "https://cchgeu.ru/studentu/onlayn-raspisanie/" in payload["text"]


@pytest.mark.parametrize("status", ["fresh", "stale"])
def test_worker_payload_rejects_fresh_or_stale_without_verified_screenshot(status: str):
    result = AcquisitionResult(status, SCHEDULE, CHECKED, None)
    with pytest.raises(ValueError, match="скриншот"):
        build_payload(result, TARGET)


def test_worker_payload_rejects_mismatched_requested_identity(tmp_path: Path):
    photo = tmp_path / "schedule.png"
    photo.write_bytes(b"png")
    with pytest.raises(ValueError, match="запрос"):
        build_payload(result_with_photo("fresh", photo), date(2026, 9, 9))


def test_worker_uses_new_live_capture_for_each_request_before_publication_fallback(tmp_path, monkeypatch):
    import ib261_schedule.worker as worker

    calls = []

    def live(target, group, output):
        calls.append(target)
        output.write_bytes(b"live-png")
        return DaySchedule(group, target, "знаменатель", ()), CHECKED

    monkeypatch.setattr(worker, "capture_live", live)
    monkeypatch.setattr(worker, "load_remote_snapshot", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fallback used")))
    first = worker.run(TARGET, tmp_path / "cache")
    second = worker.run(TARGET, tmp_path / "cache")
    assert calls == [TARGET, TARGET]
    assert first["status"] == second["status"] == "fresh"
    assert first["capture_id"] != second["capture_id"]
