from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
import time
import zlib
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from unicodedata import normalize
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .schedule import (
    DaySchedule,
    ScheduleParseError,
    parse_schedule_html,
    parse_week_schedule_html,
    week_monday,
)
from .source import SOURCE_URL, build_canonical_url, build_source_url

UrlBuilder = Callable[[date, str], str]
_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_ERROR_MARKERS = (
    "captcha",
    "cloudflare",
    "access denied",
    "forbidden",
    "not found",
    "страница не найдена",
    "ошибка сервера",
    "авторизац",
    "войдите",
)


class SourceUnavailable(RuntimeError):
    pass


class SourceIntegrityError(ScheduleParseError):
    """The response loaded but cannot be proven to be the requested schedule."""


def _playwright_proxy(value: str) -> dict[str, str]:
    """Convert the protected proxy URL into Playwright's credential fields."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise ValueError("SCHEDULE_PROXY_URL has an invalid proxy address")
    result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username is not None:
        result["username"] = unquote(parsed.username)
    if parsed.password is not None:
        result["password"] = unquote(parsed.password)
    return result


def _normalized_segment(value: str) -> str:
    return normalize("NFC", unquote(value)).strip()


def _url_segments(value: str) -> list[str]:
    return [_normalized_segment(part) for part in urlsplit(value).path.split("/") if part]


def _validate_canonical_url(
    final_url: str,
    requested_url: str,
    target: date,
    group: str,
    parity: str,
) -> list[str]:
    requested = urlsplit(requested_url)
    final = urlsplit(final_url)
    if final.scheme != requested.scheme or final.netloc.casefold() != requested.netloc.casefold():
        raise SourceIntegrityError("Источник перенаправил на другой адрес")
    actual = _url_segments(final_url)
    expected = [_normalized_segment(group), target.isoformat(), _normalized_segment(parity)]
    if len(actual) < 3 or actual[-3:] != expected:
        raise SourceIntegrityError("Канонический URL не подтверждает группу, дату и тип недели")
    prefix = _url_segments(requested_url)
    if prefix and actual[:-3] != prefix[:-3]:
        raise SourceIntegrityError("Канонический URL имеет неподдерживаемый путь")
    return actual


def _canonical_validation_metadata(
    final_url: str,
    requested_url: str,
    target: date,
    group: str,
    parity: str,
) -> dict[str, Any]:
    """Validate and describe the canonical path without consulting JavaScript UI state."""
    segments = _validate_canonical_url(final_url, requested_url, target, group, parity)
    return {
        "group_validation_method": "canonical_url_path",
        "canonical_group": segments[-3],
        "requested_group": group,
        "canonical_date": segments[-2],
        "requested_date": target.isoformat(),
        "canonical_parity": segments[-1],
        "expected_parity": parity,
        "redirect_detected": final_url.rstrip("/") != requested_url.rstrip("/"),
        "normalized_final_segments": segments,
    }


def _validate_canonical_snapshot_state(
    state: dict[str, Any], target: date, group: str, parity: str
) -> None:
    """Validate only facts available in a JavaScript-disabled server snapshot."""
    if state.get("promptPresent"):
        raise SourceIntegrityError("Источник вернул приглашение выбрать преподавателя или группу")
    if not state.get("dateHeaderMatches"):
        raise SourceIntegrityError("Официальная страница не подтвердила запрошенную дату")
    if not state.get("parityMatches"):
        raise SourceIntegrityError("Официальная страница не подтвердила тип учебной недели")
    if _looks_like_error_page(state.get("title", ""), state.get("pageText", "")):
        raise SourceIntegrityError("Канонический URL вернул страницу ошибки")
    if not state.get("headingPresent") or not state.get("tablePresent"):
        raise SourceIntegrityError("Канонический URL не содержит заголовок и таблицу расписания")
    if not state.get("hasContent"):
        raise SourceIntegrityError("Источник не подтвердил занятия или официальное отсутствие занятий")


def _validate_schedule_document(
    state: dict[str, Any], fragment: dict[str, Any], week: dict[date, DaySchedule]
) -> int:
    """Validate structured schedule content independently from UI controls."""
    if fragment.get("tableCount") != 1 or not fragment.get("tableHtml"):
        raise SourceIntegrityError("Источник не содержит ровно одну таблицу расписания")
    if not state.get("weekdays"):
        raise SourceIntegrityError("Источник не содержит дней недельного расписания")
    lesson_count = sum(len(day.lessons) for day in week.values())
    if lesson_count == 0 and len(week) != 7:
        raise SourceIntegrityError("Пустая неделя не подтверждена разделами всех семи дней")
    if fragment.get("lessonNameCount", 0) and fragment["lessonNameCount"] != lesson_count:
        raise SourceIntegrityError("Число lesson-name не совпадает с распознанными занятиями")
    if fragment.get("lessonRows", 0) and fragment["lessonRows"] != lesson_count:
        raise SourceIntegrityError("Число строк занятий не совпадает с распознанными занятиями")
    return lesson_count


def _looks_like_error_page(title: str, text: str) -> bool:
    sample = f"{title}\n{text[:4000]}".casefold()
    return any(marker in sample for marker in _ERROR_MARKERS)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)(authorization|cookie|set-cookie|proxy(?:[_ -]?url)?|api[_ -]?key|token|secret)"
    r"(\s*[:=]\s*)([^\s,;<>\"']+)"
)
_DIAGNOSTIC_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;<>\"']+")


def _redact_diagnostic(value: Any) -> Any:
    """Remove credential-shaped values before writing browser diagnostics."""
    if isinstance(value, str):
        value = _DIAGNOSTIC_BEARER_RE.sub("Bearer <redacted>", value)
        return _DIAGNOSTIC_SECRET_RE.sub(r"\1\2<redacted>", value)
    if isinstance(value, list):
        return [_redact_diagnostic(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_diagnostic(item) for key, item in value.items()}
    return value


def _select_metadata(page) -> list[dict[str, Any]]:
    return page.evaluate("""() => [...document.querySelectorAll('select')].map((el) => ({
      id: el.id || '', name: el.name || '', value: el.value || '',
      selectedText: el.selectedOptions?.[0]?.textContent?.trim() || '',
      options: [...el.options].map((o) => ({value: o.value, text: o.textContent.trim()}))
    }))""")


def _response_mentions_group(response_url: str, group: str) -> bool:
    return group.casefold() in unquote(response_url).casefold()


def _response_is_target_group(response, group: str) -> bool:
    """Identify the group schedule XHR without trusting analytics calls."""
    try:
        if response.request.resource_type not in {"xhr", "fetch"}:
            return False
    except Exception:
        return False
    lowered_url = response.url.casefold()
    if any(marker in lowered_url for marker in ("google-analytics", "googletagmanager", "doubleclick", "metrika", "mc.yandex", "facebook")):
        return False
    if _response_mentions_group(response.url, group):
        return True
    try:
        post_data = response.request.post_data or ""
    except Exception:
        post_data = ""
    return _response_mentions_group(post_data, group)


def _redirect_chain(response) -> list[str]:
    """Return only safe URL paths for a document redirect chain."""
    chain: list[str] = []
    try:
        request = response.request
        while request is not None and len(chain) < 20:
            parsed = urlsplit(request.url)
            chain.append(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
            request = request.redirected_from
    except Exception:
        return chain
    return list(reversed(chain))


def _extract_parity(page) -> str:
    """Read the source's currently active week type without guessing it."""
    value = page.evaluate(
        """() => {
          const values = [];
          const add = (node) => {
            if (!node) return;
            const text = (node.innerText || node.textContent || '').trim().toLowerCase();
            if (text) values.push(text);
          };
          for (const node of document.querySelectorAll(
            '[aria-pressed="true"], input:checked, .active, .selected, .btn.active'
          )) add(node);
          add(document.querySelector('#weekParity'));
          add(document.querySelector('#schedule-container h2'));
          add(document.querySelector('#todayDate'));
          for (const text of values) {
            if (text.includes('числитель')) return 'числитель';
            if (text.includes('знаменатель')) return 'знаменатель';
          }
          return '';
        }"""
    )
    if value not in {"числитель", "знаменатель"}:
        raise ScheduleParseError("Источник не подтвердил активный тип учебной недели")
    return value


