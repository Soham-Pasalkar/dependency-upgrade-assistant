# Review lenses: the senior team in a checklist

Before every gate, review your own work through the people a strong team would bring in. The goal is
to **find problems**, not to certify. Report findings under "Lens findings" as *Fixed* and *Open*.

If subagents are available, run the review in a fresh subagent given only the artifact and the lens
checklist. An independent context catches what the author's context rationalizes. Otherwise review
slowly, one lens at a time, and attack your own work.

## Which lenses at which gate

| Gate | Lenses |
|------|--------|
| G1 requirements | Product, QA, Security |
| G2 architecture and threats | Architect, Security, SRE |
| G3 milestone plan | Tech lead, QA, SRE |
| G4 milestone | Tech lead, QA, Security, plus Architect or SRE if touched |
| G5 release | SRE, Security, QA, Writer, Product |

## Product owner
- Does this solve the stated intent? Is anything built that nobody asked for?
- Is every requirement testable? Are priorities clear? Is out-of-scope explicit?
- Are failure and empty states specified? Who is the user in each flow?

## Architect
- Simplest design that meets the requirements? What would you delete?
- Are boundaries and ownership clear? Any hidden coupling or shared database?
- Where are the single points of failure and the scaling limits? Are they acceptable now?
- Is each non-obvious choice an ADR? Any new technology without justification?

## Security engineer
- Walk each trust boundary: authn, authz (object level), validation, output encoding
- Secrets, logs, errors, dependencies, supply chain, sandbox controls
- Does every mitigation name where it is enforced, and is it tested?
- What is the worst thing a malicious user, a compromised dependency, or a prompt-injected agent can do?

## QA / SDET
- Does each acceptance criterion have a test that would fail if the behaviour broke?
- Failure paths, boundaries, concurrency, idempotence, data migration covered?
- Were any tests deleted, skipped, loosened or mocked into meaninglessness? Check the test diff
- Did you actually run it? Is the evidence real and current?

## Tech lead / code reviewer
- Would you approve this PR? Readable, minimal, consistent with the codebase?
- Dead code, duplication, leaky abstractions, swallowed errors, magic values?
- Scope: anything beyond the milestone? Dependencies added and justified?
- Is the diff reviewable in one sitting?

## SRE / DevOps
- Can it be deployed, configured, monitored, rolled back, and debugged at 3am?
- Health checks real? Timeouts, retries and limits sane? Resource use bounded?
- Migrations safe (additive first, reversible, tested on realistic data)?
- Logs structured, with request IDs, free of secrets? Alerts defined?

## Technical writer
- Can a new engineer install, run, test and deploy from the README alone?
- Are configuration, errors, and operational procedures documented?
- Are limitations and non-goals stated rather than hidden?

## Skeptic (always)
- What assumption, if wrong, breaks this? What have I not verified?
- If a reviewer disagreed, where would they point first?

## Output format

```
Lens findings
- Fixed: [QA] test_create_link only asserted status code; added body and DB assertions
- Fixed: [Security] redirect target now scheme-allowlisted (T-1)
- Open: [Architect] SQLite limits write concurrency; fine for v1 load, revisit at 500 rps
```
