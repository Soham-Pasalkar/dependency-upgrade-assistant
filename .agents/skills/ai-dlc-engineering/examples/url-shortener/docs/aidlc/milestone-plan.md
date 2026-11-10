# Milestone plan: url-shortener

## Milestones
| ID | Title | Depends on | Acceptance criteria | Test approach | Risk |
|----|-------|------------|---------------------|---------------|------|
| M1 | Walking skeleton | none | App starts in a container; /healthz returns 200 and 503 when DB is down; CI runs tests and lint | pytest with a Postgres container; CI run | low |
| M2 | Create links safely | M1 | FR-1 and FR-3 pass; keys hashed; unsafe URLs rejected | unit tests for urlcheck; API tests; mutation check of urlcheck | med |
| M3 | Redirect and rate limit | M2 | FR-2 passes; 429 after 60 writes/min; p95 under 50 ms at 100 rps | API tests; load script on the reference VM | med |
| M4 | Stats and hardening | M3 | FR-4 passes; owner isolation tested; dependency audit and secrets scan clean | API tests; pip-audit; scan_secrets | low |

## Test strategy
Pure logic gets unit tests. Everything touching HTTP or the database is tested against a real
PostgreSQL container, not mocks. Each threat-model row has a named test. NFR-1 is measured with a
repeatable load script.

## Risks
- Latency target may need a connection pool tweak (checked in M3, the first place it is measurable)
