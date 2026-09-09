from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import signal
from contextlib import contextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .controller import Action, ButtonController

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKER_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
CACHE_ROOT = PROJECT_ROOT / "runtime" / "cache"
MOSCOW = ZoneInfo("Europe/Moscow")
GROUP = "ИБ-261"
BUTTON = "📅 Расписание ИБ-261"
MENU_COMMAND = "/schedule"
MENU_TEXTS = {"Сегодня", "Завтра", "Неделя", "Обновить"}


def _moscow_date(instant: datetime | None = None) -> date:
    value = instant or datetime.now(MOSCOW)
    if value.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    return value.astimezone(MOSCOW).date()


def _worker_env() -> dict[str, str]:
    env = {
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "TZ": "Europe/Moscow",
    }
    for key in ("HOME", "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _key(event: Any) -> tuple[str, str, str | None]:
    source = getattr(event, "source", event)
    return (
        str(getattr(source, "platform", "telegram")),
        str(getattr(source, "chat_id", "")),
        getattr(source, "thread_id", None),
    )


def _event_chat_id(event: Any) -> str:
    source = getattr(event, "source", event)
    return str(getattr(source, "chat_id", ""))


@contextmanager
def _open_verified_screenshot(root: Path, path: Path, digest: str):
    resolved_root = root.resolve()
    resolved = path.resolve()
    allowed_roots = (resolved_root, (PROJECT_ROOT / "published-schedules").resolve())
    if not any(root == resolved or root in resolved.parents for root in allowed_roots) or not resolved.is_file():
        raise ValueError("screenshot is outside trusted roots")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("screenshot hash mismatch")
            yield io.BytesIO(data)
    except OSError as exc:
        raise ValueError("screenshot is unavailable") from exc


def _validate_worker_payload(payload: Any, requested: date) -> dict[str, Any]:
    required = {
        "status",
        "text",
        "requested_date",
        "group",
        "cache_version",
        "screenshot",
        "screenshot_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("worker payload schema mismatch")
    if payload["status"] not in {"fresh", "stale", "unavailable"}:
        raise ValueError("invalid worker status")
    if (
        not isinstance(payload["text"], str)
        or payload["requested_date"] != requested.isoformat()
        or payload["group"] != GROUP
    ):
        raise ValueError("worker metadata mismatch")
    if payload["status"] == "unavailable":
        if any(
            payload[field] is not None
            for field in ("cache_version", "screenshot", "screenshot_sha256")
        ):
            raise ValueError("unavailable payload contains data")
        return payload
    if not all(
        isinstance(payload[field], str) and payload[field]
        for field in ("cache_version", "screenshot", "screenshot_sha256")
    ):
        raise ValueError("complete worker payload requires screenshot metadata")
    path = Path(payload["screenshot"]).resolve()
    allowed_roots = (CACHE_ROOT.resolve(), (PROJECT_ROOT / "published-schedules").resolve())
    if not any(root == path or root in path.parents for root in allowed_roots) or not path.is_file():
        raise ValueError("screenshot is outside trusted roots")
    with path.open("rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    if digest != payload["screenshot_sha256"]:
        raise ValueError("screenshot hash mismatch")
    return payload


class _RequestGate:
    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str, str | None], int] = {}
        self._counter = 0
        self._tasks: dict[tuple[str, str, str | None], Any] = {}

    def begin(self, key: tuple[str, str, str | None]) -> int:
        old = self._tasks.pop(key, None)
        if old is not None:
            old.cancel()
        self._counter += 1
        self._tokens[key] = self._counter
        return self._counter

    def attach(self, key: tuple[str, str, str | None], token: int, task: Any) -> None:
        if self.is_current(key, token):
            self._tasks[key] = task

    def is_current(self, key: tuple[str, str, str | None], token: int) -> bool:
        return self._tokens.get(key) == token


async def _run_worker(target: date, timeout: float = 35.0) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        str(WORKER_PYTHON),
        "-m",
        "ib261_schedule.worker",
        "--date",
        target.isoformat(),
        "--cache-root",
        str(CACHE_ROOT),
        cwd=str(PROJECT_ROOT),
        env=_worker_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        with suppress(Exception):
            await process.wait()
        if asyncio.current_task() and asyncio.current_task().cancelled():
            raise
        return {
            "status": "unavailable",
            "text": "⚠️ Источник расписания недоступен.",
            "requested_date": target.isoformat(),
            "group": GROUP,
            "cache_version": None,
            "screenshot": None,
            "screenshot_sha256": None,
        }
    if process.returncode != 0:
        return {
            "status": "unavailable",
            "text": "⚠️ Источник расписания недоступен.",
            "requested_date": target.isoformat(),
            "group": GROUP,
            "cache_version": None,
            "screenshot": None,
            "screenshot_sha256": None,
        }
    try:
        return _validate_worker_payload(json.loads(stdout.decode()), target)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Invalid schedule worker payload: %s", exc)
        return {
            "status": "unavailable",
            "text": "⚠️ Получен непроверенный ответ расписания.",
            "requested_date": target.isoformat(),
            "group": GROUP,
            "cache_version": None,
            "screenshot": None,
            "screenshot_sha256": None,
        }


async def _send_result(
    bot: Any,
    chat_id: str,
    payload: dict[str, Any],
    target: date,
    key: tuple[str, str, str | None],
    controller: ButtonController,
    token: int,
    gate: _RequestGate,
) -> None:
    await bot.send_message(chat_id=chat_id, text=payload["text"])
    screenshot = payload.get("screenshot")
    if screenshot:
        try:
            with _open_verified_screenshot(CACHE_ROOT, Path(screenshot), payload["screenshot_sha256"]) as image:
                await bot.send_photo(chat_id=chat_id, photo=image)
        except (OSError, ValueError):
            logger.warning("Verified schedule screenshot could not be sent")
    if gate.is_current(key, token):
        controller.mark_displayed(key, target)


def parse_output(output: str) -> dict[str, Any]:
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError("Malformed JSON") from exc


def display_schedule(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "No schedule available"
    schedule = payload.get("schedule", {})
    return "\n".join(str(value) for value in schedule.values())


def register(ctx: Any) -> None:
    controller = ButtonController()
    gate = _RequestGate()
    bots: dict[str, Any] = {}
    target_chat = str(ctx.get_config("chat_id", "")).strip()

    async def handle(event: Any) -> None:
        key = _key(event)
        action = controller.handle(getattr(event, "text", ""), key, _moscow_date())
        if action is None:
            return
        bot = bots.get(key[1])
        if action.kind == "menu":
            if bot:
                await bot.send_message(chat_id=key[1], text="Выберите: Сегодня, Завтра, Неделя или Обновить.")
            return
        if action.kind == "keyboard":
            return
        if action.kind == "ask_date":
            if bot:
                await bot.send_message(chat_id=key[1], text="Введите дату в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД")
            return
        if action.kind == "invalid_date":
            if bot:
                await bot.send_message(chat_id=key[1], text="Некорректная дата")
            return
        token = gate.begin(key)
        task = asyncio.create_task(_deliver(key, action, token))
        gate.attach(key, token, task)

    async def _deliver(key: tuple[str, str, str | None], action: Action, token: int) -> None:
        target = action.target or _moscow_date()
        bot = bots.get(key[1])
        if action.kind == "week":
            if not bot or not gate.is_current(key, token):
                return
            for offset in range(7):
                week_target = target.fromordinal(target.toordinal() + offset)
                payload = await _run_worker(week_target)
                try:
                    _validate_worker_payload(payload, week_target)
                except ValueError:
                    continue
                if gate.is_current(key, token):
                    await bot.send_message(chat_id=key[1], text=payload["text"])
            if gate.is_current(key, token):
                controller.mark_displayed(key, target)
            return
        payload = await _run_worker(target)
        try:
            _validate_worker_payload(payload, target)
        except ValueError:
            return
        if bot and gate.is_current(key, token):
            await _send_result(bot, key[1], payload, target, key, controller, token, gate)

    def hook(event: Any, **kwargs: Any) -> dict[str, str] | None:
        if (_event_chat_id(event) != target_chat or getattr(getattr(event, "source", event), "platform", "telegram") != "telegram"):
            return None
        text = getattr(event, "text", "")
        if text in {BUTTON, MENU_COMMAND, "Сегодня", "Завтра", "Неделя", "Обновить", "⬅️ Вчера", "➡️ Завтра", "📆 Другая дата", "/ib261"} or controller._awaiting_date.__contains__(_key(event)):
            ctx.spawn_task(handle(event))
            return {"action": "skip", "reason": "ib261-button"}
        return None

    def factory(application: Any, _adapter: Any = None) -> None:
        bots[target_chat] = application.bot
        try:
            from telegram import ReplyKeyboardMarkup
            markup = ReplyKeyboardMarkup(
                [[BUTTON], ["Сегодня", "Завтра"], ["Неделя", "Обновить"], ["⬅️ Вчера", "➡️ Завтра"], ["📆 Другая дата"]],
                resize_keyboard=True,
                is_persistent=True,
            )
            asyncio.create_task(application.bot.send_message(chat_id=int(target_chat), text="Меню расписания ИБ-261 готово.", reply_markup=markup))
        except Exception:
            logger.exception("Unable to install schedule keyboard")

    ctx.register_hook("pre_gateway_dispatch", hook)
    ctx.register_platform_handler("telegram", factory)
    ctx.register_command("ib261", lambda *_args, **_kwargs: None)
    ctx.register_command("schedule", lambda *_args, **_kwargs: None)


__all__ = [
    "register",
    "parse_output",
    "display_schedule",
    "_validate_worker_payload",
    "_run_worker",
    "_RequestGate",
    "_moscow_date",
    "_worker_env",
]
