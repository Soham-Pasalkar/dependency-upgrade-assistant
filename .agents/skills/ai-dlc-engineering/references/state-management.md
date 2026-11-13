# State management

Two directories, both committed to git:

```
.aidlc/                  machine-managed; never hand-edit
  state.json             current state
  events.jsonl           append-only audit log, hash-chained
  STATE.md               human-readable render, regenerated on every change
docs/aidlc/              human-authored artifacts
  requirements.md  architecture.md  threat-model.md  milestone-plan.md  completion-report.md
  adr/ADR-001-*.md       decision records
  reports/M1.md ...      one milestone report each
```

## Lifecycle state machine

```
Gates:   G1 --approve--> G2 --approve--> G3 --approve--> (construction) ... --all milestones done--> G5
         each gate:  pending -> presented -> approved | changes_requested -> (revise) -> presented

Milestone:  planned --start--> in_progress --submit--> awaiting_approval --accept/approve--> done
                                   ^   |                      |
                                   |   +--block--> blocked    +--reject (rework+1, criteria reset)
                                   +------------------------------+
            done --reopen--> in_progress          (also resets G5)
```

Invariants the CLI enforces:
- Gates go in order. `milestone start` needs G1, G2 and G3 approved
- At most one milestone is `in_progress` or `awaiting_approval`
- `submit` needs at least one logged evidence entry, every criterion verified, and a valid report
- `accept` (agent) or `approve` (human) only from `awaiting_approval`. `gate approve` only from `presented`
- `reject` resets every criterion; all must be re-verified. Three rejections raises a STOP
- Changing an approved plan needs `--replan`, which resets G3 and G5
- Presenting a gate fails if the artifact still contains placeholders, TODOs or missing sections

## Resuming a project (any new session)

1. `AIDLC status`: phase, gates, milestones
2. `AIDLC next`: the single next action. Follow it
3. `AIDLC verify`: integrity of state and log. Errors mean someone edited state by hand or the
   log was changed. Report this to the user before continuing
4. Read `docs/aidlc/` artifacts relevant to the current step, and the last few `AIDLC log` entries
5. Tell the user in two lines where things stand, then continue

Never redo inception for a project with state. Never assume that "approved last time" covers new scope.

## Audit log

Each event: `seq`, timestamp, actor (`agent`, or `human-tty` for strict approvals), type, data, and
the hash of the previous event. `verify` recomputes the chain, checks `state.json` against the last
hash, and cross-checks that every approved gate and done milestone has a matching approval event.
This detects casual tampering (hand-editing JSON to mark things done). It is not cryptographic proof
against someone who rewrites the whole log, and `actor` is self-reported unless strict mode is on. For
real assurance, commit often and require reviews on the repo.

## Strict approvals

`init --strict` stores `strict_approvals: true`. Approvals then require an interactive terminal and a
typed `approve`. Agents run without a TTY, so they cannot self-approve. When you hit this, tell the
user the exact command to run in their shell, wait, then continue. It is a speed bump against
accidental or sloppy self-approval, not a defense against an attacker with shell access.

## Manual mode (no code execution)

If scripts cannot run (some chat surfaces), keep the same discipline by hand:
1. Copy `templates/state-manual.md` to `docs/aidlc/STATE.md`
2. Update it at every gate and milestone change; quote the user's approval in the table
3. Obey the same rules: one milestone open, evidence before done, stop at gates
4. Say clearly that nothing is mechanically enforced in this mode

## Multiple sessions and agents

State is plain files; two agents editing at once can conflict. Use one agent per project at a time. If a
merge conflict hits `.aidlc/`, resolve events by sequence order and run `verify`; if the chain breaks,
tell the user rather than patching it quietly.

## Concurrency and crash safety

`state.json` is written atomically. The event is appended before the state is saved, so a crash can
leave `state.json` one step behind the log. `verify` reports a head mismatch; re-run the last command.
