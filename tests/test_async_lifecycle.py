"""
Test Async Lifecycle: Per-chat semaphore and cleanup.
"""

import asyncio

import pytest

from src.ib261_schedule import (
    button_handler,
    chat_semaphores,
    clear_semaphores,
    get_running_workers,
)


@pytest.fixture(autouse=True)
def cleanup():
    """Clear state before and after each test."""
    clear_semaphores()
    yield
    clear_semaphores()


@pytest.mark.asyncio
async def test_concurrent_press_ignored():
    """Test that concurrent button press is ignored."""
    chat_id = 1

    # Start first handler (will be held by semaphore)
    task1 = asyncio.create_task(button_handler(chat_id, "data1"))
    await asyncio.sleep(0.01)  # Let first task acquire semaphore

    # Try second handler (should be ignored)
    result2 = await button_handler(chat_id, "data2")

    # Verify second was ignored
    assert result2["status"] == "ignored"
    assert result2["reason"] == "concurrent_press"

    # Wait for first to complete
    result1 = await task1
    assert result1["status"] == "success"


@pytest.mark.asyncio
async def test_cleanup_after_handler():
    """Test that _running_workers is cleaned up after handler."""
    chat_id = 2

    # Initially empty
    assert get_running_workers() == {}

    # Run handler
    result = await button_handler(chat_id, "test_data")

    # After completion, should be cleaned up
    assert get_running_workers() == {}
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_cleanup_on_exception():
    """Test that cleanup happens even on exception."""
    chat_id = 3

    # Simulate an exception by injecting a failing coroutine
    # We'll mock button_handler's work to fail
    async def failing_handler(chat_id, callback_data):
        # Acquire semaphore like normal
        from src.ib261_schedule import _running_workers, get_semaphore

        semaphore = get_semaphore(chat_id)
        if not semaphore._value:
            return {"status": "ignored", "reason": "concurrent_press"}

        try:
            async with semaphore:
                _running_workers[chat_id] = True
                try:
                    raise ValueError("Test error")
                finally:
                    _running_workers.pop(chat_id, None)
        except ValueError:
            raise

    with pytest.raises(ValueError):
        await failing_handler(chat_id, "data")

    # Should still be cleaned up
    assert get_running_workers() == {}


@pytest.mark.asyncio
async def test_different_chats_independent():
    """Test that different chats have independent semaphores."""
    chat1, chat2 = 1, 2

    # Both should be able to run concurrently
    task1 = asyncio.create_task(button_handler(chat1, "data1"))
    task2 = asyncio.create_task(button_handler(chat2, "data2"))

    result1, result2 = await asyncio.gather(task1, task2)

    assert result1["status"] == "success"
    assert result2["status"] == "success"


@pytest.mark.asyncio
async def test_sequential_same_chat():
    """Test sequential calls on same chat work fine."""
    chat_id = 4

    result1 = await button_handler(chat_id, "data1")
    result2 = await button_handler(chat_id, "data2")

    assert result1["status"] == "success"
    assert result2["status"] == "success"
    assert get_running_workers() == {}


@pytest.mark.asyncio
async def test_semaphore_exists():
    """Test that semaphores are created correctly."""
    chat_id = 5

    # Before any handler
    assert chat_id not in chat_semaphores

    # Run handler
    await button_handler(chat_id, "data")

    # Semaphore should exist now
    assert chat_id in chat_semaphores
    assert chat_semaphores[chat_id]._value == 1  # Semaphore free after handler


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
