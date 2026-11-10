"""AI-DLC Governance and Integrity Verification Tests.

These tests verify the state machine, event hash chain, and configuration audit trail
enforced by aidlc.py.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

AIDLC_PY = Path(__file__).resolve().parent.parent / ".agents" / "skills" / "ai-dlc-engineering" / "scripts" / "aidlc.py"


def run_aidlc(args, root: Path, expect_code: int = 0):
    cmd = [sys.executable, str(AIDLC_PY), "--root", str(root)] + args
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == expect_code, f"Expected code {expect_code}, got {res.returncode}.\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    return res


def test_valid_strict_mode_transition(tmp_path):
    """Verifies that config set strict_approvals creates a verifiable config.update event."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    # verify initially passes
    run_aidlc(["verify"], tmp_path, expect_code=0)

    # update config
    res = run_aidlc(["config", "set", "strict_approvals", "true", "--reason", "Enable strict mode"], tmp_path)
    assert "strict_approvals = True" in res.stdout

    # verify passes
    run_aidlc(["verify"], tmp_path, expect_code=0)

    # verify config get
    res = run_aidlc(["config", "get", "strict_approvals"], tmp_path)
    assert "strict_approvals: True" in res.stdout


def test_missing_config_update_event_detected(tmp_path):
    """Verifies that editing state.json config without config.update event is detected by verify."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    state_file = tmp_path / ".aidlc" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))

    # Modify state.json directly
    state["config"]["strict_approvals"] = True
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # verify must fail due to config mismatch (and head check if head wasn't forged)
    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "config 'strict_approvals' mismatch" in res.stdout


def test_tampered_config_event_detected(tmp_path):
    """Verifies that tampering with an event's data causes hash mismatch."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    run_aidlc(["config", "set", "strict_approvals", "true", "--reason", "Legit change"], tmp_path)
    events_file = tmp_path / ".aidlc" / "events.jsonl"
    lines = events_file.read_text(encoding="utf-8").splitlines()

    # Tamper with the config event data
    ev = json.loads(lines[-1])
    ev["data"]["reason"] = "Tampered reason"
    lines[-1] = json.dumps(ev)
    events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "content hash mismatch" in res.stdout


def test_state_file_head_modified_without_event_detected(tmp_path):
    """Verifies that an unrecorded state head modification is detected."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    state_file = tmp_path / ".aidlc" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["head"] = "0" * 64
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "state.json head does not match the last event" in res.stdout


def test_broken_event_hash_chain_detected(tmp_path):
    """Verifies that breaking the prev hash pointer in events.jsonl is detected."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    run_aidlc(["config", "set", "strict_approvals", "true", "--reason", "Legit change"], tmp_path)
    events_file = tmp_path / ".aidlc" / "events.jsonl"
    lines = events_file.read_text(encoding="utf-8").splitlines()

    ev = json.loads(lines[-1])
    ev["prev"] = "1" * 64
    lines[-1] = json.dumps(ev)
    events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "broken chain" in res.stdout or "content hash mismatch" in res.stdout


def test_direct_strict_approval_bypass_prevented(tmp_path):
    """Verifies that directly setting state.json config without events is rejected."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    state_file = tmp_path / ".aidlc" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))

    # Turn strict approvals off in state.json directly
    state["config"]["strict_approvals"] = False
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # If project was init with strict=False, setting to True is caught:
    state["config"]["strict_approvals"] = True
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "config 'strict_approvals' mismatch" in res.stdout


def test_historical_old_value_mismatch_detected(tmp_path):
    """Verifies that if an event records an incorrect 'old' value, verify flags an error."""
    import hashlib
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    
    events_file = tmp_path / ".aidlc" / "events.jsonl"
    lines = events_file.read_text(encoding="utf-8").splitlines()
    ev1 = json.loads(lines[0])
    
    # Forge event 2 with mismatched 'old' value (recorded old=True, but preceding was False)
    body = {
        "seq": 2,
        "ts": "2026-11-10T12:00:00Z",
        "actor": "agent",
        "type": "config.update",
        "data": {
            "param": "strict_approvals",
            "old": True,
            "new": True,
            "reason": "Mismatched old param"
        },
        "prev": ev1["hash"]
    }
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body["hash"] = hashlib.sha256(canon.encode()).hexdigest()
    
    events_file.write_text(lines[0] + "\n" + json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    
    state_file = tmp_path / ".aidlc" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["head"] = body["hash"]
    state["config"]["strict_approvals"] = True
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    
    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "config.update 'old' value mismatch for 'strict_approvals'" in res.stdout


def test_agent_cannot_disable_strict_mode_non_interactively(tmp_path):
    """Verifies that disabling strict approvals non-interactively fails closed."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3", "--strict"], tmp_path)
    res = run_aidlc(
        ["config", "set", "strict_approvals", "false", "--reason", "Attempted disable"],
        tmp_path,
        expect_code=1
    )
    assert "strict approvals can only be disabled by a human in an interactive terminal" in (res.stdout + res.stderr)


