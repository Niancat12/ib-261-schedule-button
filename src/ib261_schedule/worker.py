from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from tempfile import NamedTemporaryFile
from pathlib import Path
from zoneinfo import ZoneInfo

from .cache import CacheStore
from .presentation import format_schedule
from .published import load_latest, load_remote_snapshot
from .schedule import DaySchedule, Lesson
from .service import AcquisitionResult
from .source import SOURCE_URL

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "published-schedules"
GROUP = "ИБ-261"
STATUSES = {"fresh", "stale", "unavailable"}
PUBLISHED_MAX_AGE = timedelta(hours=36)


def build_payload(
    result: AcquisitionResult, requested_date: date, group: str = GROUP
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

    return {
        "status": result.status,
        "text": text,
        "requested_date": requested_date.isoformat(),
        "group": group,
        "cache_version": cache_version,
        "screenshot": screenshot,
        "screenshot_sha256": screenshot_sha256,
    }


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
        status="fresh" if age <= PUBLISHED_MAX_AGE else "stale",
        schedule=cached.schedule,
        checked_at=cached.checked_at,
        screenshot=cached.screenshot,
        cache_version=cached.version,
        screenshot_sha256=cached.screenshot_sha256,
    )

def _result_from_local_publication(target: date) -> AcquisitionResult:
    payload, _image, fresh = load_latest(PUBLISHED_ROOT, target, max_age=PUBLISHED_MAX_AGE)
    return AcquisitionResult(
        status="fresh" if fresh else "stale",
        schedule=_schedule_from_snapshot(payload, target),
        checked_at=datetime.fromisoformat(str(payload["checked_at"])),
        screenshot=PUBLISHED_ROOT / target.isoformat() / "schedule.png",
        cache_version=str(payload["data_sha256"]),
        screenshot_sha256=str(payload["screenshot_sha256"]),
    )

def run(target: date, cache_root: Path, *, refresh: bool = False) -> dict[str, object]:
    cache = CacheStore(cache_root)
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

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--cache-root", type=Path, default=PROJECT_ROOT / "runtime" / "cache")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = date.fromisoformat(args.date)
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")
    print(json.dumps(run(target, args.cache_root, refresh=args.refresh), ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
