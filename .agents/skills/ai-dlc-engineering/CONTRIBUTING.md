# Contributing

Thanks for helping. This project is a skill (instructions plus tools) for coding agents, so changes
are judged on two things: does the agent behave better, and do the tools stay correct.

## Setup
No dependencies. Python 3.9+.

```bash
python3 -m unittest discover -s tests      # unit tests
python3 scripts/lint_skill.py .            # package lint
python3 scripts/install.py --agent all --dry-run   # installer smoke test
python3 scripts/scan_secrets.py .          # secrets scan (tests build fake keys at runtime)
python3 scripts/aidlc.py --root examples/url-shortener verify
```

## Principles for changes
1. **SKILL.md stays under 500 lines.** Detail goes in `references/`, loaded only when needed
2. **Explain why, not only what.** Agents follow reasoning better than bare commands
3. **Every rule that matters should be checkable** by a script or an eval. If you add a hard rule,
   add a test or an eval case
4. **No filler.** Each line must change what the agent does
5. **Be honest about enforcement.** Do not claim guarantees the tools cannot give
6. **Stdlib only** for scripts, so the skill runs anywhere

## Adding or changing behaviour
- Tools: add unit tests in `tests/`; run the full suite
- Guidance: add or update an eval case under `evals/<case-name>/` (prompt, `case.yaml`, graders).
  Describe observable behaviour, for example "stops after presenting G1 and does not write code"
- Trigger description: update `evals/trigger-queries.json` with near-miss examples that should and
  should not trigger

## Pull requests
Describe the failure mode you saw (ideally with a transcript), the change, and the evidence it helps.
Keep PRs focused. Update `CHANGELOG.md`.

## Code of conduct
Be kind and constructive. Critique ideas, not people.
