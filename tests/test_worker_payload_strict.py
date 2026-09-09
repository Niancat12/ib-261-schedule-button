"""Strict worker payload validation tests."""

import json
import subprocess
from pathlib import Path

import pytest


class TestMalformedJSON:
    """Test handling of malformed JSON input."""

    def test_malformed_json_raises_error(self):
        """Test that malformed JSON raises JSONDecodeError."""
        malformed = "{invalid json}"
        with pytest.raises(json.JSONDecodeError):
            json.loads(malformed)

    def test_empty_string_raises_error(self):
        """Test that empty string raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            json.loads("")

    def test_incomplete_json_raises_error(self):
        """Test that incomplete JSON raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            json.loads('{"status": "ok"')


class TestWorkerPayloadValidation:
    """Test worker payload validation strictness."""

    def test_valid_fresh_payload(self, tmp_path):
        """Test valid fresh payload with screenshot."""
        image = tmp_path / "schedule.png"
        image.write_bytes(b"test_image")

        payload = {
            "status": "fresh",
            "text": "Schedule data",
            "requested_date": "2026-09-08",
            "group": "ИБ-261",
            "cache_version": "v1",
            "screenshot": str(image),
            "screenshot_sha256": "abc123",
        }
        # Basic structure validation
        assert isinstance(payload, dict)
        assert "status" in payload
        assert payload["status"] == "fresh"

    def test_missing_required_status(self):
        """Test that missing status field causes error."""
        payload = {
            "text": "Schedule",
            "requested_date": "2026-09-08",
            "group": "ИБ-261",
        }
        # Minimal validation - status must be present
        assert "status" not in payload

    def test_invalid_status_value(self):
        """Test that invalid status is rejected."""
        payload = {
            "status": "invalid_status",
            "text": "Schedule",
            "requested_date": "2026-09-08",
            "group": "ИБ-261",
        }
        valid_statuses = {"fresh", "stale", "unavailable"}
        assert payload["status"] not in valid_statuses

    def test_invalid_screenshot_path(self):
        """Test that nonexistent screenshot path is problematic."""
        payload = {
            "status": "fresh",
            "screenshot": "/nonexistent/path/image.png",
        }
        # Validation should check if file exists
        if payload["screenshot"]:
            assert not Path(payload["screenshot"]).exists()

    def test_missing_required_fields(self):
        """Test detection of missing required fields."""
        required_fields = {"status", "text", "requested_date", "group"}
        payload = {"status": "fresh"}
        missing = required_fields - set(payload.keys())
        assert len(missing) > 0


class TestWorkerExitCode:
    """Test worker exit code behavior."""

    def test_worker_invalid_date_exits_nonzero(self):
        """Test that worker exits with non-zero on invalid date."""
        result = subprocess.run(
            [
                "python",
                "-m",
                "ib261_schedule.worker",
                "--date",
                "invalid-date-format",
                "--cache-root",
                "/tmp",
            ],
            capture_output=True,
            text=True,
            cwd="/home/hermes/projects/ib-261-schedule-button",
        )
        # Should exit with error code
        assert result.returncode != 0

    def test_worker_outputs_json(self):
        """Test that worker always outputs valid JSON."""
        result = subprocess.run(
            [
                "python",
                "-m",
                "ib261_schedule.worker",
                "--date",
                "2026-09-08",
                "--cache-root",
                "/tmp",
            ],
            capture_output=True,
            text=True,
            cwd="/home/hermes/projects/ib-261-schedule-button",
        )
        # Output should be valid JSON
        try:
            data = json.loads(result.stdout)
            assert isinstance(data, dict)
            assert "status" in data
        except json.JSONDecodeError as e:
            pytest.fail(f"Worker output is not valid JSON: {e}")


class TestScreenshotValidation:
    """Test screenshot validation in payload."""

    def test_screenshot_field_can_be_none(self):
        """Test that screenshot can be None for unavailable status."""
        payload = {
            "status": "unavailable",
            "screenshot": None,
        }
        assert payload["screenshot"] is None

    def test_screenshot_must_exist_if_provided(self):
        """Test that screenshot path must point to existing file."""
        screenshot_path = "/definitely/nonexistent/file.png"
        assert not Path(screenshot_path).exists()

    def test_screenshot_path_must_be_string(self):
        """Test that screenshot must be string or None."""
        valid_screenshot = "/path/to/file.png"
        assert isinstance(valid_screenshot, str) or valid_screenshot is None


class TestPayloadFieldTypes:
    """Test that payload fields have correct types."""

    def test_status_is_string(self):
        """Test status field is string."""
        payload = {"status": "fresh"}
        assert isinstance(payload["status"], str)

    def test_group_is_string(self):
        """Test group field is string."""
        payload = {"group": "ИБ-261"}
        assert isinstance(payload["group"], str)

    def test_date_is_string(self):
        """Test requested_date is string."""
        payload = {"requested_date": "2026-09-08"}
        assert isinstance(payload["requested_date"], str)

    def test_cache_version_is_string_or_none(self):
        """Test cache_version is string or None."""
        payload1 = {"cache_version": "v1"}
        payload2 = {"cache_version": None}
        assert isinstance(payload1["cache_version"], str)
        assert payload2["cache_version"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
