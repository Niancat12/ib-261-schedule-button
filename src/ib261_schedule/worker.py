from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from tempfile import NamedTemporaryFile
from pathlib import Path
from zoneinfo import ZoneInfo

from .cache import CacheStore
from .browser_capture import capture_live, capture_live_week
from .presentation import format_schedule, format_week_schedule
from .published import load_latest, load_remote_snapshot
from .schedule import DaySchedule, Lesson
from .service import AcquisitionResult
from .source import SOURCE_URL, build_canonical_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "published-schedules"
GROUP = "ИБ-261"
STATUSES = {"fresh", "stale", "unavailable"}
PUBLISHED_MAX_AGE = timedelta(hours=36)


def build_payload(
    result: AcquisitionResult, requested_date: date, group: str = GROUP, *, crop_warning: bool = False
) -> dict[str, object]:
    if group != GROUP:
        raise ValueError("Некорректная группа запроса")
    if result.status not in STATUSES:
        raise ValueError("Некорректный статус обработчика")

    if result.status in {"fresh", "stale"}:
        if (
            result.schedule is None
            or result.checked_at is None
            or result.screenshot is None
            or not result.cache_version
            or not result.screenshot_sha256
        ):
            raise ValueError("Актуальный или кэшированный результат требует проверенный скриншот")
        if result.schedule.schedule_date != requested_date or result.schedule.group != group:
            raise ValueError("Результат не соответствует запросу")
        try:
            image = result.screenshot.read_bytes()
        except OSError as exc:
            raise ValueError("Проверенный скриншот недоступен") from exc
        if not image or hashlib.sha256(image).hexdigest() != result.screenshot_sha256:
            raise ValueError("Хэш скриншота не соответствует результату")
        text = format_schedule(
            result.schedule,
            result.checked_at,
            stale=result.status == "stale",
        )
        screenshot: str | None = str(result.screenshot)
        cache_version: str | None = result.cache_version
        screenshot_sha256: str | None = result.screenshot_sha256
    else:
        if any(
            value is not None
            for value in (
                result.schedule,
                result.checked_at,
                result.screenshot,
                result.cache_version,
                result.screenshot_sha256,
            )
        ):
            raise ValueError("Недоступный результат не может содержать скриншот или кэш")
        checked = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")
        text = (
            "⚠️ Источник расписания сейчас недоступен.\n"
            "Ранее полученных данных для этой даты нет.\n\n"
            f"Источник: {SOURCE_URL}\n"
            f"Последняя попытка проверки: {checked} MSK"
        )
        screenshot = None
        cache_version = None
        screenshot_sha256 = None

    capture_id = result.cache_version if result.status in {"fresh", "stale"} else None
    checked_at_value = result.checked_at.isoformat() if result.checked_at else None
    source_url = (
        build_canonical_url(requested_date, GROUP, result.schedule.parity)
        if result.schedule is not None
        else None
    )
    payload = {
        "status": result.status,
        "text": text,
        "requested_date": requested_date.isoformat(),
        "group": group,
        "cache_version": cache_version,
        "screenshot": screenshot,
        "screenshot_sha256": screenshot_sha256,
        "parity": result.schedule.parity if result.schedule is not None else None,
        "capture_id": capture_id,
        "checked_at": checked_at_value,
        "source_url": source_url,
        "crop_warning": crop_warning,
    }
    if result.status == "unavailable":
        payload["json_sha256"] = None
    else:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        payload["json_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def build_week_payload(
    schedules: dict[date, DaySchedule], checked_at: datetime, screenshot: Path,
    cache_version: str, screenshot_sha256: str, requested_date: date, *, stale: bool = False,
) -> dict[str, object]:
    if not schedules or GROUP not in {item.group for item in schedules.values()}:
        raise ValueError("Недельный результат не подтверждён")
    image = screenshot.read_bytes()
    if not image or hashlib.sha256(image).hexdigest() != screenshot_sha256:
        raise ValueError("Хэш скриншота не соответствует результату")
    monday = min(schedules)
    parity = schedules[monday].parity
    text = format_week_schedule(schedules, checked_at, parity=parity, stale=stale)
    payload = {
        "status": "stale" if stale else "fresh", "mode": "week", "text": text,
        "requested_date": requested_date.isoformat(), "group": GROUP,
        "cache_version": cache_version, "screenshot": str(screenshot),
        "screenshot_sha256": screenshot_sha256, "parity": parity,
        "capture_id": cache_version, "checked_at": checked_at.isoformat(),
        "source_url": build_canonical_url(requested_date, GROUP, parity),
        "crop_warning": False,
        "json_sha256": "",
        "week_dates": [day.isoformat() for day in sorted(schedules)],
    }
    payload["json_sha256"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return payload


def _schedule_from_snapshot(payload: dict[str, object], target: date) -> DaySchedule:
    return DaySchedule(
        group=str(payload["group"]),
        schedule_date=target,
        parity=str(payload["parity"]),
        lessons=tuple(
            Lesson(**{**item, "extra": tuple(item.get("extra", ()))})
            for item in payload["lessons"]
        ),
        empty_confirmed=bool(payload["empty_schedule_confirmed"]),
    )

def _result_from_snapshot(payload: dict[str, object], image: bytes, target: date, cache_root: Path) -> AcquisitionResult:
    schedule = _schedule_from_snapshot(payload, target)
    checked_at = datetime.fromisoformat(str(payload["checked_at"]))
    with NamedTemporaryFile(suffix=".png") as temp:
        temp.write(image)
        temp.flush()
        cached = CacheStore(cache_root).save(schedule, checked_at, Path(temp.name))
    age = datetime.now(checked_at.tzinfo) - checked_at
    return AcquisitionResult(
        # A remote GitHub publication is never the primary response path.  It
        # is explicitly marked stale because this branch is reached only after
        # the new live capture failed.
        status="stale",
        schedule=cached.schedule,
        checked_at=cached.checked_at,
        screenshot=cached.screenshot,
        cache_version=cached.version,
        screenshot_sha256=cached.screenshot_sha256,
    )

def _result_from_local_publication(target: date) -> AcquisitionResult:
    payload, _image, _fresh = load_latest(PUBLISHED_ROOT, target, max_age=PUBLISHED_MAX_AGE)
    return AcquisitionResult(
        status="stale",
        schedule=_schedule_from_snapshot(payload, target),
        checked_at=datetime.fromisoformat(str(payload["checked_at"])),
        screenshot=PUBLISHED_ROOT / target.isoformat() / "schedule.png",
        cache_version=str(payload["data_sha256"]),
        screenshot_sha256=str(payload["screenshot_sha256"]),
    )

def run(target: date, cache_root: Path, *, refresh: bool = False, day_crop: bool = False) -> dict[str, object]:
    cache = CacheStore(cache_root)
    # Every user request reaches the official source first.  Published GitHub
    # data and the local cache are strictly fallback paths for outages.
    try:
        with NamedTemporaryFile(suffix=".png") as temp, NamedTemporaryFile(suffix=".png") as day_temp:
            if day_crop:
                schedule, checked_at = capture_live(
                    target, GROUP, Path(temp.name), day_output=Path(day_temp.name)
                )
            else:
                schedule, checked_at = capture_live(target, GROUP, Path(temp.name))
            cropped = day_crop and Path(day_temp.name).stat().st_size > 0
            source_image = Path(day_temp.name) if cropped else Path(temp.name)
            cached = cache.save(schedule, checked_at, source_image)
        return build_payload(
            AcquisitionResult(
                status="fresh", schedule=cached.schedule, checked_at=cached.checked_at,
                screenshot=cached.screenshot, cache_version=cached.version,
                screenshot_sha256=cached.screenshot_sha256,
            ),
            target,
            crop_warning=day_crop and not cropped,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, RuntimeError):
        pass
    try:
        payload, image = load_remote_snapshot(target)
        return build_payload(_result_from_snapshot(payload, image, target, cache_root), target)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    try:
        return build_payload(_result_from_local_publication(target), target)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    cached = cache.load(target, GROUP)
    if cached is not None:
        result = AcquisitionResult(
            status="stale", schedule=cached.schedule, checked_at=cached.checked_at,
            screenshot=cached.screenshot, cache_version=cached.version,
            screenshot_sha256=cached.screenshot_sha256,
        )
        return build_payload(result, target)
    return build_payload(AcquisitionResult("unavailable", None, None, None), target)


def run_week(target: date, cache_root: Path) -> dict[str, object]:
    cache = CacheStore(cache_root)
    try:
        with NamedTemporaryFile(suffix=".png") as temp:
            week, checked_at = capture_live_week(target, GROUP, Path(temp.name))
            selected = week.get(target) or next(iter(week.values()))
            cached = cache.save(selected, checked_at, Path(temp.name))
        return build_week_payload(week, checked_at, cached.screenshot, cached.version, cached.screenshot_sha256, target)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        # GitHub publication/local cache remain an explicitly stale fallback.
        schedules: dict[date, DaySchedule] = {}
        checked_at: datetime | None = None
        cached_image = None
        cached_version = None
        cached_hash = None
        for offset in range(7):
            day = target + timedelta(days=offset - target.weekday())
            try:
                payload, image = load_remote_snapshot(day)
                result = _result_from_snapshot(payload, image, day, cache_root)
                if result.schedule is not None:
                    schedules[day] = result.schedule
                    checked_at = checked_at or result.checked_at
                    cached_image = cached_image or result.screenshot
                    cached_version = cached_version or result.cache_version
                    cached_hash = cached_hash or result.screenshot_sha256
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        if schedules and checked_at and cached_image and cached_version and cached_hash:
            return build_week_payload(schedules, checked_at, cached_image, cached_version, cached_hash, target, stale=True)
        return build_payload(AcquisitionResult("unavailable", None, None, None), target)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--cache-root", type=Path, default=PROJECT_ROOT / "runtime" / "cache")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--week", action="store_true")
    parser.add_argument("--day-crop", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = date.fromisoformat(args.date)
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")
    result = run_week(target, args.cache_root) if args.week else run(target, args.cache_root, refresh=args.refresh, day_crop=args.day_crop)
    print(json.dumps(result, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
