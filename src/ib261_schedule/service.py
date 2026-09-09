from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from .cache import CacheStore
from .schedule import DaySchedule

CaptureFunction = Callable[[date, str, Path], tuple[DaySchedule, datetime]]


@dataclass(frozen=True)
class AcquisitionResult:
    status: Literal["fresh", "stale", "unavailable"]
    schedule: DaySchedule | None
    checked_at: datetime | None
    screenshot: Path | None
    error: str | None = None
    cache_version: str | None = None
    screenshot_sha256: str | None = None


class ScheduleService:
    def __init__(
        self,
        cache: CacheStore,
        capture: CaptureFunction,
        *,
        group: str = "ИБ-261",
    ):
        self.cache = cache
        self.capture = capture
        self.group = group

    def get(self, target: date) -> AcquisitionResult:
        try:
            with tempfile.TemporaryDirectory(prefix="ib261-capture-") as tmp:
                output = Path(tmp) / "schedule.png"
                schedule, checked_at = self.capture(target, self.group, output)
                if schedule.group != self.group or schedule.schedule_date != target:
                    raise ValueError("Результат источника не соответствует запросу")
                cached = self.cache.save(schedule, checked_at, output)
            return AcquisitionResult(
                status="fresh",
                schedule=cached.schedule,
                checked_at=cached.checked_at,
                screenshot=cached.screenshot,
                cache_version=cached.version,
                screenshot_sha256=cached.screenshot_sha256,
            )
        except Exception:
            cached = self.cache.load(target, self.group)
            if cached is not None:
                return AcquisitionResult(
                    status="stale",
                    schedule=cached.schedule,
                    checked_at=cached.checked_at,
                    screenshot=cached.screenshot,
                    error="Источник расписания сейчас недоступен",
                    cache_version=cached.version,
                    screenshot_sha256=cached.screenshot_sha256,
                )
            return AcquisitionResult(
                status="unavailable",
                schedule=None,
                checked_at=None,
                screenshot=None,
                error="Источник расписания сейчас недоступен",
            )
