from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from .schedule import DaySchedule, Lesson

GROUP = "ИБ-261"


@dataclass(frozen=True)
class CachedSnapshot:
    schedule: DaySchedule
    checked_at: datetime
    screenshot: Path
    metadata: Path
    version: str
    screenshot_sha256: str


class CacheStore:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()

    @staticmethod
    def _validate_group(group: str) -> None:
        if group != GROUP:
            raise ValueError("Кэш поддерживает только точную группу ИБ-261")

    def _contained(self, path: Path) -> Path:
        candidate = Path(path).absolute()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Путь кэша выходит за корневой каталог") from exc
        return candidate

    def _reject_symlink_components(self, path: Path) -> None:
        candidate = self._contained(path)
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current /= part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise ValueError("Путь кэша содержит символическую ссылку")
            except FileNotFoundError:
                continue

    def _day_root(self, schedule_date: date, group: str) -> Path:
        self._validate_group(group)
        path = self._contained(self.root / GROUP / schedule_date.isoformat())
        self._reject_symlink_components(path)
        return path

    def _read_file(self, path: Path) -> bytes:
        candidate = self._contained(path)
        relative = candidate.relative_to(self.root)
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = [root_fd]
        try:
            parent_fd = root_fd
            for part in relative.parts[:-1]:
                parent_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                opened.append(parent_fd)
            file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            opened.append(file_fd)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise ValueError("Файл кэша отсутствует или пуст")
            chunks: list[bytes] = []
            while chunk := os.read(file_fd, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    @staticmethod
    def _read_input_screenshot(path: Path) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise ValueError("Скриншот отсутствует или пуст")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError as exc:
            raise ValueError("Скриншот отсутствует или небезопасен") from exc
        finally:
            os.close(descriptor)

    def save(self, schedule: DaySchedule, checked_at: datetime, screenshot: Path) -> CachedSnapshot:
        self._validate_group(schedule.group)
        image = self._read_input_screenshot(screenshot)
        day_root = self._day_root(schedule.schedule_date, schedule.group)
        day_root.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_components(day_root)
        version = f"{checked_at.strftime('%Y%m%dT%H%M%S%z')}-{uuid4().hex[:8]}"
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=day_root))
        self._reject_symlink_components(stage)
        try:
            image_target = stage / "schedule.png"
            image_target.write_bytes(image)
            digest = hashlib.sha256(image).hexdigest()
            payload = {
                "group": schedule.group,
                "schedule_date": schedule.schedule_date.isoformat(),
                "parity": schedule.parity,
                "lessons": [asdict(lesson) for lesson in schedule.lessons],
                "empty_schedule_confirmed": schedule.empty_confirmed,
                "checked_at": checked_at.isoformat(),
                "screenshot_sha256": digest,
            }
            metadata = stage / "metadata.json"
            metadata.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            final = day_root / version
            os.replace(stage, final)
            pointer_tmp = day_root / f".latest-{uuid4().hex}.json"
            pointer_tmp.write_text(json.dumps({"version": version}), encoding="utf-8")
            os.replace(pointer_tmp, day_root / "latest.json")
            return self._load_version(final)
        finally:
            if stage.exists():
                for child in stage.iterdir():
                    child.unlink()
                stage.rmdir()

    def load(self, schedule_date: date, group: str) -> CachedSnapshot | None:
        self._validate_group(group)
        try:
            day_root = self._day_root(schedule_date, group)
            pointer = json.loads(self._read_file(day_root / "latest.json").decode("utf-8"))
            version = pointer["version"]
            if (
                not isinstance(version, str)
                or Path(version).name != version
                or version.startswith(".")
            ):
                return None
            loaded = self._load_version(day_root / version)
        except (OSError, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if loaded.schedule.schedule_date != schedule_date or loaded.schedule.group != group:
            return None
        return loaded

    def _load_version(self, version_dir: Path) -> CachedSnapshot:
        version_dir = self._contained(version_dir)
        self._reject_symlink_components(version_dir)
        metadata = version_dir / "metadata.json"
        screenshot = version_dir / "schedule.png"
        payload = json.loads(self._read_file(metadata).decode("utf-8"))
        image = self._read_file(screenshot)
        actual_hash = hashlib.sha256(image).hexdigest()
        if actual_hash != payload["screenshot_sha256"]:
            raise ValueError("Хэш скриншота кэша не совпадает")
        lessons = tuple(
            Lesson(**{**item, "extra": tuple(item.get("extra", ()))}) for item in payload["lessons"]
        )
        schedule = DaySchedule(
            group=payload["group"],
            schedule_date=date.fromisoformat(payload["schedule_date"]),
            parity=payload["parity"],
            lessons=lessons,
            empty_confirmed=bool(payload.get("empty_schedule_confirmed", False)),
        )
        return CachedSnapshot(
            schedule=schedule,
            checked_at=datetime.fromisoformat(payload["checked_at"]),
            screenshot=screenshot,
            metadata=metadata,
            version=version_dir.name,
            screenshot_sha256=actual_hash,
        )