def test_human_can_disable_strict_mode_interactively(tmp_path, monkeypatch):
    """Verifies that an interactive human confirmation can disable strict mode and records actor human-tty."""
    import sys
    sys.path.insert(0, str(AIDLC_PY.parent))
    import aidlc
    
    run_aidlc(["init", "--project", "TestProj", "--tier", "3", "--strict"], tmp_path)
    
    monkeypatch.setattr(aidlc, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "disable-strict")
    
    class Args:
        root = str(tmp_path)
        action = "set"
        param = "strict_approvals"
        value = "false"
        reason = "Human requested disable"
    
    ret = aidlc.cmd_config(Args())
    assert ret == 0
    
    state = aidlc.load_state(tmp_path)
    assert state["config"]["strict_approvals"] is False
    
    events = aidlc.read_events(tmp_path)
    last_event = events[-1]
    assert last_event["type"] == "config.update"
    assert last_event["actor"] == "human-tty"
    assert last_event["data"]["param"] == "strict_approvals"
    assert last_event["data"]["old"] is True
    assert last_event["data"]["new"] is False
    
    # Verify passes
    run_aidlc(["verify"], tmp_path, expect_code=0)


def test_disabling_strict_mode_with_wrong_confirmation_fails(tmp_path, monkeypatch):
    """Verifies that wrong confirmation text aborts without disabling strict mode."""
    import sys
    sys.path.insert(0, str(AIDLC_PY.parent))
    import aidlc
    
    run_aidlc(["init", "--project", "TestProj", "--tier", "3", "--strict"], tmp_path)
    
    monkeypatch.setattr(aidlc, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    
    class Args:
        root = str(tmp_path)
        action = "set"
        param = "strict_approvals"
        value = "false"
        reason = "Human aborted"
        
    with pytest.raises(aidlc.AidlcError) as excinfo:
        aidlc.cmd_config(Args())
    assert "confirmation failed" in str(excinfo.value)
    
    state = aidlc.load_state(tmp_path)
    assert state["config"]["strict_approvals"] is True


def test_unknown_or_invalid_config_parameter_rejected(tmp_path):
    """Verifies that unknown parameters and non-boolean values are rejected."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    
    # Unknown key
    res = run_aidlc(["config", "set", "arbitrary_key", "true", "--reason", "test"], tmp_path, expect_code=1)
    assert "unknown configuration parameter 'arbitrary_key'" in (res.stdout + res.stderr)
    
    # Non-boolean value
    res2 = run_aidlc(["config", "set", "strict_approvals", "not_a_bool", "--reason", "test"], tmp_path, expect_code=1)
    assert "requires a boolean value" in (res2.stdout + res2.stderr)


def _append_forged_event(tmp_path: Path, event_type: str, data: dict):
    import hashlib
    events_file = tmp_path / ".aidlc" / "events.jsonl"
    lines = events_file.read_text(encoding="utf-8").splitlines()
    prev_hash = json.loads(lines[-1])["hash"] if lines else "0" * 64
    body = {
        "seq": len(lines) + 1,
        "ts": "2026-11-10T15:00:00Z",
        "actor": "agent",
        "type": event_type,
        "data": data,
        "prev": prev_hash
    }
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body["hash"] = hashlib.sha256(canon.encode()).hexdigest()
    lines.append(json.dumps(body, sort_keys=True, separators=(",", ":")))
    events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    state_file = tmp_path / ".aidlc" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["head"] = body["hash"]
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return body


def test_historical_missing_old_field_rejected(tmp_path):
    """Verifies that a historical config.update event lacking 'old' is rejected."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    _append_forged_event(tmp_path, "config.update", {
        "param": "strict_approvals",
        "new": True,
        "reason": "Missing old field"
    })
    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "missing or null 'old' value" in (res.stdout + res.stderr)


def test_historical_null_old_field_rejected(tmp_path):
    """Verifies that a historical config.update event with null 'old' is rejected."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    _append_forged_event(tmp_path, "config.update", {
        "param": "strict_approvals",
        "old": None,
        "new": True,
        "reason": "Null old field"
    })
    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "missing or null 'old' value" in (res.stdout + res.stderr)


def test_historical_unknown_parameter_rejected(tmp_path):
    """Verifies that a historical config.update event with an unknown parameter is rejected."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    _append_forged_event(tmp_path, "config.update", {
        "param": "unrecognized_flag",
        "old": False,
        "new": True,
        "reason": "Unknown parameter"
    })
    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "unknown parameter 'unrecognized_flag'" in (res.stdout + res.stderr)


