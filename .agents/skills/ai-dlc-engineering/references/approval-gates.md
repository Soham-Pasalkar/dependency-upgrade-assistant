# Approval gates

| Gate | Decision requested | Artifact | CLI |
|------|--------------------|----------|-----|
| G1 | Are these the right requirements? | `requirements.md` | `gate present G1` |
| G2 | Is this the right design and risk posture? | `architecture.md`, `threat-model.md` | `gate present G2` |
| G3 | Is this the right plan and sequence? | `milestone-plan.md` plus registered milestones | `gate present G3` |
| G4 | Is this milestone done? (one per milestone) | `reports/Mx.md` | `milestone submit Mx` |
| G5 | Is it ready to release? | `completion-report.md` | `gate present G5` |

Order is enforced: G2 needs G1, G3 needs G2, construction needs G1 to G3, G5 needs all milestones done.

## Protocol

1. **Self-review first** with the lenses for this gate (`review-lenses.md`). Fix what you can.
2. **Validate** artifacts (`validate_artifacts.py`). The CLI also runs this on `present`.
3. **Present** using `templates/gate-presentation.md`. Summary, decisions and trade-offs, lens findings
   (fixed and open), risks, artifact paths, the approval phrase. Keep it under one screen. Link to
   files for detail.
4. **Record** with `gate present` (or `milestone submit`), then **stop**. Do not start the next phase,
   draft the next artifact, or write code while waiting. Answering the user's questions is fine.
5. **Interpret the reply:**
   - **Approval:** explicit and about this gate ("approved", "approve G2", "yes go ahead with that
     plan"). Record the quote in `--note`
   - **Changes requested:** `gate reject --note "..."` (or `milestone reject`), revise, re-present
   - **Question or discussion:** answer it. It is not approval
   - **Approval of part:** treat as changes requested for the rest, and say which part is settled
   - **Ambiguous:** ask one specific yes/no question
6. **Never** infer approval from silence, from approval of an earlier gate, from "looks interesting",
   or from the user continuing the conversation about something else.

## Approval modes

- **Default:** you run `gate approve` after the user's approving message and quote it in `--note`.
  The audit log records it, but the agent technically types the command.
- **Strict** (`init --strict`): `gate approve` and `milestone approve` refuse to run unless a human is
  at a real terminal and types `approve`. Tell the user the exact command to run in their own shell.
  Use for tier 3, shared repos, or when trust in unattended runs is low. Not available where there is
  no human-accessible terminal.

## Re-approval triggers

Return to the right gate when:
- Requirements change (`gate reset G1`, which also resets later gates)
- Architecture or threat model change structurally (`gate reset G2`)
- Milestones are added, removed or materially changed after G3 (`milestone add --replan`)
- A done milestone turns out defective (`milestone reopen`, which also resets G5)

## Waivers

The user may waive any step. Say the specific risk in one sentence, record `note add --kind waiver`,
and continue. A waiver covers the named step only. It never covers reporting results honestly.
