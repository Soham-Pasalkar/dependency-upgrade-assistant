# Requirements elicitation

## What to ask (only when it changes the design)

| Area | Typical blocking questions |
|------|----------------------------|
| Users | Who uses it? How many? Internal or public? |
| Data | What data, how sensitive (PII, secrets, regulated)? Retention? Volume? |
| Auth | Who may do what? Existing identity provider? |
| Integrations | Which external systems, APIs, formats? Sandbox or production access? |
| Scale and performance | Requests per second, data size, latency targets, growth |
| Deployment | Where does it run? Cloud, on-prem, local? Existing CI/CD? |
| Constraints | Language, framework, license, budget, deadline, team skills |
| Quality bar | Prototype, internal tool, or production? Compliance needs? |
| Non-goals | What should it explicitly not do? |

Prefer questions with a closed set of options and a default over open-ended ones.
Format: `templates/clarifying-questions.md`. One message. Numbered.

## Ambiguity detectors: stop and sharpen when you see these words

| Word | Ask or define |
|------|---------------|
| fast, responsive | p95 latency under what, at what load? |
| secure | against whom, protecting what? Which threats and standards? |
| scalable | to what number, over what time? |
| simple, clean | for whom? What can be dropped? |
| like X | which exact behaviours of X? |
| real-time | sub-second? Streaming or polling? |
| support (a format, a browser) | which versions, which behaviours? |
| users can manage ... | create, read, update, delete, share? Who else sees it? |

## Writing requirements

- One behaviour per row. Avoid "and".
- Functional criteria use Given/When/Then with concrete values:
  `Given a valid URL, when the client POSTs /links, then the API returns 201 and a 7-character code.`
- Non-functional criteria use thresholds and conditions:
  `p95 redirect latency under 50 ms at 100 requests/s on the reference VM.`
- Always cover failure behaviour: invalid input, dependency down, unauthorized, rate limited.
- Priority: MUST, SHOULD or COULD. Only MUST items gate release.

## Non-functional prompts

| Quality | Prompt |
|---------|--------|
| Security | Authn/z model, data classification, secrets handling, input trust |
| Privacy | What personal data, why, retention, deletion |
| Performance | Latency and throughput targets, data growth |
| Availability | Acceptable downtime, recovery time and point objectives |
| Observability | Logs, metrics, traces, alerts needed to run it |
| Maintainability | Test coverage expectations, docs, conventions |
| Accessibility | WCAG level for UIs |
| Cost | Budget ceiling, cost per request for AI or cloud calls |

## Assumptions

When you decide without asking, write it down: `- Assumes a single region; multi-region is out of
scope.` Assumptions are how the user catches your wrong guesses cheaply at G1.
