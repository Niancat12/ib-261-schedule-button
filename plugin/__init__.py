from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import signal
import struct
import sys
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .controller import Action, ButtonController
from ib261_schedule.presentation import format_day_heading

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Run the capture worker in the same interpreter that loaded the Hermes
# plugin.  This prevents the gateway from silently executing a stale project
# virtualenv while the active plugin imports a different editable checkout.
WORKER_PYTHON = Path(sys.executable)
CACHE_ROOT = PROJECT_ROOT / "runtime" / "cache"
MOSCOW = ZoneInfo("Europe/Moscow")
GROUP = "ИБ-261"
BUTTON = "📅 Расписание ИБ-261"
MENU_COMMAND = "/schedule"
MENU_TEXTS = {"Сегодня", "Завтра", "Неделя", "Обновить"}
CALLBACK_TEXT = {
    "ib261:yesterday": "⬅️ Вчера",
    "ib261:today": "Сегодня",
    "ib261:tomorrow": "Завтра",
    "ib261:week": "Неделя",
    "ib261:refresh": "Обновить",
    "ib261:date": "📅 Другая дата",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class TelegramSchedulePayload:
    """Immutable presentation result shared by both Telegram sends."""

    view_mode: str
    requested_date: date
    capture_id: str
    checked_at: datetime
    text: str
    photo_path: Path
    photo_bytes: bytes
    photo_sha256: str
    photo_width: int
    photo_height: int
    photo_kind: str
    caption: str
    keyboard: Any


def _png_dimensions(image: bytes) -> tuple[int, int]:
    if not image.startswith(PNG_SIGNATURE) or len(image) < 24:
        raise ValueError("invalid PNG")
    width, height = struct.unpack(">II", image[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("invalid PNG dimensions")
    return width, height


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
    for key in ("HOME", "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "SCHEDULE_PROXY_URL"):
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
            if not data or not data.startswith(PNG_SIGNATURE):
                raise ValueError("screenshot is not a PNG")
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
        "parity",
        "capture_id",
        "checked_at",
        "source_url",
        "json_sha256",
        "crop_warning",
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
            for field in ("cache_version", "screenshot", "screenshot_sha256", "parity", "capture_id", "checked_at", "source_url", "json_sha256")
        ):
            raise ValueError("unavailable payload contains data")
        return payload
    if not isinstance(payload["crop_warning"], bool):
        raise ValueError("invalid crop warning")
    if not all(
        isinstance(payload[field], str) and payload[field]
        for field in ("cache_version", "screenshot", "screenshot_sha256", "parity", "capture_id", "checked_at", "source_url", "json_sha256")
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
    if not isinstance(payload["checked_at"], str) or not payload["checked_at"]:
        raise ValueError("missing capture timestamp")
    if not isinstance(payload["capture_id"], str) or not payload["capture_id"]:
        raise ValueError("missing capture id")
    if not isinstance(payload["source_url"], str) or not payload["source_url"].startswith("https://cchgeu.ru/"):
        raise ValueError("untrusted source URL")
    return payload


def _validate_week_payload(payload: Any, requested: date) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("mode") != "week":
        raise ValueError("weekly payload schema mismatch")
    for field in ("text", "group", "parity", "capture_id", "checked_at", "source_url", "screenshot", "screenshot_sha256", "json_sha256"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError("weekly payload is incomplete")
    if payload["group"] != GROUP or not isinstance(payload.get("week_dates"), list) or not payload["week_dates"]:
        raise ValueError("weekly payload metadata mismatch")
    if payload.get("requested_date") != requested.isoformat():
        raise ValueError("weekly requested date mismatch")
    path = Path(payload["screenshot"]).resolve()
    if not path.is_file():
        raise ValueError("weekly screenshot unavailable")
    image = path.read_bytes()
    if not image.startswith(PNG_SIGNATURE) or hashlib.sha256(image).hexdigest() != payload["screenshot_sha256"]:
        raise ValueError("weekly screenshot hash mismatch")
    return payload


class _RequestGate:
    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str, str | None], int] = {}
        self._valid_tokens: set[tuple[tuple[str, str, str | None], int]] = set()
        self._counter = 0
        self._tasks: dict[tuple[str, str, str | None], Any] = {}

    def begin(self, key: tuple[str, str, str | None], *, cancel_previous: bool = True) -> int:
        if cancel_previous and key in self._tokens:
            self._valid_tokens.discard((key, self._tokens[key]))
        old = self._tasks.pop(key, None) if cancel_previous else None
        if old is not None:
            old.cancel()
        self._counter += 1
        self._tokens[key] = self._counter
        self._valid_tokens.add((key, self._counter))
        return self._counter

    def attach(self, key: tuple[str, str, str | None], token: int, task: Any) -> None:
        if self.is_current(key, token):
            self._tasks[key] = task

    def is_current(self, key: tuple[str, str, str | None], token: int) -> bool:
        return (key, token) in self._valid_tokens


async def _run_worker(target: date, timeout: float = 35.0, *, week: bool = False, day_crop: bool = False) -> dict[str, Any]:
    args = [
        str(WORKER_PYTHON), "-m", "ib261_schedule.worker", "--date", target.isoformat(),
        "--cache-root", str(CACHE_ROOT),
    ]
    if week:
        args.append("--week")
    elif day_crop:
        args.append("--day-crop")
    process = await asyncio.create_subprocess_exec(
        *args,
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
            "parity": None,
            "capture_id": None,
            "checked_at": None,
            "source_url": None,
            "json_sha256": None,
            "crop_warning": False,
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
            "parity": None,
            "capture_id": None,
            "checked_at": None,
            "source_url": None,
            "json_sha256": None,
            "crop_warning": False,
        }
    try:
        payload = json.loads(stdout.decode())
        if week:
            _validate_week_payload(payload, target)
            return payload
        return _validate_worker_payload(payload, target)
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
            "parity": None,
            "capture_id": None,
            "checked_at": None,
            "source_url": None,
            "json_sha256": None,
            "crop_warning": False,
        }


def _build_telegram_payload(
    payload: dict[str, Any], *, mode: str, keyboard: Any = None,
) -> TelegramSchedulePayload:
    screenshot = payload.get("screenshot")
    if not screenshot:
        raise ValueError("schedule payload has no screenshot")
    path = Path(str(screenshot)).resolve()
    with _open_verified_screenshot(CACHE_ROOT, path, str(payload["screenshot_sha256"])) as stream:
        image = stream.read()
    width, height = _png_dimensions(image)
    requested_date = date.fromisoformat(str(payload["requested_date"]))
    checked_at = datetime.fromisoformat(str(payload["checked_at"])).astimezone(MOSCOW)
    parity = str(payload["parity"]).capitalize()
    if mode == "week":
        monday = requested_date - timedelta(days=requested_date.weekday())
        sunday = monday + timedelta(days=6)
        months = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
        span = (f"{monday.day}–{sunday.day} {months[sunday.month]} {sunday.year}"
                if monday.month == sunday.month else
                f"{monday.day} {months[monday.month]}–{sunday.day} {months[sunday.month]} {sunday.year}")
        caption = f"📅 ИБ-261\nНеделя: {span}\n{parity}\nПроверено: {checked_at:%H:%M:%S} МСК"
        photo_kind = "full_week"
    else:
        caption = (f"📅 ИБ-261\n{format_day_heading(requested_date)}\n"
                   f"{parity}\nПроверено: {checked_at:%H:%M:%S} МСК")
        photo_kind = "full_week" if payload.get("crop_warning") or payload.get("status") == "stale" else "daily_crop"
        if payload.get("crop_warning"):
            caption += "\nНе удалось выделить день — показана вся неделя"
    outbound = TelegramSchedulePayload(
        view_mode=mode,
        requested_date=requested_date,
        capture_id=str(payload["capture_id"]),
        checked_at=checked_at,
        text=str(payload["text"]),
        photo_path=path,
        photo_bytes=image,
        photo_sha256=hashlib.sha256(image).hexdigest(),
        photo_width=width,
        photo_height=height,
        photo_kind=photo_kind,
        caption=caption,
        keyboard=keyboard,
    )
    if outbound.photo_sha256 != str(payload["screenshot_sha256"]):
        raise ValueError("outbound photo hash mismatch")
    if mode == "week":
        if not outbound.text.startswith("📅 Расписание ИБ-261 на неделю"):
            raise ValueError("weekly text is not the current formatter output")
    else:
        heading_lines = outbound.text.splitlines()[:5]
        heading_parts = format_day_heading(requested_date).split(", ")
        expected_weekday = heading_parts[1] if len(heading_parts) > 2 else heading_parts[0]
        expected_date = f"{requested_date.day} "
        if not any(
            GROUP in line and expected_weekday in line and expected_date in line
            for line in heading_lines
        ):
            raise ValueError("daily text is not the current formatter output")
    return outbound


def _safe_text_fallback(payload: dict[str, Any], target: date, mode: str) -> str:
    """Return text only when it carries the current canonical heading."""
    text = payload.get("text")
    if not isinstance(text, str):
        return "⚠️ Расписание не удалось подготовить."
    if mode == "week":
        valid = text.startswith("📅 Расписание ИБ-261 на неделю")
    else:
        heading_parts = format_day_heading(target).split(", ")
        expected_weekday = heading_parts[1] if len(heading_parts) > 2 else heading_parts[0]
        expected_date = f"{target.day} "
        valid = any(
            GROUP in line and expected_weekday in line and expected_date in line
            for line in text.splitlines()[:5]
        )
    return text if valid else "⚠️ Расписание не удалось подготовить."


async def _send_result(
    bot: Any,
    adapter: Any,
    chat_id: str,
    payload: dict[str, Any],
    target: date,
    key: tuple[str, str, str | None],
    controller: ButtonController,
    token: int,
    gate: _RequestGate,
    *,
    mode: str = "day",
) -> None:
    photo_sent = False
    outbound: TelegramSchedulePayload | None = None
    try:
        outbound = _build_telegram_payload(payload, mode=mode, keyboard=_inline_keyboard())
        logger.info(
            "schedule outbound handler=_send_result view_mode=%s requested_date=%s capture_id=%s "
            "first_line=%r photo_sha256=%s photo_size=%sx%s photo_source=%s callback_query_id=%s",
            outbound.view_mode, outbound.requested_date.isoformat(), outbound.capture_id,
            outbound.text.splitlines()[0] if outbound.text else "", outbound.photo_sha256,
            outbound.photo_width, outbound.photo_height, outbound.photo_kind,
            payload.get("callback_query_id", "-"),
        )
        sender = getattr(adapter, "send_image_file", None)
        if not callable(sender):
            raise ValueError("Hermes Telegram media API is unavailable")
        result = await sender(
            chat_id=chat_id,
            image_path=str(outbound.photo_path),
            caption=outbound.caption,
            metadata={
                "schedule_capture_id": outbound.capture_id,
                "schedule_photo_sha256": outbound.photo_sha256,
                "schedule_photo_width": outbound.photo_width,
                "schedule_photo_height": outbound.photo_height,
                "schedule_photo_kind": outbound.photo_kind,
            },
        )
        telegram_sizes = getattr(result, "photo_sizes", None)
        logger.info(
            "schedule sendPhoto result success=%s capture_id=%s photo_sha256=%s photo_size=%sx%s telegram_photo_sizes=%s",
            getattr(result, "success", True), outbound.capture_id, outbound.photo_sha256,
            outbound.photo_width, outbound.photo_height, telegram_sizes or "unreported",
        )
        if getattr(result, "success", True) is False:
            raise ValueError(getattr(result, "error", "media upload failed"))
        photo_sent = True
    except Exception as exc:
        logger.warning("Verified schedule screenshot could not be sent (%s)", type(exc).__name__)
    if not photo_sent:
        await bot.send_message(chat_id=chat_id, text="Не удалось загрузить изображение расписания")
    if outbound is None:
        # Keep text delivery available when media validation fails.
        text = _safe_text_fallback(payload, target, mode)
        keyboard = _inline_keyboard()
    else:
        text, keyboard = outbound.text, outbound.keyboard
        logger.info(
            "schedule sendMessage capture_id=%s first_line=%r photo_sha256=%s",
            outbound.capture_id, text.splitlines()[0] if text else "", outbound.photo_sha256,
        )
    await bot.send_message(
        chat_id=chat_id, text=text, reply_markup=keyboard,
    )
    if gate.is_current(key, token):
        controller.mark_displayed(key, target, mode)


def _inline_keyboard() -> Any:
    """Build the callback keyboard lazily so registration stays SDK-optional."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ Вчера", callback_data="ib261:yesterday"),
                InlineKeyboardButton("Сегодня", callback_data="ib261:today"),
                InlineKeyboardButton("Завтра ➡️", callback_data="ib261:tomorrow"),
            ],
            [
                InlineKeyboardButton("Неделя", callback_data="ib261:week"),
                InlineKeyboardButton("🔄 Обновить", callback_data="ib261:refresh"),
            ],
            [InlineKeyboardButton("📅 Другая дата", callback_data="ib261:date")],
        ]
    )


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
    adapters: dict[str, Any] = {}
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

    def _start_action(key: tuple[str, str, str | None], action: Action, callback_query_id: str = "") -> None:
        token = gate.begin(key, cancel_previous=False)
        task = asyncio.create_task(_deliver(key, action, token, callback_query_id))
        gate.attach(key, token, task)

    async def _deliver(key: tuple[str, str, str | None], action: Action, token: int, callback_query_id: str = "") -> None:
        target = action.target or _moscow_date()
        bot = bots.get(key[1])
        if action.kind == "week" or action.mode == "week":
            if not bot or not gate.is_current(key, token):
                return
            payload = await _run_worker(target, week=True)
            try:
                _validate_week_payload(payload, target)
            except ValueError:
                return
            if gate.is_current(key, token):
                payload["callback_query_id"] = callback_query_id
                await _send_result(bot, adapters.get(key[1]), payload, target, key, controller, token, gate, mode="week")
            if gate.is_current(key, token):
                controller.mark_displayed(key, target, "week")
            return
        payload = await _run_worker(target, day_crop=True)
        try:
            _validate_worker_payload(payload, target)
        except ValueError:
            return
        if bot and gate.is_current(key, token):
            payload["callback_query_id"] = callback_query_id
            await _send_result(
                bot,
                adapters.get(key[1]),
                payload,
                target,
                key,
                controller,
                token,
                gate,
                mode="day",
            )

    def hook(event: Any, **kwargs: Any) -> dict[str, str] | None:
        if (_event_chat_id(event) != target_chat or getattr(getattr(event, "source", event), "platform", "telegram") != "telegram"):
            return None
        text = getattr(event, "text", "")
        if text in {BUTTON, MENU_COMMAND, "Сегодня", "Завтра", "Неделя", "Обновить", "⬅️ Вчера", "➡️ Завтра", "📆 Другая дата", "📅 Другая дата", "/ib261"} or controller._awaiting_date.__contains__(_key(event)):
            ctx.spawn_task(handle(event))
            return {"action": "skip", "reason": "ib261-button"}
        return None

    def factory(application: Any, adapter: Any = None) -> None:
        bots[target_chat] = application.bot
        adapters[target_chat] = adapter
        try:
            from telegram.ext import CallbackQueryHandler

            async def _on_callback(update: Any, _context: Any) -> None:
                query = update.callback_query
                if query is None or query.message is None:
                    return
                await query.answer("Получаю свежее расписание…")
                chat = getattr(query.message, "chat", None)
                chat_id = str(getattr(chat, "id", getattr(query.message, "chat_id", "")))
                if chat_id != target_chat:
                    return
                callback_id = str(getattr(query, "id", ""))
                seen = getattr(_on_callback, "_seen", set())
                if callback_id and callback_id in seen:
                    return
                if callback_id:
                    seen.add(callback_id)
                    _on_callback._seen = seen
                text = CALLBACK_TEXT.get(str(query.data))
                if text is None:
                    return
                key = ("telegram", chat_id, None)
                action = controller.handle(text, key, _moscow_date())
                if action is not None:
                    if action.kind == "ask_date":
                        await bots[chat_id].send_message(
                            chat_id=chat_id,
                            text="Введите дату в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД",
                        )
                    else:
                        _start_action(key, action, callback_id)

            application.add_handler(CallbackQueryHandler(_on_callback, pattern=r"^ib261:"))
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
