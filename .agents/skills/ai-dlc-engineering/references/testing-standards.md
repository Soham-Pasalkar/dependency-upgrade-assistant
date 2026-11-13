# Testing standards

Tests exist to make change safe and claims checkable. Coverage is a signal, not a target.

## What to test
- Every acceptance criterion has at least one automated test that fails if the behaviour breaks
- Failure paths: invalid input, dependency failures, authorization denials, boundary values
- Each security mitigation in the threat model has a test or a documented manual check
- Bug fixes begin with a failing test that reproduces the bug

## Shape
- Many fast unit tests of pure logic; fewer integration tests across real boundaries (database,
  HTTP, filesystem); a few end-to-end tests for critical journeys
- Prefer real dependencies in containers over mocks for databases and queues. Mock only at true
  system edges (third-party APIs, clocks, randomness)
- Deterministic: no network to the internet, no wall-clock sleeps, seeded randomness, isolated state

## Anti-patterns that make tests lie

| Anti-pattern | Why it is a problem | Instead |
|--------------|---------------------|---------|
| Mocking the unit under test | Asserts the mock, not the code | Mock collaborators only |
| Asserting what the code returns by copying the code's logic | Tautology | Assert independently derived expected values |
| No assertion, or only "does not raise" | Passes when broken | Assert outcomes |
| Snapshot of huge output accepted blindly | Locks in bugs | Targeted assertions |
| Catch-all `except` hiding failures in tests | Silent pass | Let it fail |
| Shared mutable state between tests | Order-dependent flakiness | Fresh fixtures |
| Loosening an assertion when it fails | Hides regressions | Fix the code or justify the test change to the user |

## Check that tests can fail

Before relying on a new test, break the behaviour on purpose (invert a condition, return a wrong value)
and confirm the test goes red, then restore. Do this for the tests guarding each acceptance
criterion and each security control. It takes a minute and catches vacuous tests.

## Evidence format

Record in the milestone report and with `milestone evidence`:
- The exact command run
- The result line (counts, exit status) or a saved log path (`--file`, which is hashed)
- For manual checks: the steps and the observed outcome

Run the full suite, not only the new tests, before submitting. Report skipped tests and why.

## Flaky tests
Never retry-until-green. Reproduce, find the shared state or timing assumption, fix it. If you must
quarantine one, record a note with owner and reason, and mention it in the report.

## Performance and load
Where an NFR sets a threshold, measure it with a repeatable script and record the environment and
numbers. State plainly when a measurement was taken on a developer machine.

## Coverage
Report coverage if tooling exists. Do not chase a percentage. A missing test for a security control
matters more than five points of overall coverage.
