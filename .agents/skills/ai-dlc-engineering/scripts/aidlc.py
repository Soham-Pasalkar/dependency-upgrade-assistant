#!/usr/bin/env python3
"""aidlc.py - state, gates and audit log for the AI-DLC Engineering skill.

stdlib only (Python 3.9+). State lives in <project>/.aidlc/:
  state.json    current state (machine readable)
  events.jsonl  append-only, hash-chained audit log of every transition
  STATE.md      human-readable render (generated; do not edit)

The CLI refuses illegal transitions (e.g. starting a milestone before G3 is
approved, submitting without evidence). `verify` cross-checks state.json
against the event log, so hand-edited approvals are detected.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_artifacts as va  # noqa: E402

SCHEMA_VERSION = 1
STATE_DIRNAME = ".aidlc"
STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
GENESIS = "0" * 64
GATE_ORDER = ["G1", "G2", "G3", "G5"]
GATE_NAMES = {"G1": "Requirements", "G2": "Architecture and threat model",
              "G3": "Milestone plan", "G5": "Release"}
GATE_ARTIFACTS = {"G1": ["requirements"], "G2": ["architecture", "threat-model"],
                  "G3": ["milestone-plan"], "G5": ["completion-report"]}
MS_STATES = {"planned", "in_progress", "awaiting_approval", "done", "blocked"}
OPEN_STATES = {"in_progress", "awaiting_approval"}
SCAFFOLD = {"requirements": "requirements.md", "architecture": "architecture.md",
            "threat-model": "threat-model.md", "milestone-plan": "milestone-plan.md",
            "completion-report": "completion-report.md"}
MAX_REWORK = 3


class AidlcError(Exception):
    pass


# --------------------------------------------------------------------------- io
def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_root(start=None):
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / STATE_DIRNAME / STATE_FILE).is_file():
            return d
    return None


def require_root(args) -> Path:
    if getattr(args, "root", None):
        root = Path(args.root).resolve()
        if not (root / STATE_DIRNAME / STATE_FILE).is_file():
            raise AidlcError(f"no {STATE_DIRNAME}/{STATE_FILE} in {root}")
        return root
    root = find_root()
    if root is None:
        raise AidlcError("no .aidlc/state.json found; run `aidlc.py init --project NAME` "
                         "in the project root")
    return root


def load_state(root: Path) -> dict:
    return json.loads((root / STATE_DIRNAME / STATE_FILE).read_text(encoding="utf-8"))


def save_state(root: Path, state: dict) -> None:
    d = root / STATE_DIRNAME
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, d / STATE_FILE)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def read_events(root: Path) -> list:
    p = root / STATE_DIRNAME / EVENTS_FILE
    if not p.is_file():
        return []
    out = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise AidlcError(f"{EVENTS_FILE}:{n}: invalid JSON ({e})")
    return out


def append_event(root: Path, etype: str, data: dict, actor: str) -> dict:
    events = read_events(root)
    prev = events[-1]["hash"] if events else GENESIS
    body = {"seq": len(events) + 1, "ts": now(), "actor": actor, "type": etype,
            "data": data, "prev": prev}
    body["hash"] = hashlib.sha256(_canon(body).encode()).hexdigest()
    with open(root / STATE_DIRNAME / EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(_canon(body) + "\n")
    return body


def default_actor() -> str:
    return os.environ.get("AIDLC_ACTOR", "agent")


def derive_phase(state: dict) -> str:
    g = state["gates"]
    ms = state["milestones"]
    if g["G5"]["status"] == "approved":
        return "released"
    if g["G3"]["status"] == "approved":
        return "operations" if ms and all(m["status"] == "done" for m in ms) else "construction"
    return "inception"


def commit(root: Path, state: dict, etype: str, data: dict, actor: str | None = None) -> dict:
    ev = append_event(root, etype, data, actor or default_actor())
    state["head"] = ev["hash"]
    state["phase"] = derive_phase(state)
    state["updated"] = now()
    save_state(root, state)
    (root / STATE_DIRNAME / "STATE.md").write_text(render_markdown(state), encoding="utf-8")
    return ev


# ----------------------------------------------------------------------- helpers
def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def approval_actor(state: dict, what: str, note: str | None) -> str:
    """Enforce note (and a human TTY confirmation in strict mode)."""
    if state["config"]["strict_approvals"]:
        if not is_interactive():
            raise AidlcError(
                f"strict approvals are on: only the human can approve {what}. Ask the user "
                f"to run this exact command in their own terminal:\n  python <skill-dir>/scripts/"
                f"aidlc.py {what_to_cmd(what)}")
        answer = input(f"Type 'approve' to approve {what}: ").strip().lower()
        if answer != "approve":
            raise AidlcError("not approved")
        return "human-tty"
    if not note or len(note.strip()) < 3:
        raise AidlcError("--note is required: quote the user's approving message")
    return default_actor()


def what_to_cmd(what: str) -> str:
    if what.startswith("gate "):
        return f"gate approve {what.split()[1]}"
    return f"milestone approve {what.split()[-1]}"


def get_ms(state: dict, mid: str) -> dict:
    for m in state["milestones"]:
        if m["id"] == mid:
            return m
    raise AidlcError(f"unknown milestone {mid}")


def gates_approved(state: dict, *gids) -> bool:
    return all(state["gates"][g]["status"] == "approved" for g in gids)


def open_milestones(state: dict) -> list:
    return [m for m in state["milestones"] if m["status"] in OPEN_STATES]


def unverified(m: dict) -> list:
    return [i for i, c in enumerate(m["criteria"], 1) if not c["verified"]]


def reset_gates_from(state: dict, gid: str, root: Path, note: str, only_if_set=True) -> list:
    changed = []
    for g in GATE_ORDER[GATE_ORDER.index(gid):]:
        if state["gates"][g]["status"] != "pending":
            state["gates"][g].update(status="pending", note=note, at=now())
            changed.append(g)
            append_event(root, "gate.reset", {"gate": g, "note": note}, default_actor())
    return changed


def scaffold_files(root: Path, names, force=False) -> list:
    tdir = Path(__file__).resolve().parent.parent / "templates"
    made = []
    for name in names:
        src = tdir / SCAFFOLD[name]
        dest = root / va.ARTIFACTS[name]["path"]
        if dest.exists() and not force:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        made.append(str(dest.relative_to(root)))
    return made


def scaffold_report(root: Path, m: dict) -> str | None:
    dest = va.artifact_path(root, "milestone-report", m["id"])
    if dest.exists():
        return None
    tpl = (Path(__file__).resolve().parent.parent / "templates" / "milestone-report.md")
    rows = "\n".join(f"| {i} | {c['text']} | {{{{pass_or_fail}}}} | {{{{evidence}}}} |"
                     for i, c in enumerate(m["criteria"], 1))
    text = (tpl.read_text(encoding="utf-8")
            .replace("{{milestone_id}}", m["id"]).replace("{{milestone_title}}", m["title"])
            .replace("{{criteria_rows}}", rows))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return str(dest.relative_to(root))


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------ next action
def next_action(state: dict) -> str:
    g, ms = state["gates"], state["milestones"]
    steps = {"G1": "Resolve blocking questions with the user, draft docs/aidlc/requirements.md",
             "G2": "Draft docs/aidlc/architecture.md and threat-model.md",
             "G3": "Draft docs/aidlc/milestone-plan.md and register each milestone "
                   "(`milestone add`)"}
    for gid in ("G1", "G2", "G3"):
        st = g[gid]["status"]
        if st in ("pending", "changes_requested"):
            return (f"{steps[gid]}; run the lens review; then `gate present {gid}`."
                    + (f" Last feedback: {g[gid]['note']}" if st == "changes_requested" else ""))
        if st == "presented":
            return f"WAIT: {gid} is presented and needs the user's approval. Do not continue."
    awaiting = [m for m in ms if m["status"] == "awaiting_approval"]
    if awaiting:
        return f"WAIT: {awaiting[0]['id']} is submitted and needs the user's approval (G4)."
    active = [m for m in ms if m["status"] == "in_progress"]
    if active:
        m = active[0]
        todo = unverified(m)
        if m.get("rework", 0) >= MAX_REWORK:
            return (f"STOP: {m['id']} was rejected {m['rework']} times. Pause, diagnose with "
                    "the user, and consider `gate reset G3` (replan).")
        return (f"Continue {m['id']}: implement, test, log `milestone evidence`, then "
                f"`milestone verify` criteria {todo or 'all done'}, fill its report, "
                f"`milestone submit {m['id']}`.")
    done = {m["id"] for m in ms if m["status"] == "done"}
    ready = [m for m in ms if m["status"] == "planned" and set(m["depends_on"]) <= done]
    if ready:
        return f"Start {ready[0]['id']}: `milestone start {ready[0]['id']}`."
    if ms and len(done) == len(ms):
        st = g["G5"]["status"]
        if st in ("pending", "changes_requested"):
            return ("Run the operations checklist (references/operations.md), write "
                    "docs/aidlc/completion-report.md, then `gate present G5`.")
        if st == "presented":
            return "WAIT: G5 is presented and needs the user's approval."
        return "Released. New work needs a new intent: `milestone add --replan`."
    blocked = [m["id"] for m in ms if m["status"] == "blocked"]
    return f"No startable milestone. Blocked: {blocked or 'none'}; check dependencies."


# ----------------------------------------------------------------------- render
def render_markdown(state: dict) -> str:
    lines = [f"# AI-DLC state: {state['project']}", "",
             "_Generated by aidlc.py from state.json. Do not edit by hand._", "",
             f"- Tier: {state['tier']}  |  Mode: {state['mode']}  |  Phase: {state['phase']}",
             f"- Strict approvals: {'on' if state['config']['strict_approvals'] else 'off'}",
             f"- Updated: {state['updated']}", "", "## Gates", ""]
    for gid in GATE_ORDER:
        g = state["gates"][gid]
        extra = f" ({g['at']}; {g['note']})" if g["note"] else ""
        lines.append(f"- {gid} {g['name']}: **{g['status']}**{extra}")
    lines += ["", "## Milestones", ""]
    if not state["milestones"]:
        lines.append("_none registered_")
    for m in state["milestones"]:
        done_c = sum(c["verified"] for c in m["criteria"])
        dep = f" (after {', '.join(m['depends_on'])})" if m["depends_on"] else ""
        lines.append(f"- {m['id']} {m['title']}{dep}: **{m['status']}**, criteria "
                     f"{done_c}/{len(m['criteria'])}, evidence {len(m['evidence'])}, "
                     f"rework {m.get('rework', 0)}")
    lines += ["", "## Notes", ""]
    open_notes = [n for n in state["notes"]]
    if not open_notes:
        lines.append("_none_")
    for n in open_notes:
        lines.append(f"- {n['id']} [{n['kind']}] {'(closed) ' if n['status'] == 'closed' else ''}"
                     f"{n['text']}")
    lines += ["", f"## Next action", "", next_action(state), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------- commands
def cmd_init(args):
    root = Path(args.root).resolve() if args.root else Path.cwd()
    if (root / STATE_DIRNAME / STATE_FILE).exists():
        raise AidlcError("already initialized")
    (root / STATE_DIRNAME).mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": SCHEMA_VERSION, "project": args.project, "tier": args.tier,
        "mode": args.mode, "phase": "inception",
        "config": {"strict_approvals": bool(args.strict),
                   "hook_enforcement": not args.no_hook},
        "created": now(), "updated": now(),
        "gates": {g: {"name": GATE_NAMES[g], "status": "pending", "note": None, "at": None}
                  for g in GATE_ORDER},
        "milestones": [], "notes": [], "head": None}
    commit(root, state, "project.init", {"project": args.project, "tier": args.tier,
                                         "mode": args.mode, "strict": bool(args.strict)})
    made = scaffold_files(root, list(SCAFFOLD)) if args.scaffold else []
    print(f"initialized AI-DLC in {root}/{STATE_DIRNAME} (tier {args.tier}, {args.mode})")
    for m in made:
        print(f"  scaffolded {m}")
    if args.tier >= 3 and not args.strict:
        print("note: tier 3 is security-sensitive; consider re-initialising with --strict")
    print("commit .aidlc/ to git: it is the audit trail.")
    print("next:", next_action(load_state(root)))
    return 0


def cmd_scaffold(args):
    root = require_root(args)
    bad = [n for n in args.names if n not in SCAFFOLD]
    if bad:
        raise AidlcError(f"unknown artifact(s) {bad}; choose from {list(SCAFFOLD)}")
    made = scaffold_files(root, args.names or list(SCAFFOLD), args.force)
    print("\n".join(f"scaffolded {m}" for m in made) or "nothing to do (files exist; use --force)")
    return 0


def cmd_status(args):
    root = require_root(args)
    state = load_state(root)
    if args.json:
        print(json.dumps({**state, "next": next_action(state)}, indent=2))
        return 0
    print(f"{state['project']}  tier {state['tier']}  {state['mode']}  phase: {state['phase']}"
          f"  strict: {'on' if state['config']['strict_approvals'] else 'off'}")
    print("gates: " + " | ".join(f"{g} {state['gates'][g]['status']}" for g in GATE_ORDER))
    for m in state["milestones"]:
        ok = sum(c["verified"] for c in m["criteria"])
        print(f"  {m['id']:<4}{m['status']:<18}{m['title']}  "
              f"[criteria {ok}/{len(m['criteria'])}, evidence {len(m['evidence'])}]")
    print("next:", next_action(state))
    return 0


def cmd_next(args):
    print(next_action(load_state(require_root(args))))
    return 0


def run_artifact_checks(root, state, gid):
    errs, warns = [], []
    for key in GATE_ARTIFACTS[gid]:
        e, w = va.check_artifact(root, key, tier=state["tier"])
        errs += e
        warns += w
    if gid == "G3":
        errs += va.check_plan_against_state(root, state)
    return errs, warns


def cmd_gate(args):
    root = require_root(args)
    state = load_state(root)
    gid = args.gate.upper()
    if gid == "G4":
        raise AidlcError("G4 is per milestone: use `milestone submit` / `milestone approve`")
    if gid not in GATE_NAMES:
        raise AidlcError(f"unknown gate {gid}; valid: {', '.join(GATE_ORDER)}")
    g = state["gates"][gid]
    act = args.action
    if act == "present":
        idx = GATE_ORDER.index(gid)
        if idx and not gates_approved(state, *GATE_ORDER[:idx]):
            raise AidlcError(f"earlier gates must be approved first: {GATE_ORDER[:idx]}")
        if g["status"] == "approved":
            raise AidlcError(f"{gid} already approved; use `gate reset {gid}` to reopen")
        if gid in ("G3", "G5") and not state["milestones"]:
            raise AidlcError("no milestones registered")
        if gid == "G5" and any(m["status"] != "done" for m in state["milestones"]):
            raise AidlcError("all milestones must be done before presenting G5")
        errs, warns = run_artifact_checks(root, state, gid)
        for w in warns:
            print("WARN ", w)
        if errs:
            for e in errs:
                print("ERROR", e)
            raise AidlcError(f"cannot present {gid}: fix the artifact problems above")
        g.update(status="presented", at=now(), note=args.summary)
        commit(root, state, "gate.present", {"gate": gid, "summary": args.summary})
        print(f"{gid} ({g['name']}) presented. STOP: present the summary to the user and wait "
              "for explicit approval before doing anything else.")
    elif act == "approve":
        if g["status"] != "presented":
            raise AidlcError(f"{gid} is '{g['status']}'; it must be presented first")
        actor = approval_actor(state, f"gate {gid}", args.note)
        g.update(status="approved", at=now(), note=args.note or "approved at terminal")
        commit(root, state, "gate.approve", {"gate": gid, "note": g["note"]}, actor)
        print(f"{gid} approved by {actor}.\nnext:", next_action(state))
    elif act == "reject":
        if g["status"] != "presented":
            raise AidlcError(f"{gid} is not presented")
        if not args.note:
            raise AidlcError("--note is required: record what the user wants changed")
        g.update(status="changes_requested", at=now(), note=args.note)
        commit(root, state, "gate.reject", {"gate": gid, "note": args.note})
        print(f"{gid}: changes requested.\nnext:", next_action(state))
    elif act == "reset":
        if not args.note:
            raise AidlcError("--note is required: say why the gate is being reopened")
        changed = reset_gates_from(state, gid, root, args.note)
        commit(root, state, "gate.reset.done", {"gate": gid, "reset": changed, "note": args.note})
        print(f"reset: {changed or 'nothing to reset'}")
    return 0


def cmd_milestone(args):
    root = require_root(args)
    state = load_state(root)
    act = args.action
    mid = args.id.upper()
    if act == "add":
        if not re.match(r"^M\d+$", mid):
            raise AidlcError("milestone id must look like M1, M2, ...")
        if any(m["id"] == mid for m in state["milestones"]):
            raise AidlcError(f"{mid} already exists")
        if not args.title or not args.accept:
            raise AidlcError("--title and at least one --accept criterion are required")
        deps = [d.strip().upper() for d in (args.depends or "").split(",") if d.strip()]
        for d in deps:
            get_ms(state, d)
        if state["gates"]["G3"]["status"] == "approved" and not args.replan:
            raise AidlcError("plan (G3) is approved; pass --replan to change it. This "
                             "resets G3 and G5 and needs fresh approval.")
        m = {"id": mid, "title": args.title, "depends_on": deps, "status": "planned",
             "criteria": [{"text": t, "verified": False, "proof": None} for t in args.accept],
             "evidence": [], "approval": None, "rework": 0, "started": None, "note": None}
        state["milestones"].append(m)
        commit(root, state, "milestone.add", {"id": mid, "title": args.title,
                                              "depends_on": deps, "criteria": args.accept})
        if state["gates"]["G3"]["status"] in ("approved", "presented"):
            reset_gates_from(state, "G3", root, f"replan: added {mid}")
            commit(root, state, "gate.reset.done", {"gate": "G3", "note": f"replan: added {mid}"})
            print("G3/G5 reset: the plan changed and needs re-approval.")
        print(f"added {mid}: {args.title}")
        return 0
    m = get_ms(state, mid)
    if act == "remove":
        if m["status"] != "planned":
            raise AidlcError("only planned milestones can be removed")
        if any(mid in x["depends_on"] for x in state["milestones"]):
            raise AidlcError("another milestone depends on it")
        if state["gates"]["G3"]["status"] == "approved" and not args.replan:
            raise AidlcError("plan (G3) is approved; pass --replan")
        state["milestones"].remove(m)
        commit(root, state, "milestone.remove", {"id": mid})
        if state["gates"]["G3"]["status"] in ("approved", "presented"):
            reset_gates_from(state, "G3", root, f"replan: removed {mid}")
            commit(root, state, "gate.reset.done", {"gate": "G3", "note": f"replan: removed {mid}"})
        print(f"removed {mid}")
    elif act == "start":
        if not gates_approved(state, "G1", "G2", "G3"):
            raise AidlcError("G1, G2 and G3 must all be approved before construction")
        if open_milestones(state):
            raise AidlcError(f"{open_milestones(state)[0]['id']} is still open; one at a time")
        if m["status"] != "planned":
            raise AidlcError(f"{mid} is '{m['status']}', not planned")
        not_done = [d for d in m["depends_on"] if get_ms(state, d)["status"] != "done"]
        if not_done:
            raise AidlcError(f"dependencies not done: {not_done}")
        m.update(status="in_progress", started=now())
        commit(root, state, "milestone.start", {"id": mid})
        rep = scaffold_report(root, m)
        print(f"{mid} started." + (f" Report scaffolded at {rep}." if rep else ""))
    elif act == "evidence":
        if m["status"] != "in_progress":
            raise AidlcError(f"{mid} is not in progress")
        if not args.cmd or not args.result:
            raise AidlcError("--cmd and --result are required (what you ran, what it printed)")
        ev = {"at": now(), "cmd": args.cmd, "result": args.result, "file": None, "sha256": None}
        if args.file:
            fp = (root / args.file)
            if not fp.is_file():
                raise AidlcError(f"evidence file not found: {args.file}")
            ev.update(file=args.file, sha256=sha256_file(fp))
        m["evidence"].append(ev)
        commit(root, state, "milestone.evidence", {"id": mid, "cmd": args.cmd,
                                                   "result": args.result, "file": args.file})
        print(f"evidence #{len(m['evidence'])} recorded for {mid}")
    elif act == "verify":
        if m["status"] != "in_progress":
            raise AidlcError(f"{mid} is not in progress")
        if not m["evidence"]:
            raise AidlcError("log evidence first (`milestone evidence`); cannot verify on no proof")
        if not args.criterion or not args.proof:
            raise AidlcError("--criterion N and --proof TEXT are required")
        if not 1 <= args.criterion <= len(m["criteria"]):
            raise AidlcError(f"criterion must be 1..{len(m['criteria'])}")
        c = m["criteria"][args.criterion - 1]
        c.update(verified=True, proof=args.proof)
        commit(root, state, "milestone.verify", {"id": mid, "criterion": args.criterion,
                                                 "proof": args.proof})
        print(f"{mid} criterion {args.criterion} verified; remaining: {unverified(m) or 'none'}")
    elif act == "submit":
        if m["status"] != "in_progress":
            raise AidlcError(f"{mid} is not in progress")
        problems = []
        if not m["evidence"]:
            problems.append("no evidence logged")
        if unverified(m):
            problems.append(f"unverified criteria: {unverified(m)}")
        errs, warns = va.check_artifact(root, "milestone-report", mid, state["tier"])
        problems += errs
        for w in warns:
            print("WARN ", w)
        if problems:
            for p in problems:
                print("ERROR", p)
            raise AidlcError(f"cannot submit {mid}")
        m["status"] = "awaiting_approval"
        commit(root, state, "milestone.submit", {"id": mid})
        print(f"{mid} submitted (G4). STOP: present the milestone report and wait for approval.")
    elif act == "approve":
        if m["status"] != "awaiting_approval":
            raise AidlcError(f"{mid} is '{m['status']}', not awaiting_approval")
        actor = approval_actor(state, f"milestone {mid}", args.note)
        m.update(status="done", approval={"at": now(), "note": args.note or "approved at terminal",
                                          "actor": actor})
        commit(root, state, "milestone.approve", {"id": mid, "note": m["approval"]["note"]}, actor)
        print(f"{mid} done.\nnext:", next_action(state))
    elif act == "reject":
        if m["status"] != "awaiting_approval":
            raise AidlcError(f"{mid} is not awaiting approval")
        if not args.note:
            raise AidlcError("--note is required: record what must change")
        m["status"] = "in_progress"
        m["rework"] = m.get("rework", 0) + 1
        m["note"] = args.note
        for c in m["criteria"]:
            c.update(verified=False, proof=None)
        commit(root, state, "milestone.reject", {"id": mid, "note": args.note})
        print(f"{mid} sent back (rework {m['rework']}). All criteria must be re-verified.")
        if m["rework"] >= MAX_REWORK:
            print(f"WARNING: {m['rework']} rejections. Stop and diagnose with the user.")
    elif act == "block":
        if m["status"] not in ("in_progress", "planned"):
            raise AidlcError(f"cannot block a milestone that is '{m['status']}'")
        if not args.note:
            raise AidlcError("--note is required: say what is blocking")
        m.update(status="blocked", note=args.note)
        commit(root, state, "milestone.block", {"id": mid, "note": args.note})
        print(f"{mid} blocked.")
    elif act == "unblock":
        if m["status"] != "blocked":
            raise AidlcError(f"{mid} is not blocked")
        m["status"] = "planned"
        commit(root, state, "milestone.unblock", {"id": mid, "note": args.note})
        print(f"{mid} back to planned; run `milestone start {mid}` when ready.")
    elif act == "reopen":
        if m["status"] != "done":
            raise AidlcError(f"{mid} is not done")
        if open_milestones(state):
            raise AidlcError("another milestone is open")
        if not args.note:
            raise AidlcError("--note is required: what regression or defect was found")
        m.update(status="in_progress", approval=None, note=args.note)
        for c in m["criteria"]:
            c.update(verified=False, proof=None)
        commit(root, state, "milestone.reopen", {"id": mid, "note": args.note})
        changed = reset_gates_from(state, "G5", root, f"reopened {mid}")
        if changed:
            commit(root, state, "gate.reset.done", {"gate": "G5", "note": f"reopened {mid}"})
        print(f"{mid} reopened. Dependents that are done may need re-verification.")
    return 0


def cmd_note(args):
    root = require_root(args)
    state = load_state(root)
    if args.action == "add":
        if not args.text:
            raise AidlcError("--text is required")
        n = {"id": f"N{len(state['notes']) + 1}", "kind": args.kind, "text": args.text,
             "status": "open", "at": now()}
        state["notes"].append(n)
        commit(root, state, "note.add", n)
        print(f"{n['id']} recorded ({n['kind']})")
    else:
        for n in state["notes"]:
            if n["id"] == args.text:
                n["status"] = "closed"
                commit(root, state, "note.close", {"id": n["id"]})
                print(f"{n['id']} closed")
                return 0
        raise AidlcError(f"unknown note {args.text}")
    return 0


ALLOWED_CONFIG_KEYS = {"strict_approvals", "hook_enforcement"}


def cmd_config(args):
    root = require_root(args)
    state = load_state(root)
    act = args.action
    param = args.param
    if param not in ALLOWED_CONFIG_KEYS:
        raise AidlcError(f"unknown configuration parameter '{param}'; allowed: {sorted(ALLOWED_CONFIG_KEYS)}")

    if act == "get":
        val = state.get("config", {}).get(param)
        print(f"{param}: {val}")
        return 0
    elif act == "set":
        if args.value is None:
            raise AidlcError(f"value is required for 'config set {param}'")
        raw_val = args.value.strip().lower() if isinstance(args.value, str) else str(args.value).lower()
        if raw_val in ("true", "1", "yes", "on"):
            val = True
        elif raw_val in ("false", "0", "no", "off"):
            val = False
        else:
            raise AidlcError(f"parameter '{param}' requires a boolean value (true/false)")

        old_val = state.get("config", {}).get(param)
        if not args.reason:
            raise AidlcError("--reason is required for configuration changes")

        actor = default_actor()
        if param == "strict_approvals" and old_val is True and val is False:
            if not is_interactive():
                raise AidlcError("strict approvals can only be disabled by a human in an interactive terminal")
            confirm = input("Type 'disable-strict' to disable strict approvals: ").strip().lower()
            if confirm != "disable-strict":
                raise AidlcError("confirmation failed: strict approvals was not disabled")
            actor = "human-tty"

        state.setdefault("config", {})[param] = val
        data = {
            "param": param,
            "old": old_val,
            "new": val,
            "reason": args.reason,
        }
        commit(root, state, "config.update", data, actor=actor)
        print(f"config updated: {param} = {val} (was {old_val})")
        return 0


def cmd_adr(args):
    root = require_root(args)
    d = root / "docs" / "aidlc" / "adr"
    d.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for p in d.glob("ADR-*.md")
            if (m := re.match(r"ADR-(\d+)", p.name))]
    n = max(nums, default=0) + 1
    slug = re.sub(r"[^a-z0-9]+", "-", args.title.lower()).strip("-")[:50]
    dest = d / f"ADR-{n:03d}-{slug}.md"
    tpl = (Path(__file__).resolve().parent.parent / "templates" / "adr.md").read_text("utf-8")
    dest.write_text(tpl.replace("{{adr_number}}", f"{n:03d}").replace("{{adr_title}}", args.title),
                    encoding="utf-8")
    print(f"created {dest.relative_to(root)}")
    return 0


def check_chain(events: list):
    errs, prev = [], GENESIS
    for i, e in enumerate(events, 1):
        if e.get("seq") != i:
            errs.append(f"event {i}: sequence number is {e.get('seq')}")
        if e.get("prev") != prev:
            errs.append(f"event {i}: broken chain (prev hash mismatch)")
        body = {k: v for k, v in e.items() if k != "hash"}
        if hashlib.sha256(_canon(body).encode()).hexdigest() != e.get("hash"):
            errs.append(f"event {i}: content hash mismatch (log was edited)")
        prev = e.get("hash", "")
    return errs, prev


def derived_approvals(events: list):
    gates, ms = set(), set()
    for e in events:
        t, d = e["type"], e["data"]
        if t == "gate.approve":
            gates.add(d["gate"])
        elif t in ("gate.reject", "gate.reset"):
            gates.discard(d["gate"])
        elif t == "milestone.approve":
            ms.add(d["id"])
        elif t in ("milestone.reject", "milestone.reopen", "milestone.remove"):
            ms.discard(d["id"])
    return gates, ms


def derived_config(events: list) -> dict:
    cfg = {"strict_approvals": False, "hook_enforcement": True}
    for e in events:
        t, d = e.get("type"), e.get("data", {})
        if t == "project.init":
            cfg["strict_approvals"] = bool(d.get("strict", False))
            if "no_hook" in d:
                cfg["hook_enforcement"] = not bool(d.get("no_hook", False))
        elif t == "config.update":
            param = d.get("param")
            old_val = d.get("old")
            val = d.get("new")
            if param is not None:
                if old_val is not None and old_val != cfg.get(param):
                    raise AidlcError(
                        f"event {e.get('seq')}: config.update 'old' value mismatch for '{param}': "
                        f"event recorded {old_val!r} but preceding state was {cfg.get(param)!r}"
                    )
                cfg[param] = val
    return cfg


def verify(root: Path, state: dict):
    errs, warns = [], []
    if state.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"unsupported schema_version {state.get('schema_version')}")
    try:
        events = read_events(root)
    except AidlcError as e:
        return [str(e)], warns
    chain_errs, head = check_chain(events)
    errs += chain_errs
    if events and state.get("head") != head:
        errs.append("state.json head does not match the last event (state or log was edited)")
    try:
        d_cfg = derived_config(events)
    except AidlcError as e:
        errs.append(str(e))
        d_cfg = {}
    s_cfg = state.get("config", {})
    for k, v in d_cfg.items():
        if s_cfg.get(k) != v:
            errs.append(
                f"config '{k}' mismatch: state.json has {s_cfg.get(k)!r} "
                f"but event history derives {v!r} (missing config.update event)"
            )
    g_ev, m_ev = derived_approvals(events)
    g_state = {g for g in GATE_ORDER if state["gates"][g]["status"] == "approved"}
    for g in sorted(g_state - g_ev):
        errs.append(f"gate {g} is approved in state.json but has no approval event")
    for g in sorted(g_ev - g_state):
        warns.append(f"gate {g} has an approval event but is not approved in state.json")
    for i, g in enumerate(GATE_ORDER[1:], 1):
        if g in g_state and GATE_ORDER[i - 1] not in g_state:
            errs.append(f"gate {g} approved while {GATE_ORDER[i - 1]} is not")
    ids = {m["id"] for m in state["milestones"]}
    if len(open_milestones(state)) > 1:
        errs.append("more than one milestone is open (in_progress/awaiting_approval)")
    for m in state["milestones"]:
        if m["status"] not in MS_STATES:
            errs.append(f"{m['id']}: invalid status {m['status']}")
        for d in m["depends_on"]:
            if d not in ids:
                errs.append(f"{m['id']}: depends on unknown {d}")
            elif m["status"] == "done" and get_ms(state, d)["status"] != "done":
                errs.append(f"{m['id']} is done but dependency {d} is not")
        if m["status"] in ("awaiting_approval", "done"):
            if not m["evidence"]:
                errs.append(f"{m['id']}: {m['status']} without evidence")
            if unverified(m):
                errs.append(f"{m['id']}: {m['status']} with unverified criteria {unverified(m)}")
        if m["status"] == "done" and m["id"] not in m_ev:
            errs.append(f"{m['id']} is done in state.json but has no approval event")
        for ev in m["evidence"]:
            if ev.get("file"):
                fp = root / ev["file"]
                if not fp.is_file():
                    warns.append(f"{m['id']}: evidence file missing: {ev['file']}")
                elif sha256_file(fp) != ev["sha256"]:
                    warns.append(f"{m['id']}: evidence file changed since logged: {ev['file']}")
    if state["gates"]["G5"]["status"] == "approved":
        if any(m["status"] != "done" for m in state["milestones"]):
            errs.append("G5 approved while milestones are not done")
    if state["tier"] >= 3 and not state["config"]["strict_approvals"]:
        warns.append("tier 3 (security-sensitive) without strict approvals")
    return errs, warns


def cmd_verify(args):
    root = require_root(args)
    errs, warns = verify(root, load_state(root))
    for w in warns:
        print("WARN ", w)
    for e in errs:
        print("ERROR", e)
    print(f"{'FAIL' if errs else 'PASS'}: {len(errs)} error(s), {len(warns)} warning(s)")
    return 1 if errs else 0


def cmd_render(args):
    root = require_root(args)
    state = load_state(root)
    (root / STATE_DIRNAME / "STATE.md").write_text(render_markdown(state), encoding="utf-8")
    print("rendered .aidlc/STATE.md")
    return 0


def cmd_log(args):
    root = require_root(args)
    for e in read_events(root)[-args.n:]:
        print(f"{e['seq']:>4} {e['ts']} {e['actor']:<10} {e['type']:<20} "
              f"{_canon(e['data'])[:110]}")
    return 0


# -------------------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(prog="aidlc.py", description=__doc__.split("\n")[0])
    ap.add_argument("--root", help="project root (default: search upwards for .aidlc/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create .aidlc/ state")
    p.add_argument("--project", required=True)
    p.add_argument("--tier", type=int, choices=[2, 3], default=2,
                   help="2 standard, 3 security-sensitive")
    p.add_argument("--mode", choices=["greenfield", "brownfield"], default="greenfield")
    p.add_argument("--strict", action="store_true",
                   help="approvals must be typed by a human in a real terminal")
    p.add_argument("--no-hook", action="store_true", help="disable tool-call/git hook enforcement (gate_hook.py)")
    p.add_argument("--scaffold", action="store_true", help="copy artifact templates to docs/aidlc/")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("scaffold", help="copy artifact templates into docs/aidlc/")
    p.add_argument("names", nargs="*", help="subset of: " + ", ".join(SCAFFOLD))
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_scaffold)

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    sub.add_parser("next", help="print the next required action").set_defaults(fn=cmd_next)

    p = sub.add_parser("gate", help="present | approve | reject | reset a gate")
    p.add_argument("action", choices=["present", "approve", "reject", "reset"])
    p.add_argument("gate")
    p.add_argument("--note", help="quote the user's words (approve/reject/reset)")
    p.add_argument("--summary", help="one-line summary shown in the log (present)")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("milestone", help="manage milestones (one bolt = one milestone)")
    p.add_argument("action", choices=["add", "remove", "start", "evidence", "verify", "submit",
                                      "approve", "reject", "block", "unblock", "reopen"])
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--depends")
    p.add_argument("--accept", action="append", help="acceptance criterion (repeatable)")
    p.add_argument("--replan", action="store_true", help="allow changing an approved plan")
    p.add_argument("--cmd", help="evidence: command that was run")
    p.add_argument("--result", help="evidence: what it printed / outcome")
    p.add_argument("--file", help="evidence: saved log or report path (hashed)")
    p.add_argument("--criterion", type=int)
    p.add_argument("--proof")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_milestone)

    p = sub.add_parser("config", help="view or update project configuration")
    p.add_argument("action", choices=["get", "set"])
    p.add_argument("param", help="parameter name, e.g. strict_approvals")
    p.add_argument("value", nargs="?", help="parameter value (for set)")
    p.add_argument("--reason", help="reason for configuration change")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("note", help="record decisions, risks, deviations, blockers")
    p.add_argument("action", choices=["add", "close"])
    p.add_argument("--kind", choices=["decision", "risk", "deviation", "blocker", "waiver"],
                   default="decision")
    p.add_argument("--text", help="note text (or note id for close)")
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("adr", help="create an architecture decision record")
    p.add_argument("title")
    p.set_defaults(fn=cmd_adr)

    sub.add_parser("verify", help="check invariants and audit-log integrity").set_defaults(
        fn=cmd_verify)
    sub.add_parser("render", help="regenerate .aidlc/STATE.md").set_defaults(fn=cmd_render)
    p = sub.add_parser("log")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_log)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except AidlcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
