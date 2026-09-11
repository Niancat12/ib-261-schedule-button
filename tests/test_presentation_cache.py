import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ib261_schedule.cache import CacheStore
from ib261_schedule.presentation import format_schedule, format_week_schedule
from ib261_schedule.schedule import DaySchedule, Lesson
from ib261_schedule.source import build_canonical_url, build_source_url

MOSCOW = ZoneInfo("Europe/Moscow")


def sample_schedule() -> DaySchedule:
    return DaySchedule(
        group="ИБ-261",
        schedule_date=date(2026, 9, 8),
        parity="знаменатель",
        lessons=(
            Lesson(
                time="08:30–10:05",
                subject="Физика",
                room="430/3",
                teacher="Иванов Иван Иванович",
                subgroup="1 п/г",
                lesson_type="Лабораторные занятия",
            ),
            Lesson(
                time="08:30–10:05",
                subject="Физика",
                room=None,
                teacher="Петров Пётр Петрович",
                subgroup="2 п/г",
                lesson_type="Лабораторные занятия",
            ),
        ),
    )


def test_source_url_preserves_exact_group_and_iso_date():
    url = build_source_url(date(2026, 9, 8), "ИБ-261")
    assert "date=2026-09-08" in url
    assert "%D0%98%D0%91-261" in url
    assert "prepodavatel=" in url


def test_canonical_source_url_encodes_group_date_and_parity_path():
    url = build_canonical_url(date(2026, 9, 10), "ИБ-261", "знаменатель")
    assert url.endswith(
        "/%D0%98%D0%91-261/2026-09-10/"
        "%D0%B7%D0%BD%D0%B0%D0%BC%D0%B5%D0%BD%D0%B0%D1%82%D0%B5%D0%BB%D1%8C"
    )


def test_format_schedule_includes_date_weekday_subgroups_source_and_check_time():
    checked = datetime(2026, 9, 8, 9, 1, tzinfo=MOSCOW)
    text = format_schedule(sample_schedule(), checked, stale=False)
    assert "8 сентября 2026" in text
    assert "Вторник" in text
    assert "1 п/г" in text and "2 п/г" in text
    assert "Аудитория: 430/3" in text
    assert "Аудитория" not in text.split("2 п/г", 1)[1]
    assert "Проверено: 08.09.2026 09:01 MSK" in text
    assert "https://cchgeu.ru/studentu/onlayn-raspisanie/" in text


def test_format_stale_cache_is_unmistakably_marked():
    text = format_schedule(
        sample_schedule(), datetime(2026, 9, 7, 10, 0, tzinfo=MOSCOW), stale=True
    )
    assert text.startswith("⚠️ РАНЕЕ ПОЛУЧЕННЫЕ ДАННЫЕ")


def test_week_format_has_calendar_span_and_all_day_headings():
    monday = date(2026, 9, 7)
    schedules = {monday + timedelta(days=i): DaySchedule(
        "ИБ-261", monday + timedelta(days=i), "знаменатель", ()
    ) for i in range(7)}
    text = format_week_schedule(schedules, datetime(2026, 9, 11, 12, tzinfo=MOSCOW), parity="знаменатель")
    assert "Расписание ИБ-261 на неделю с 7 по 13 сентября 2026" in text
    assert "Понедельник" in text and "Воскресенье" in text


def test_cache_commits_json_and_screenshot_as_one_snapshot(tmp_path: Path):
    screenshot = tmp_path / "fresh.png"
    screenshot.write_bytes(b"real-png")
    store = CacheStore(tmp_path / "cache")
    checked = datetime(2026, 9, 8, 9, 1, tzinfo=MOSCOW)

    store.save(sample_schedule(), checked, screenshot)
    loaded = store.load(date(2026, 9, 8), "ИБ-261")

    assert loaded is not None
    assert loaded.schedule == sample_schedule()
    assert loaded.checked_at == checked
    assert loaded.screenshot.read_bytes() == b"real-png"
    assert loaded.screenshot.parent == loaded.metadata.parent

    loaded.screenshot.write_bytes(b"tampered")
    assert store.load(date(2026, 9, 8), "ИБ-261") is None


def test_cache_returns_none_for_a_different_date(tmp_path: Path):
    screenshot = tmp_path / "fresh.png"
    screenshot.write_bytes(b"real-png")
    store = CacheStore(tmp_path / "cache")
    store.save(sample_schedule(), datetime(2026, 9, 8, 9, 1, tzinfo=MOSCOW), screenshot)
    assert store.load(date(2026, 9, 9), "ИБ-261") is None


def test_cache_rejects_every_group_except_exact_ib261(tmp_path: Path):
    screenshot = tmp_path / "fresh.png"
    screenshot.write_bytes(b"real-png")
    invalid = DaySchedule("ИБ261", date(2026, 9, 8), "знаменатель", ())
    store = CacheStore(tmp_path / "cache")

    with pytest.raises(ValueError, match="групп"):
        store.save(invalid, datetime(2026, 9, 8, 9, 1, tzinfo=MOSCOW), screenshot)
    with pytest.raises(ValueError, match="групп"):
        store.load(date(2026, 9, 8), "../ИБ-261")


def test_cache_rejects_symlink_components(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "ИБ-261").symlink_to(outside, target_is_directory=True)
    screenshot = tmp_path / "fresh.png"
    screenshot.write_bytes(b"real-png")
    store = CacheStore(cache_root)

    with pytest.raises(ValueError, match="символ"):
        store.save(sample_schedule(), datetime(2026, 9, 8, 9, 1, tzinfo=MOSCOW), screenshot)
    assert store.load(date(2026, 9, 8), "ИБ-261") is None


def test_cache_snapshot_exposes_version_and_verified_hash(tmp_path: Path):
    screenshot = tmp_path / "fresh.png"
    screenshot.write_bytes(b"real-png")
    store = CacheStore(tmp_path / "cache")
    snapshot = store.save(sample_schedule(), datetime(2026, 9, 8, 9, 1, tzinfo=MOSCOW), screenshot)

    assert snapshot.version == snapshot.screenshot.parent.name
    assert snapshot.screenshot_sha256 == hashlib.sha256(b"real-png").hexdigest()
