"""
IB261 Schedule Plugin with Async Lifecycle
"""

import asyncio
from typing import Any

__version__ = "0.1.0"

# Per-chat semaphore for one active session per chat
chat_semaphores: dict[int, asyncio.Semaphore] = {}

# Track running workers per chat
_running_workers: dict[int, bool] = {}


def get_semaphore(chat_id: int) -> asyncio.Semaphore:
    """Get or create a semaphore for a chat."""
    if chat_id not in chat_semaphores:
        chat_semaphores[chat_id] = asyncio.Semaphore(1)
    return chat_semaphores[chat_id]


async def button_handler(chat_id: int, callback_data: str) -> dict[str, Any]:
    """
    Button handler with per-chat semaphore.
    Only one active session per chat is allowed.
    """
    semaphore = get_semaphore(chat_id)

    # Try to acquire semaphore (non-blocking check)
    if not semaphore._value:  # Already locked
        return {"status": "ignored", "reason": "concurrent_press"}

    try:
        async with semaphore:
            # Mark worker as running
            _running_workers[chat_id] = True

            try:
                # Simulate async work
                await asyncio.sleep(0.1)
                result = {"status": "success", "chat_id": chat_id, "data": callback_data}
                return result
            finally:
                # Cleanup
                _running_workers.pop(chat_id, None)
    except Exception:
        _running_workers.pop(chat_id, None)
        raise


def get_running_workers() -> dict[int, bool]:
    """Get current running workers state."""
    return _running_workers.copy()


def clear_semaphores():
    """Clear all semaphores (for testing)."""
    chat_semaphores.clear()
    _running_workers.clear()
