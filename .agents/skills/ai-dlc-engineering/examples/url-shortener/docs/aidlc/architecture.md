# Architecture: url-shortener

## Overview
A single FastAPI service backed by PostgreSQL, deployed as one container. Redirects read one indexed
row. Click counting is a separate fire-and-forget update so redirects never wait on it.

## Components
| Component | Responsibility | Talks to | Tech |
|-----------|----------------|----------|------|
| api | HTTP routing, validation, auth, rate limit | store | FastAPI, Python 3.12 |
| store | Persistence of links, keys, clicks | PostgreSQL | psycopg 3 |
| urlcheck | Scheme and host safety checks (SSRF-style) | none | stdlib |
| limiter | Per-key sliding window | memory | in-process |

## Data
Tables: `api_keys(id, owner, key_hash UNIQUE, created_at)`, `links(code PK, url, owner_id FK, created_at)`,
`clicks(code FK, ts)`. Codes are 7 random base62 characters, retried on collision. Key hashes use
SHA-256 of a 32-byte random key (high entropy, so a fast hash is acceptable). Retention: clicks 13 months.

## Interfaces
`POST /links`, `GET /{code}`, `GET /links/{code}/stats`, `GET /healthz`. Errors are JSON:
`{"error": "unsafe_url", "message": "..."}` with stable codes.

## Failure modes
| Dependency or step | Failure | Behaviour |
|--------------------|---------|-----------|
| PostgreSQL | down or slow | /healthz returns 503; writes and redirects return 503 with Retry-After; no retries on writes |
| Click write | fails | logged, redirect still succeeds |
| Code collision | duplicate key | regenerate up to 5 times, then 500 and alert |

## Quality attributes
Latency from a single indexed lookup and a connection pool. Observability: JSON logs with request ID,
RED metrics on /metrics. Testability: urlcheck and limiter are pure and unit-tested.

## Decision records
- ADR-001: FastAPI and PostgreSQL over serverless plus DynamoDB. See docs/aidlc/adr/
- ADR-002: Random codes over sequential IDs, to prevent enumeration. See docs/aidlc/adr/
