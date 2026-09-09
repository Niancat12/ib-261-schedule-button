"""Basic tests for plugin functionality."""

import json

import pytest

from plugin import display_schedule, parse_output


def test_parse_valid_json():
    """Test that valid JSON is parsed correctly."""
    output = json.dumps(
        {
            "status": "ok",
            "schedule": {"group": "Group A", "time": "14:00"},
            "cached_at": "2024-01-01T12:00:00Z",
        }
    )
    result = parse_output(output)
    assert result is not None
    assert result["status"] == "ok"
    assert result["schedule"]["group"] == "Group A"


def test_invalid_json():
    """Test that invalid JSON raises an error."""
    output = "{invalid json}"
    with pytest.raises(ValueError, match="Malformed JSON"):
        parse_output(output)


def test_display_schedule():
    """Test schedule display with valid data."""
    schedule_data = {
        "status": "ok",
        "schedule": {"group": "Group A", "time": "14:00", "location": "Room 101"},
        "cached_at": "2024-01-01T12:00:00Z",
    }
    result = display_schedule(schedule_data)
    assert "Group A" in result
    assert "14:00" in result
    assert "Room 101" in result


def test_display_schedule_none():
    """Test schedule display with None."""
    result = display_schedule(None)
    assert result == "No schedule available"
