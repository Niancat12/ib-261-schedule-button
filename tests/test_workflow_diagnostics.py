from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "update-schedule.yml"
CAPTURE = Path(__file__).parents[1] / "src" / "ib261_schedule" / "browser_capture.py"


def test_workflow_uses_one_diagnostics_path_and_finalizes_it_unconditionally():
    text = WORKFLOW.read_text(encoding="utf-8")
    capture_text = CAPTURE.read_text(encoding="utf-8")

    assert text.count("IB261_DIAGNOSTICS_DIR: ${{ runner.temp }}/ib261-diagnostics") == 3
    assert 'mkdir -p "$IB261_DIAGNOSTICS_DIR"' in text
    assert "SCHEDULE_DIAGNOSTIC_DIR" not in text
    assert 'os.environ.get("IB261_DIAGNOSTICS_DIR", "")' in capture_text
    assert "path: ${{ runner.temp }}/ib261-diagnostics" in text
    assert "if-no-files-found: warn" in text
    assert "- name: Finalize browser diagnostics" in text
    assert "workflow-context.txt" in text
    assert "continue-on-error" not in text

    jobs_prefix = text.split("steps:", 1)[0]
    assert "runner.temp" not in jobs_prefix


def test_workflow_context_does_not_include_secret_values():
    text = WORKFLOW.read_text(encoding="utf-8")
    context_start = text.index("- name: Finalize browser diagnostics")
    context_end = text.index("- name: Upload browser diagnostics", context_start)
    context_step = text[context_start:context_end]

    assert "secrets." not in context_step
    assert "Authorization" not in context_step
    assert "cookie" not in context_step.casefold()


def test_workflow_validates_and_stages_root_publication_before_diff():
    text = WORKFLOW.read_text(encoding="utf-8")
    verify = text.index("- name: Verify published snapshot files")
    commit = text.index("- name: Commit only changed verified publication")
    section = text[verify:commit]
    assert "test -s published-schedules/schedule.json" in section
    assert "test -s published-schedules/schedule.png" in section
    assert "scripts/validate_publication.py" in section
    commit_section = text[commit:]
    assert "git add published-schedules/schedule.json published-schedules/schedule.png published-schedules" in commit_section
    assert "git diff --cached --quiet" in commit_section
    assert "git push origin HEAD:main" in commit_section
    assert "git diff --quiet" not in commit_section
    assert "exit 0" not in commit_section
    assert "git ls-files --error-unmatch published-schedules/schedule.json" in commit_section
    assert "git ls-files --error-unmatch published-schedules/schedule.png" in commit_section


def test_fetch_uses_workspace_root_for_publication():
    fetch = (Path(__file__).parents[1] / "scripts" / "fetch_schedule.py").read_text(encoding="utf-8")
    assert 'os.environ.get("GITHUB_WORKSPACE"' in fetch
    assert 'WORKSPACE / "published-schedules"' in fetch
