from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ib261_schedule.cache import CacheStore
from ib261_schedule.schedule import DaySchedule
from ib261_schedule.service import ScheduleService

MOSCOW = ZoneInfo("Europe/Moscow")
TARGET = date(2026, 9, 8)
CHECKED = datetime(2026, 9, 8, 9, 1, tzinfo=MOSCOW)
SCHEDULE = DaySchedule("ИБ-261", TARGET, "знаменатель", ())


def test_service_caches_only_complete_live_pair(tmp_path: Path):
    screenshot = tmp_path / "live.png"
    screenshot.write_bytes(b"png")

    def capture(target: date, group: str, output: Path):
        output.write_bytes(screenshot.read_bytes())
        return SCHEDULE, CHECKED

    service = ScheduleService(CacheStore(tmp_path / "cache"), capture)
    result = service.get(TARGET)

    assert result.status == "fresh"
    assert result.schedule == SCHEDULE
    assert result.checked_at == CHECKED
    assert result.screenshot is not None and result.screenshot.is_file()
    assert CacheStore(tmp_path / "cache").load(TARGET, "ИБ-261") is not None


def test_service_returns_same_date_cache_marked_stale_when_live_source_fails(tmp_path: Path):
    original = tmp_path / "original.png"
    original.write_bytes(b"png")
    cache = CacheStore(tmp_path / "cache")
    cache.save(SCHEDULE, CHECKED, original)

    def fail_capture(target: date, group: str, output: Path):
        raise RuntimeError("403 with secret-looking details")

    result = ScheduleService(cache, fail_capture).get(TARGET)
    assert result.status == "stale"
    assert result.schedule == SCHEDULE
    assert result.checked_at == CHECKED
    assert result.error == "Источник расписания сейчас недоступен"


def test_service_does_not_substitute_cache_from_another_date(tmp_path: Path):
    original = tmp_path / "original.png"
    original.write_bytes(b"png")
    cache = CacheStore(tmp_path / "cache")
    cache.save(SCHEDULE, CHECKED, original)

    def fail_capture(target: date, group: str, output: Path):
        raise RuntimeError("403")

    result = ScheduleService(cache, fail_capture).get(date(2026, 9, 9))
    assert result.status == "unavailable"
    assert result.schedule is None
    assert result.screenshot is None
    assert result.error == "Источник расписания сейчас недоступен"