def test_historical_invalid_new_type_rejected(tmp_path):
    """Verifies that a historical config.update event with a non-boolean new value is rejected."""
    run_aidlc(["init", "--project", "TestProj", "--tier", "3"], tmp_path)
    _append_forged_event(tmp_path, "config.update", {
        "param": "strict_approvals",
        "old": False,
        "new": "enabled",
        "reason": "String instead of boolean"
    })
    res = run_aidlc(["verify"], tmp_path, expect_code=1)
    assert "must be a boolean" in (res.stdout + res.stderr)


# ---------------------------------------------------------------------------
# Autonomous Milestone Acceptance (G4) Tests
# ---------------------------------------------------------------------------

def _setup_project_at_m1_submitted(tmp_path):
    import shutil
    repo_root = Path(__file__).resolve().parent.parent
    run_aidlc(["init", "--project", "TestProj", "--tier", "2"], tmp_path)
    docs_dest = tmp_path / "docs" / "aidlc"
    docs_dest.mkdir(parents=True, exist_ok=True)
    for name in ("requirements.md", "architecture.md", "threat-model.md"):
        shutil.copy(repo_root / "docs" / "aidlc" / name, docs_dest / name)

    (docs_dest / "milestone-plan.md").write_text(
        "# Milestone plan: TestProj\n\n"
        "## Milestones\n"
        "| # | Milestone | Depends on | Acceptance criteria | Test approach |\n"
        "|---|---|---|---|---|\n"
        "| M1 | Core slice | none | Unit tests pass | automated pytest |\n\n"
        "## Test strategy\nRun automated pytest suite.\n\n"
        "## Risks\nNone identified.\n",
        encoding="utf-8"
    )

    run_aidlc(["gate", "present", "G1", "--summary", "Requirements"], tmp_path)
    run_aidlc(["gate", "approve", "G1", "--note", "Approved G1"], tmp_path)
    run_aidlc(["gate", "present", "G2", "--summary", "Design"], tmp_path)
    run_aidlc(["gate", "approve", "G2", "--note", "Approved G2"], tmp_path)
    run_aidlc(["milestone", "add", "M1", "--title", "Core slice", "--accept", "Pass unit tests"], tmp_path)
    run_aidlc(["gate", "present", "G3", "--summary", "Plan"], tmp_path)
    run_aidlc(["gate", "approve", "G3", "--note", "Approved G3"], tmp_path)
    run_aidlc(["milestone", "start", "M1"], tmp_path)
    run_aidlc(["milestone", "evidence", "M1", "--cmd", "pytest", "--result", "1 passed"], tmp_path)
    run_aidlc(["milestone", "verify", "M1", "--criterion", "1", "--proof", "pytest passes"], tmp_path)
    
    report_path = docs_dest / "reports" / "M1.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "# Milestone report: M1 Core slice\n\n"
        "## Summary\nM1 is complete.\n\n"
        "## Changes\n- Created core slice\n\n"
        "## Tests run\n| Command | Result |\n|---|---|\n| pytest | 1 passed |\n\n"
        "## Acceptance criteria\n| # | Criterion | Status | Evidence |\n|---|---|---|---|\n| 1 | Pass unit tests | PASS | 1 passed |\n\n"
        "## Lens review\n- Architecture: pass\n- Security: pass\n- QA: pass\n- UX: pass\n- SRE: pass\n\n"
        "## Known limitations\nNone.\n\n"
        "## Deviations\nNone.\n",
        encoding="utf-8"
    )
    run_aidlc(["milestone", "submit", "M1"], tmp_path)