def _canonical_url(bootstrap_url: str, target: date, group: str, parity: str) -> str:
    """Use the official canonical path, preserving local test origins when supplied."""
    parsed = urlparse(bootstrap_url)
    if parsed.hostname == urlparse(SOURCE_URL).hostname:
        return build_canonical_url(target, group, parity)
    base_path = parsed.path.rstrip("/") or "/"
    path = f"{base_path}/{quote(group, safe='')}/{target.isoformat()}/{quote(parity, safe='')}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _canonical_state(page, target: date, group: str, parity: str) -> dict[str, Any]:
    return page.evaluate(
        r"""({target, group, parity}) => {
          const decode = (value) => { try { return decodeURIComponent(value); } catch (_) { return ''; } };
          const parts = location.pathname.split('/').filter(Boolean).slice(-3).map(decode);
          const select = document.querySelector('#gruppa');
          const selectedText = select?.selectedOptions?.[0]?.textContent?.trim() || '';
          const body = document.body?.textContent || '';
          const bodyClone = document.body?.cloneNode(true);
          bodyClone?.querySelectorAll('script, style, noscript').forEach((node) => node.remove());
          const safeBody = bodyClone?.textContent || body;
          const container = document.querySelector('#schedule-container');
          const text = container?.textContent || '';
          const options = [...document.querySelectorAll('#gruppa option')];
          const dateParts = target.split('-').map(Number);
          const dateTexts = [
            target,
            `${dateParts[2]}.${dateParts[1]}.${dateParts[0]}`,
            `${String(dateParts[2]).padStart(2, '0')}.${String(dateParts[1]).padStart(2, '0')}.${dateParts[0]}`,
          ];
          const parityFrom = (value) => {
            const text = (value || '').trim().toLowerCase();
            const matches = ['числитель', 'знаменатель'].filter((item) => text.includes(item));
            return matches.length === 1 ? matches[0] : '';
          };
          const weekNode = document.querySelector('#weekParity');
          const activeNodes = [...document.querySelectorAll('#weekParity.active, #weekParity .active, #weekParity [aria-pressed="true"], #weekParity [data-week-parity].active, #weekParity [data-week-parity][aria-pressed="true"], [data-week-parity].active, [data-week-parity][aria-pressed="true"], button.active, [role="button"].active')];
          const activeValues = activeNodes.map((node) => parityFrom(node.innerText || node.textContent)).filter(Boolean);
          const active = activeValues[0] || '';
          const weekValues = [
            weekNode?.value,
            weekNode?.dataset?.parity,
            ...(weekNode ? [...weekNode.querySelectorAll('.active, [aria-pressed="true"], [data-week-parity]')].map((node) => node.innerText || node.textContent) : []),
          ].map(parityFrom).filter(Boolean);
          const weekText = parityFrom(weekNode?.textContent);
          const weekParity = weekValues[0] || weekText || '';
          const activeParityControlPresent = activeNodes.length > 0;
          const todayDateText = (document.querySelector('#todayDate')?.textContent || '').trim();
          const heading = (document.querySelector('#schedule-container h2')?.textContent || '').toLowerCase();
          const headingPresent = !!document.querySelector('#schedule-container h1, #schedule-container h2, #schedule-container h3');
          const parityText = [weekParity, active, parityFrom(heading), parityFrom(todayDateText)].filter(Boolean).join(' | ');
          const dateHeaderMatches = dateTexts.some((value) => todayDateText.includes(value));
          const hasLesson = /\d{2}:\d{2}\s*[-–—]\s*\d{2}:\d{2}/.test(text);
          const explicitEmpty = /нет занятий|занятий нет/i.test(text);
          const weekdays = [...new Set([...container?.querySelectorAll('tr') || []]
            .map((row) => (row.textContent || '').trim())
            .filter((value) => /(?:Пн|Вт|Ср|Чт|Пт|Сб|Вс)\.?/i.test(value)))];
          return {
            pathParts: parts,
            selectedGroup: select?.value || '',
            selectedText,
            groupOptionExists: options.some((option) => option.value === group || (option.textContent || '').trim() === group),
            title: document.title || '',
            pageText: safeBody,
            dateMatches: dateHeaderMatches || dateTexts.some((value) => safeBody.includes(value) || (document.title || '').includes(value)),
            dateHeaderMatches,
            todayDateText,
            parityMatches: parityText.includes(parity) && (!weekNode || weekParity === parity || active === parity) && (!activeParityControlPresent || active === parity),
            weekParityText: weekParity,
            activeParityText: active,
            activeParityControlPresent,
            parityText,
            headingPresent,
            promptPresent: /Выберете преподавателя или группу/i.test(safeBody) || /Выберете преподавателя или группу/i.test(text),
            containerClass: container?.className || '',
            scheduleText: text,
            containerVisible: !!container && !!(container.offsetWidth || container.offsetHeight),
            tablePresent: !!container?.querySelector('table'),
            weekdays,
            hasContent: hasLesson || explicitEmpty,
            explicitEmpty,
            finalUrl: location.href
          };
        }""",
        {"target": target.isoformat(), "group": group, "parity": parity},
    )


