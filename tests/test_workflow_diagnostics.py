from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "update-schedule.yml"
CAPTURE = Path(__file__).parents[1] / "src" / "ib261_schedule" / "browser_capture.py"


def test_workflow_uses_one_diagnostics_path_and_finalizes_it_unconditionally():
    text = WORKFLOW.read_text(encoding="utf-8")
    capture_text = CAPTURE.read_text(encoding="utf-8")

    assert "IB261_DIAGNOSTICS_DIR: ${{ runner.temp }}/ib261-diagnostics" in text
    assert 'mkdir -p "$IB261_DIAGNOSTICS_DIR"' in text
    assert "SCHEDULE_DIAGNOSTIC_DIR" not in text
    assert 'os.environ.get("IB261_DIAGNOSTICS_DIR", "")' in capture_text
    assert "path: ${{ env.IB261_DIAGNOSTICS_DIR }}" in text
    assert "if-no-files-found: warn" in text
    assert "- name: Finalize browser diagnostics" in text
    assert "workflow-context.txt" in text
    assert "continue-on-error" not in text


def test_workflow_context_does_not_include_secret_values():
    text = WORKFLOW.read_text(encoding="utf-8")
    context_start = text.index("- name: Finalize browser diagnostics")
    context_end = text.index("- name: Upload browser diagnostics", context_start)
    context_step = text[context_start:context_end]

    assert "secrets." not in context_step
    assert "Authorization" not in context_step
    assert "cookie" not in context_step.casefold()