def test_milestone_accept_success_transition(tmp_path):
    """Verifies that milestone accept records an agent acceptance event and marks milestone done."""
    _setup_project_at_m1_submitted(tmp_path)
    
    # Milestone is awaiting_approval
    status_res = run_aidlc(["status", "--json"], tmp_path)
    state = json.loads(status_res.stdout)
    m1 = next(m for m in state["milestones"] if m["id"] == "M1")
    assert m1["status"] == "awaiting_approval"

    # Accept milestone via autonomous agent action
    res = run_aidlc(["milestone", "accept", "M1", "--note", "Agent verified all tests and criteria"], tmp_path)
    assert "M1 accepted by agent" in res.stdout

    # Verify state is done
    status_res = run_aidlc(["status", "--json"], tmp_path)
    state = json.loads(status_res.stdout)
    m1 = next(m for m in state["milestones"] if m["id"] == "M1")
    assert m1["status"] == "done"
    assert m1["approval"]["actor"] == "agent"
    assert m1["approval"]["type"] == "agent_acceptance"

    # Verify event log contains milestone.accept event with actor agent
    events_file = tmp_path / ".aidlc" / "events.jsonl"
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    accept_ev = events[-1]
    assert accept_ev["type"] == "milestone.accept"
    assert accept_ev["actor"] == "agent"
    assert accept_ev["data"]["id"] == "M1"

    # Verify hash chain and invariants pass
    run_aidlc(["verify"], tmp_path, expect_code=0)


def test_milestone_accept_requires_awaiting_approval(tmp_path):
    """Verifies that milestone accept cannot be run on a milestone that is in_progress or planned."""
    import shutil
    repo_root = Path(__file__).resolve().parent.parent
    run_aidlc(["init", "--project", "TestProj", "--tier", "2"], tmp_path)
    docs_dest = tmp_path / "docs" / "aidlc"
    docs_dest.mkdir(parents=True, exist_ok=True)
    for name in ("requirements.md", "architecture.md", "threat-model.md"):
        shutil.copy(repo_root / "docs" / "aidlc" / name, docs_dest / name)

    (docs_dest / "milestone-plan.md").write_text(
        "# Milestone plan: TestProj\n\n"
        "## Milestones\n"
        "| # | Milestone | Depends on | Acceptance criteria | Test approach |\n"
        "|---|---|---|---|---|\n"
        "| M1 | Core slice | none | Unit tests pass | automated pytest |\n\n"
        "## Test strategy\nRun automated pytest suite.\n\n"
        "## Risks\nNone identified.\n",
        encoding="utf-8"
    )

    run_aidlc(["gate", "present", "G1", "--summary", "Reqs"], tmp_path)
    run_aidlc(["gate", "approve", "G1", "--note", "Approved G1"], tmp_path)
    run_aidlc(["gate", "present", "G2", "--summary", "Design"], tmp_path)
    run_aidlc(["gate", "approve", "G2", "--note", "Approved G2"], tmp_path)
    run_aidlc(["milestone", "add", "M1", "--title", "Core slice", "--accept", "Pass"], tmp_path)
    run_aidlc(["gate", "present", "G3", "--summary", "Plan"], tmp_path)
    run_aidlc(["gate", "approve", "G3", "--note", "Approved G3"], tmp_path)

    # Attempt accept on planned milestone
    res = run_aidlc(["milestone", "accept", "M1", "--note", "Premature accept"], tmp_path, expect_code=1)
    assert "not awaiting_approval" in (res.stdout + res.stderr)


def test_milestone_accept_reopen_invalidates_acceptance(tmp_path):
    """Verifies that reopening an accepted milestone resets approval and criteria."""
    _setup_project_at_m1_submitted(tmp_path)
    run_aidlc(["milestone", "accept", "M1", "--note", "Agent accepted M1"], tmp_path)
    
    # Reopen M1
    run_aidlc(["milestone", "reopen", "M1", "--note", "Regression discovered"], tmp_path)
    status_res = run_aidlc(["status", "--json"], tmp_path)
    state = json.loads(status_res.stdout)
    m1 = next(m for m in state["milestones"] if m["id"] == "M1")
    assert m1["status"] == "in_progress"
    assert m1["approval"] is None
    assert all(not c["verified"] for c in m1["criteria"])

    # Audit log verification still passes
    run_aidlc(["verify"], tmp_path, expect_code=0)


def test_milestone_accept_requires_note_and_report_validation(tmp_path):
    """Verifies that milestone accept requires note and validates milestone report."""
    _setup_project_at_m1_submitted(tmp_path)

    # Empty/short note rejected
    res = run_aidlc(["milestone", "accept", "M1", "--note", "bad"], tmp_path, expect_code=1)
    assert "--note is required" in (res.stdout + res.stderr)

    # Corrupt milestone report with missing sections
    report_path = tmp_path / "docs" / "aidlc" / "reports" / "M1.md"
    report_path.write_text("# Invalid Report\n\nNo required sections.", encoding="utf-8")

    res = run_aidlc(["milestone", "accept", "M1", "--note", "Valid note but invalid report"], tmp_path, expect_code=1)
    assert "acceptance prerequisites not met" in (res.stdout + res.stderr) or "cannot accept" in (res.stdout + res.stderr)