def _wait_for_canonical_state(page, target: date, group: str, parity: str, timeout_ms: int) -> dict[str, Any]:
    try:
        page.wait_for_selector("#schedule-container", state="attached", timeout=timeout_ms)
        page.locator("#schedule-container table").first.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise ScheduleParseError("Канонический snapshot не содержит прикреплённую таблицу расписания") from exc
    return _canonical_state(page, target, group, parity)


def _schedule_fragment(page) -> dict[str, Any]:
    return page.evaluate(
        r"""() => {
          const container = document.querySelector('#schedule-container, section.schedule-container, .schedule-container');
          const table = container?.querySelector('table');
          const style = container ? getComputedStyle(container) : null;
          const rows = table ? [...table.querySelectorAll('tr')] : [];
          const allRows = container ? [...container.querySelectorAll('tr')] : [];
          const lessons = container ? [...container.querySelectorAll('.lesson-name')] : [];
          const text = container?.textContent || '';
          return {
            containerHtml: container?.outerHTML || '',
            tableHtml: table?.outerHTML || '',
            text,
            containerClass: container?.className || '',
            display: style?.display || '',
            visibility: style?.visibility || '',
            opacity: style?.opacity || '',
            width: container?.getBoundingClientRect().width || 0,
            height: container?.getBoundingClientRect().height || 0,
            tableCount: container ? container.querySelectorAll('table').length : 0,
            rowCount: rows.length,
            lessonNameCount: lessons.length,
            lessonRows: lessons.filter((node) => node.closest('tr')).length,
            weekdays: [...new Set(allRows.map((row) => row.textContent || '').filter((value) => /(?:Пн|Вт|Ср|Чт|Пт|Сб|Вс)\.?/i.test(value)))],
            htmlSha256: ''
          };
        }"""
    )


