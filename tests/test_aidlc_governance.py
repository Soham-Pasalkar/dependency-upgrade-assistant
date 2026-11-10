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
