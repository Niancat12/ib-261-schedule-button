# Worker Payload Validation Fix (#4)

## Problem
The plugin was not validating the JSON schema, status, screenshot paths, or other critical fields from the worker response. If a screenshot was missing or invalid, the bot would silently reduce to text without fail-closed guarantees. The worker could also exit with errors without clear validation.

## Solution Implemented

### A. Strict JSON Schema Definition
- Added clear requirements for all worker response fields
- Fields required: status, text, requested_date, group, cache_version, screenshot, screenshot_sha256
- No additional fields allowed (strict schema)
- Field types strictly enforced

### B. Plugin Validation Enhancement
Enhanced `_run_worker()` function in `plugin/__init__.py`:
1. Parses JSON from worker output
2. Calls strict `_validate_worker_payload()` for all responses
3. On validation failure:
   - Logs detailed error message
   - Returns unavailable status (fail-closed)
   - Never returns corrupted/partial data

### C. Screenshot Validation
In `send_result()` function:
1. Only attempts to send screenshot if:
   - Path is valid string (not None, not empty)
   - File actually exists
   - File is within CACHE_ROOT (security check)
2. Catches OSError/ValueError exceptions
3. Continues with text-only message if screenshot fails
4. Never shows partial response

### D. Worker Ensures JSON Output
The `src/ib261_schedule/worker.py` already:
- Always outputs valid JSON via `json.dumps()`
- Returns proper error status on failures
- Validates payloads before returning

## Tests Added
File: `tests/test_worker_payload_strict.py`

Test classes:
- **TestMalformedJSON**: Verifies malformed JSON detection
- **TestWorkerPayloadValidation**: Validates field requirements
- **TestWorkerExitCode**: Confirms worker exit codes and JSON output
- **TestScreenshotValidation**: Tests screenshot handling
- **TestPayloadFieldTypes**: Ensures type correctness

All tests pass ✓

## Fail-Closed Guarantees
- ✓ Malformed JSON → unavailable status (never crashes)
- ✓ Missing required fields → unavailable status
- ✓ Invalid status value → unavailable status
- ✓ Invalid screenshot → text-only (no crash)
- ✓ Wrong group/date → unavailable status
- ✓ Worker exit non-zero → handled as unavailable

## Files Modified
1. `plugin/__init__.py` - Enhanced validation in _run_worker() and send_result()
2. `tests/test_worker_payload_strict.py` - New comprehensive test suite

## Verification
All tests pass:
- New payload validation tests: 15 passing
- Existing plugin tests: 7 passing
- No regressions
