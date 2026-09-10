from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import ib261_schedule.published as published
from ib261_schedule.published import (
    load_latest,
    publish_snapshot,
    snapshot_payload,
    validate_snapshot,
)
from ib261_schedule.schedule import DaySchedule, Lesson

PNG = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364f80f00010501012718e366"
    "0000000049454e44ae426082"
)
PNG_2 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364f80f00010501012718e366"
    "000000027445587476325d91517c0000000049454e44ae426082"
)


def schedule(group="ИБ-261", day=date(2026, 9, 8)):
    return DaySchedule(
        group=group,
        schedule_date=day,
        parity="знаменатель",
        lessons=(Lesson("08:30-10:05", "Физика", "430/3", "Иванов", None, "лекция", (), False),),
    )


def test_rejects_group_date_and_empty_data():
    image = PNG
    checked = datetime(2026, 9, 8, 10, tzinfo=__import__("datetime").timezone.utc)
    payload = snapshot_payload(
        schedule(), checked, "https://cchgeu.ru/studentu/onlayn-raspisanie/", image
    )
    assert validate_snapshot(payload, image, target=date(2026, 9, 8))["group"] == "ИБ-261"
    for invalid in (
        {**payload, "group": "ИБ-262"},
        {**payload, "schedule_date": "2026-09-09"},
        {**payload, "lessons": []},
    ):
        with pytest.raises(ValueError):
            validate_snapshot(invalid, image, target=date(2026, 9, 8))


def test_atomic_publish_changes_only_when_hash_changes(tmp_path):
    checked = datetime(2026, 9, 8, 10, tzinfo=__import__("datetime").timezone.utc)
    root = tmp_path / "published"
    assert publish_snapshot(
        root, schedule(), checked, "https://cchgeu.ru/studentu/onlayn-raspisanie/", PNG
    )
    assert not publish_snapshot(
        root, schedule(), checked, "https://cchgeu.ru/studentu/onlayn-raspisanie/", PNG
    )
    assert publish_snapshot(
        root,
        schedule(),
        checked + timedelta(seconds=1),
        "https://cchgeu.ru/studentu/onlayn-raspisanie/",
        PNG,
    )
    payload, image, fresh = load_latest(
        root, date(2026, 9, 8), max_age=timedelta(hours=1), now=checked + timedelta(minutes=5)
    )
    assert payload["group"] == "ИБ-261" and image == PNG and fresh


def test_old_snapshot_is_marked_stale(tmp_path):
    checked = datetime(2026, 9, 8, 10, tzinfo=__import__("datetime").timezone.utc)
    root = tmp_path / "published"
    publish_snapshot(
        root, schedule(), checked, "https://cchgeu.ru/studentu/onlayn-raspisanie/", PNG
    )
    _, _, fresh = load_latest(
        root, date(2026, 9, 8), max_age=timedelta(hours=1), now=checked + timedelta(hours=2)
    )
    assert not fresh


def test_atomic_publish_rolls_back_when_second_file_replace_fails(tmp_path, monkeypatch):
    checked = datetime(2026, 9, 8, 10, tzinfo=__import__("datetime").timezone.utc)
    root = tmp_path / "published"
    publish_snapshot(
        root, schedule(), checked, "https://cchgeu.ru/studentu/onlayn-raspisanie/", PNG
    )
    day_dir = root / "2026-09-08"
    old_json = (day_dir / "schedule.json").read_bytes()
    old_png = (day_dir / "schedule.png").read_bytes()
    original_replace = published.os.replace
    failed = False

    def fail_png(source, destination):
        nonlocal failed
        if not failed and Path(destination).name == "schedule.png" and Path(source).parent != day_dir:
            failed = True
            raise OSError("simulated publication failure")
        return original_replace(source, destination)

    monkeypatch.setattr(published.os, "replace", fail_png)
    with pytest.raises(OSError, match="simulated"):
        publish_snapshot(
            root,
            schedule(),
            checked,
            "https://cchgeu.ru/studentu/onlayn-raspisanie/",
            PNG_2,
        )
    assert (day_dir / "schedule.json").read_bytes() == old_json
    assert (day_dir / "schedule.png").read_bytes() == old_png
