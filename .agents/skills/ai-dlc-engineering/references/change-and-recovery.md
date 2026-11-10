# Change and recovery

Plans meet reality. The aim is controlled change, not no change.

## The user changes or adds requirements

| When | Do |
|------|----|
| During inception | Update the artifacts and re-present the affected gate |
| In construction, small and inside the current milestone's criteria | Proceed; note the clarification in the report |
| In construction, new scope | Finish or block the open milestone, then `milestone add --replan` (resets G3 and G5), update `milestone-plan.md`, re-present G3 |
| Changes the architecture or threat model | `gate reset G2` with a note; revise; re-present; re-plan |
| After release | New intent: `milestone add --replan` |

Say what the change costs (milestones added, risk, ordering) in two sentences so the user can choose.

## A milestone keeps failing

- Test fails: hypothesis, one change, rerun. After three failed attempts, stop and report: what you
  tried, what you learned, two or three options, your recommendation
- Rejected by the user three times: STOP. Probably the criteria or the design is wrong. Discuss, then
  amend the plan with `--replan` or reset G2
- Flaky or environment problems: record a blocker note, show the evidence, ask for help. Do not paper
  over it with retries or skips

## You discover an earlier mistake

Tell the user plainly: what is wrong, what it affects, how you found it, the fix and its cost. If a
`done` milestone is affected, `milestone reopen Mx --note "..."`. Never patch an approved milestone
quietly inside a later one. The audit trail should show the correction.

## Blocked

`milestone block Mx --note "need staging credentials for the payment sandbox"`. Say exactly what you
need and from whom. While blocked, do not start dependent work. Another independent milestone may be
started only if the user agrees and none is open.

## Rollback

- Work in git branches or commit per milestone so each milestone can be reverted alone
- Destructive operations (dropping data, force pushes, deleting infrastructure) need explicit user
  approval and a verified backup or rollback path first
- If a deployed change misbehaves: stabilise first (revert or disable via flag), then diagnose,
  then write the post-mortem as a short note in the next milestone report

## Context loss

If a conversation resets or the context is compacted: run `status`, `next`, `verify`, read
`docs/aidlc/` and the latest report, then summarise state to the user in two lines. Do not guess from
memory.

## Disagreement with the user

State your concern once with concrete reasoning and an alternative. If they decide otherwise, record it
(`note add --kind decision`) and proceed. If the choice would cause harm outside the user's own
project (illegal, unsafe to others), decline that part and explain.

## Tool or environment limits

If you cannot run tests, a scanner, or the sandbox, say so and mark the criterion "not verified" in the
report. Do not substitute reading the code for running it.