def _reveal_schedule(page) -> None:
    page.evaluate(
        r"""() => {
          const target = document.querySelector('#schedule-container, section.schedule-container, .schedule-container');
          if (!target) return false;
          let node = target;
          while (node && node !== document.body) {
            if (node instanceof HTMLElement) {
              node.style.setProperty('display', 'block', 'important');
              node.style.setProperty('visibility', 'visible', 'important');
              node.style.setProperty('opacity', '1', 'important');
              node.style.setProperty('max-height', 'none', 'important');
              node.style.setProperty('height', 'auto', 'important');
              node.style.setProperty('overflow', 'visible', 'important');
            }
            node = node.parentElement;
          }
          return true;
        }"""
    )


def _png_stats(image: bytes) -> dict[str, Any]:
    signature = b"\x89PNG\r\n\x1a\n"
    if not image.startswith(signature):
        raise SourceIntegrityError("Скриншот не является PNG")
    pos = len(signature)
    width = height = bit_depth = color_type = None
    idat: list[bytes] = []
    saw_iend = False
    while pos + 12 <= len(image):
        length = struct.unpack(">I", image[pos : pos + 4])[0]
        kind = image[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(image):
            raise SourceIntegrityError("PNG повреждён")
        payload = image[pos + 8 : pos + 8 + length]
        crc = struct.unpack(">I", image[pos + 8 + length : end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise SourceIntegrityError("PNG имеет неверную CRC")
        if kind == b"IHDR":
            if length != 13:
                raise SourceIntegrityError("PNG имеет неверный IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if compression or filtering or interlace or bit_depth != 8:
                raise SourceIntegrityError("PNG имеет неподдерживаемый формат пикселей")
        elif kind == b"IDAT":
            idat.append(payload)
        elif kind == b"IEND":
            saw_iend = end == len(image)
            break
        pos = end
    if not saw_iend or width is None or height is None or not idat:
        raise SourceIntegrityError("PNG неполный")
    if width < 100 or height < 50:
        raise SourceIntegrityError("Скриншот слишком мал для расписания")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise SourceIntegrityError("PNG имеет неподдерживаемый тип цвета")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise SourceIntegrityError("PNG имеет повреждённые данные изображения") from exc
    stride = width * channels
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise SourceIntegrityError("PNG имеет неверный размер данных")
    previous = bytearray(stride)
    nonzero = 0
    distinct: set[bytes] = set()
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        row = bytearray(raw[offset + 1 : offset + 1 + stride])
        offset += stride + 1
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + up) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                p = left + up - upper_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                predictor = left if pa <= pb and pa <= pc else (up if pb <= pc else upper_left)
                row[index] = (row[index] + predictor) & 0xFF
            elif filter_type != 0:
                raise SourceIntegrityError("PNG имеет неизвестный фильтр")
        previous = row
        nonzero += sum(1 for value in row if value)
        if len(distinct) < 8:
            distinct.update(bytes(row[index : index + channels]) for index in range(0, stride, channels))
    if nonzero < max(100, width * height // 1000) or len(distinct) < 2:
        raise SourceIntegrityError("Скриншот пустой или одноцветный")
    return {"width": width, "height": height, "nonzero_pixels": nonzero, "distinct_colors": len(distinct)}


def _select_group(
    page, group: str, timeout_ms: int, target_statuses: list[int] | None = None
) -> None:
    """Select only #gruppa; never probe or change unrelated group options."""
    group_select = page.locator("select#gruppa").first
    try:
        group_select.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise ScheduleParseError("Интерфейс источника не содержит select#gruppa") from exc

    option = page.locator(f'select#gruppa option[value="{group}"]').first
    try:
        option.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise ScheduleParseError(
            f"Группа {group} отсутствует в интерфейсе источника после полной загрузки списка"
        ) from exc

    target_responses: list[int] = []

    def on_response(response) -> None:
        if _response_is_target_group(response, group):
            target_responses.append(response.status)
            if target_statuses is not None:
                target_statuses.append(response.status)

    page.on("response", on_response)
    try:
        # This is intentionally the only option mutation in the whole routine.
        group_select.select_option(value=group, timeout=timeout_ms)
        group_select.evaluate(
            "el => { [...el.options].forEach(o => o.removeAttribute('selected')); "
            "el.selectedOptions[0]?.setAttribute('selected', 'selected'); }"
        )
        page.wait_for_function(
            "expected => document.querySelector('#gruppa')?.value === expected",
            arg=group,
            timeout=timeout_ms,
        )

        # Select2 mirrors the hidden select in a visible rendered selection.
        rendered = page.locator(".select2-selection__rendered")
        if rendered.count() and not rendered.filter(has_text=re.compile(rf"^{re.escape(group)}$")).count():
            raise ScheduleParseError("Select2 не отобразил выбранную группу")

        if target_responses and any(status >= 400 for status in target_responses):
            raise SourceUnavailable(f"Целевой запрос группы {group} вернул HTTP {target_responses[-1]}")
    finally:
        page.remove_listener("response", on_response)


def _safe_diagnostic_mkdir(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return True
    except Exception:
        return False


def _safe_diagnostic_text(directory: Path, name: str, value: str) -> None:
    try:
        (directory / name).write_text(value, encoding="utf-8")
    except Exception:
        pass


def _safe_diagnostic_json(directory: Path, name: str, value: Any) -> None:
    try:
        _safe_diagnostic_text(
            directory,
            name,
            json.dumps(_redact_diagnostic(value), ensure_ascii=False, indent=2, default=str),
        )
    except Exception:
        pass


def _write_minimal_diagnostics(
    directory: Path,
    *,
    responses: list[dict[str, Any]],
    console_errors: list[str],
    page_errors: list[str],
    target_statuses: list[int],
    capture_metadata: dict[str, Any],
    error: BaseException,
) -> None:
    """Best-effort fallback that never replaces the original capture error."""
    if not _safe_diagnostic_mkdir(directory):
        return
    meta = {
        **capture_metadata,
        "url": capture_metadata.get("final_url", ""),
        "title": "",
        "selects": [],
        "page_text": "",
        "schedule_text": "",
        "responses": responses[-100:],
        "target_group_http_statuses": target_statuses[-20:],
        "console_errors": console_errors[-100:],
        "page_errors": page_errors[-100:],
        "error": type(error).__name__,
        "error_message": str(error),
    }
    _safe_diagnostic_json(directory, "metadata.json", meta)
    _safe_diagnostic_json(directory, "network.json", responses[-100:])
    _safe_diagnostic_json(directory, "console-errors.json", console_errors[-100:])
    _safe_diagnostic_json(directory, "page-errors.json", page_errors[-100:])


def _write_diagnostics(
    page,
    directory: Path,
    *,
    responses: list[dict[str, Any]],
    console_errors: list[str],
    page_errors: list[str],
    target_statuses: list[int] | None = None,
    capture_metadata: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    if not _safe_diagnostic_mkdir(directory):
        return
    try:
        page.screenshot(path=str(directory / "full-page.png"), full_page=True)
    except Exception:
        pass
    try:
        page.screenshot(path=str(directory / "page.png"), full_page=True)
    except Exception:
        pass
    try:
        page.locator("#schedule-container, section.schedule-container, .schedule-container").first.screenshot(
            path=str(directory / "schedule-container.png"), animations="disabled"
        )
    except Exception:
        pass
    try:
        (directory / "page.html").write_text(
            _redact_diagnostic(page.content()), encoding="utf-8"
        )
    except Exception:
        pass

    # Keep each diagnostic field independent: a broken selector must not prevent
    # metadata.json from being written for the failed browser attempt.
    def safe_page_value(factory, default):
        try:
            return factory()
        except Exception:
            return default

    selects = safe_page_value(lambda: _select_metadata(page), [])
    page_text = safe_page_value(
        lambda: page.locator("body").text_content(timeout=1000)
        if page.locator("body").count()
        else "",
        "",
    )
    title = safe_page_value(page.title, "")
    url = safe_page_value(lambda: page.url, "")
    fragment = safe_page_value(lambda: _schedule_fragment(page), {})
    if fragment.get("tableHtml"):
        fragment["htmlSha256"] = _sha256_text(fragment["tableHtml"])
    meta: dict[str, Any] = {
        "url": url,
        "title": title,
        "selects": selects,
        "page_text": page_text,
        "schedule_text": fragment.get("text", ""),
        "schedule_class": fragment.get("containerClass", ""),
        "schedule_fragment": fragment,
        "responses": responses[-100:],
        "console_errors": console_errors[-100:],
        "page_errors": page_errors[-100:],
        "target_group_http_statuses": (target_statuses or [])[-20:],
    }
    if error is not None:
        meta["error"] = type(error).__name__
    try:
        if capture_metadata:
            meta.update(capture_metadata)
    except Exception:
        # capture_metadata is diagnostic-only; never let an unserialisable value
        # suppress the basic page/select/network record.
        meta["capture_metadata_error"] = True
    meta = _redact_diagnostic(meta)
    meta["error_message"] = str(error) if error is not None else ""
    _safe_diagnostic_json(directory, "metadata.json", meta)
    _safe_diagnostic_json(directory, "network.json", responses[-100:])
    _safe_diagnostic_json(directory, "console-errors.json", console_errors[-100:])
    _safe_diagnostic_json(directory, "page-errors.json", page_errors[-100:])


def _capture_live_once(
    target: date,
    group: str,
    output: Path,
    *,
    url_builder: UrlBuilder = build_source_url,
    timeout_ms: int = 35_000,
    return_week: bool = False,
    day_output: Path | None = None,
) -> tuple[DaySchedule, datetime] | tuple[DaySchedule, datetime, dict[date, DaySchedule]]:
    """Capture one official, server-rendered weekly snapshot.

    The site JavaScript is known to reset the group select and replace a valid
    schedule with a prompt.  Consequently the capture context deliberately has
    JavaScript disabled.  The same frozen HTML is validated and parsed, while
    the only later mutation is presentation CSS used to render the screenshot.
    """
    url = url_builder(target, group)
    output.parent.mkdir(parents=True, exist_ok=True)
    diagnostic_env = os.environ.get("IB261_DIAGNOSTICS_DIR", "").strip()
    diagnostics_dir = Path(diagnostic_env) if diagnostic_env else None
    page = None
    browser = None
    context = None
    responses: list[dict[str, Any]] = []
    target_statuses: list[int] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    capture_metadata: dict[str, Any] = {
        "requested_bootstrap_url": url,
        "requested_url": url,
        "requested_strategy": "canonical-server-snapshot",
        "javascript_enabled": False,
        "target_date": target.isoformat(),
        "target_weekday": _WEEKDAYS[target.weekday()],
        "computed_week_monday": week_monday(target).isoformat(),
        "final_url": "",
    }
    temporary_output: Path | None = None
    diagnostics_written = False
    try:
        with sync_playwright() as playwright:
            browser_options: dict[str, Any] = {}
            proxy = os.environ.get("SCHEDULE_PROXY_URL", "").strip()
            if proxy:
                browser_options["proxy"] = _playwright_proxy(proxy)
            browser = playwright.chromium.launch(headless=True, **browser_options)
            context = browser.new_context(
                timezone_id="Europe/Moscow",
                locale="ru-RU",
                viewport={"width": 1400, "height": 1200},
                java_script_enabled=False,
            )
            page = context.new_page()
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            def record_response(response) -> None:
                try:
                    resource_type = response.request.resource_type
                except Exception:
                    return
                if resource_type not in {"document", "xhr", "fetch"}:
                    return
                parsed = urlsplit(response.url)
                responses.append(
                    {
                        "url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
                        "status": response.status,
                        "resource_type": resource_type,
                        "content_type": response.headers.get("content-type", ""),
                    }
                )

            page.on("response", record_response)
            try:
                try:
                    bootstrap_response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeoutError as exc:
                    raise SourceUnavailable("Сетевой тайм-аут при открытии официального источника") from exc
                if bootstrap_response is None or bootstrap_response.status != 200:
                    status = bootstrap_response.status if bootstrap_response is not None else "no-response"
                    raise SourceUnavailable(f"Официальный источник HTTP {status}")
                bootstrap_content_type = bootstrap_response.headers.get("content-type", "")
                capture_metadata.update({
                    "bootstrap_http_status": bootstrap_response.status,
                    "bootstrap_content_type": bootstrap_content_type,
                })
                if "text/html" not in bootstrap_content_type.casefold():
                    raise SourceIntegrityError("Bootstrap-ответ не является HTML")

                # The bootstrap document is static as well; it only supplies the
                # source-confirmed parity needed to construct the canonical URL.
                parity = _extract_parity(page)
                canonical_url = _canonical_url(url, target, group, parity)
                capture_metadata.update({
                    "parity": parity,
                    "requested_canonical_url": canonical_url,
                })
                try:
                    canonical_response = page.goto(canonical_url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeoutError as exc:
                    raise SourceUnavailable("Сетевой тайм-аут при открытии канонического URL расписания") from exc
                content_type = canonical_response.headers.get("content-type", "") if canonical_response else ""
                capture_metadata.update({
                    "final_url": page.url,
                    "http_status": canonical_response.status if canonical_response is not None else None,
                    "content_type": content_type,
                    "redirect_chain": _redirect_chain(canonical_response) if canonical_response is not None else [],
                })
                if canonical_response is None or canonical_response.status != 200:
                    status = canonical_response.status if canonical_response is not None else "no-response"
                    raise SourceUnavailable(f"Канонический URL расписания вернул HTTP {status}")
                if "text/html" not in content_type.casefold():
                    raise SourceIntegrityError("Канонический ответ не является HTML")
                capture_metadata.update(
                    _canonical_validation_metadata(page.url, canonical_url, target, group, parity)
                )

                state = _wait_for_canonical_state(page, target, group, parity, timeout_ms)
                _validate_canonical_snapshot_state(state, target, group, parity)
                capture_metadata.update({
                    "selected_group": state.get("selectedGroup", ""),
                    "selected_group_text": state.get("selectedText", ""),
                    "group_option_exists": state.get("groupOptionExists", False),
                    "group_option_validation": "advisory_only_javascript_disabled",
                    "date_header": state.get("todayDateText", ""),
                    "parity_text": state.get("parityText", ""),
                    "schedule_class": state.get("containerClass", ""),
                    "schedule_text": state.get("scheduleText", ""),
                    "active_parity": state.get("activeParityText", ""),
                    "weekday_labels": state.get("weekdays", []),
                })

                # Freeze the authoritative server DOM.  No site JavaScript has run
                # in this context, so this HTML is also the source for parsing.
                fragment_before = _schedule_fragment(page)
                table_html = fragment_before.get("tableHtml", "")
                table_hash = _sha256_text(table_html)
                frozen_html = page.content()
                source_hash = _sha256_text(frozen_html)
                fragment_before["htmlSha256"] = table_hash
                capture_metadata.update({
                    "schedule_class": fragment_before.get("containerClass", ""),
                    "schedule_display": fragment_before.get("display", ""),
                    "schedule_visibility": fragment_before.get("visibility", ""),
                    "schedule_opacity": fragment_before.get("opacity", ""),
                    "table_count": fragment_before.get("tableCount", 0),
                    "row_count": fragment_before.get("rowCount", 0),
                    "lesson_name_count": fragment_before.get("lessonNameCount", 0),
                    "lesson_row_count": fragment_before.get("lessonRows", 0),
                    "schedule_html_sha256": table_hash,
                    "source_html_sha256": source_hash,
                })

                # The canonical URL is the authoritative group proof for the
                # JavaScript-disabled snapshot.  Its catalog may legitimately
                # contain only the "Все" option, so parsing must not require a
                # client-populated group option in this strategy.
                schedule = parse_schedule_html(
                    frozen_html,
                    target,
                    group,
                    group_confirmed_by_url=True,
                )
                week = parse_week_schedule_html(
                    frozen_html,
                    target,
                    group,
                    group_confirmed_by_url=True,
                )
                if week.get(target) != schedule:
                    raise SourceIntegrityError("Дневной и недельный разбор snapshot расходятся")
                if schedule.parity != parity:
                    raise SourceIntegrityError("Распарсенная чётность не совпадает с canonical URL")
                total_lessons = _validate_schedule_document(state, fragment_before, week)
                capture_metadata.update({
                    "week_days": [day.isoformat() for day in sorted(week)],
                    "week_lesson_count": total_lessons,
                    "selected_day_lesson_count": len(schedule.lessons),
                    "lesson_count": total_lessons,
                    "schedule_table_valid": True,
                    "empty_confirmed": schedule.empty_confirmed,
                })

                # Only presentation CSS is changed.  The table's source HTML must
                # remain identical to the frozen snapshot used above.
                if not page.evaluate("""() => !!document.querySelector('#schedule-container')"""):
                    raise SourceIntegrityError("Блок расписания исчез до создания PNG")
                _reveal_schedule(page)
                fragment_after_reveal = _schedule_fragment(page)
                after_hash = _sha256_text(fragment_after_reveal.get("tableHtml", ""))
                capture_metadata["dom_hash_before_png"] = table_hash
                capture_metadata["dom_hash_after_reveal"] = after_hash
                if after_hash != table_hash:
                    raise SourceIntegrityError("HTML таблицы изменился при раскрытии блока")
                capture_metadata.update({
                    "revealed_width": fragment_after_reveal.get("width", 0),
                    "revealed_height": fragment_after_reveal.get("height", 0),
                    "revealed_schedule_class": fragment_after_reveal.get("containerClass", ""),
                    "revealed_display": fragment_after_reveal.get("display", ""),
                    "revealed_visibility": fragment_after_reveal.get("visibility", ""),
                })
                if fragment_after_reveal.get("width", 0) < 100 or fragment_after_reveal.get("height", 0) < 50:
                    raise SourceIntegrityError("Блок расписания имеет недопустимый размер")

                fd, temporary_name = tempfile.mkstemp(prefix=".schedule-", suffix=".png", dir=output.parent)
                os.close(fd)
                temporary_output = Path(temporary_name)
                page.locator("#schedule-container").first.screenshot(
                    path=str(temporary_output), animations="disabled"
                )
                image = temporary_output.read_bytes()
                image_stats = _png_stats(image)
                capture_metadata.update({"png_sha256": hashlib.sha256(image).hexdigest(), "png_stats": image_stats})
                os.replace(temporary_output, output)

                if day_output is not None:
                    # Derive the crop from actual table row geometry.  The
                    # weekday heading starts a section; the next heading (or
                    # the table bottom for Sunday) closes it.
                    label = _WEEKDAYS[target.weekday()]
                    crop = page.evaluate(
                        """(label) => {
                          const root = document.querySelector('#schedule-container');
                          const table = root?.querySelector('table');
                          if (!table) return null;
                          const labels = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
                          const rows = [...table.querySelectorAll('tr')];
                          const found = [];
                          for (let i = 0; i < rows.length; i++) {
                            const cells = [...rows[i].children];
                            const text = (cells[0]?.textContent || '').replace(/\\s+/g,' ').trim();
                            if (text === label || text.startsWith(label + '.')) found.push(i);
                          }
                          if (!found.length) return null;
                          const start = found[0];
                          const next = found.slice(1).find(i => i > start);
                          const end = next === undefined ? rows.length : next;
                          const chosen = rows.slice(start, end).map(r => r.getBoundingClientRect()).filter(r => r.width && r.height);
                          const box = table.getBoundingClientRect();
                          if (!chosen.length || !box.width || !box.height) return null;
                          const top = Math.max(box.top, Math.min(...chosen.map(r => r.top)) - 4);
                          const bottom = Math.min(box.bottom, Math.max(...chosen.map(r => r.bottom)) + 4);
                          return {x: box.left, y: top, width: box.width, height: bottom-top};
                        }""",
                        label,
                    )
                    if crop and crop.get("width", 0) >= 100 and crop.get("height", 0) >= 30:
                        try:
                            day_output.parent.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(day_output), clip=crop)
                            _png_stats(day_output.read_bytes())
                        except Exception:
                            # The authoritative weekly capture remains valid.
                            # Its caller will send it with an explicit crop warning.
                            day_output.unlink(missing_ok=True)

                checked_at = datetime.now(ZoneInfo("Europe/Moscow"))
                if return_week:
                    return schedule, checked_at, week
                return schedule, checked_at
            except Exception as exc:
                if temporary_output is not None and temporary_output.exists():
                    try:
                        temporary_output.unlink()
                    except Exception:
                        pass
                if diagnostics_dir is not None and page is not None:
                    try:
                        _write_diagnostics(
                            page,
                            diagnostics_dir,
                            responses=responses,
                            console_errors=console_errors,
                            page_errors=page_errors,
                            target_statuses=target_statuses,
                            capture_metadata=capture_metadata,
                            error=exc,
                        )
                    except Exception:
                        pass
                    diagnostics_written = True
                raise
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as exc:
        # Covers failures before a page exists (for example browser launch).
        if diagnostics_dir is not None and not diagnostics_written:
            if page is not None:
                try:
                    _write_diagnostics(
                        page, diagnostics_dir, responses=responses,
                        console_errors=console_errors, page_errors=page_errors,
                        target_statuses=target_statuses, capture_metadata=capture_metadata, error=exc,
                    )
                except Exception:
                    pass
            else:
                _write_minimal_diagnostics(
                    diagnostics_dir,
                    responses=responses,
                    console_errors=console_errors,
                    page_errors=page_errors,
                    target_statuses=target_statuses,
                    capture_metadata=capture_metadata,
                    error=exc,
                )
        raise


def _is_retryable_capture_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if not isinstance(exc, SourceUnavailable):
        return False
    message = str(exc).casefold()
    if any(marker in message for marker in ("403", "400", "401", "404", "целевой запрос", "не является html")):
        return False
    return any(marker in message for marker in ("тайм-аут", "timeout", "429", "http 5", "connection", "reset", "no-response"))


def capture_live(
    target: date,
    group: str,
    output: Path,
    *,
    url_builder: UrlBuilder = build_source_url,
    timeout_ms: int = 35_000,
    return_week: bool = False,
    day_output: Path | None = None,
) -> tuple[DaySchedule, datetime] | tuple[DaySchedule, datetime, dict[date, DaySchedule]]:
    """Capture at most twice for transient network failures only."""
    last_error: BaseException | None = None
    for attempt in range(2):
        try:
            return _capture_live_once(
                target,
                group,
                output,
                url_builder=url_builder,
                timeout_ms=timeout_ms,
                return_week=return_week,
                day_output=day_output,
            )
        except Exception as exc:
            last_error = exc
            if attempt >= 1 or not _is_retryable_capture_error(exc):
                raise
            time.sleep(0.25 * (2**attempt))
    assert last_error is not None
    raise last_error


def capture_live_week(
    target: date,
    group: str,
    output: Path,
    *,
    day_output: Path | None = None,
    timeout_ms: int = 35_000,
) -> tuple[dict[date, DaySchedule], datetime]:
    """Capture one official page and return every parsed day plus its PNG."""
    _day, checked_at, week = capture_live(
        target, group, output, timeout_ms=timeout_ms, return_week=True, day_output=day_output
    )
    return week, checked_at
