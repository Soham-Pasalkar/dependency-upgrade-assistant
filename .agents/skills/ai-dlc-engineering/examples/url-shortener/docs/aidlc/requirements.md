# Requirements: url-shortener

## Intent
A small public HTTP service that turns long URLs into short codes and redirects visitors, so teams
can share compact links. Owners can see how often their links are used.

## Users and context
Internal teams (about 200 people) create links with an API key. The public follows links without auth.

## Functional requirements
| ID | Requirement | Acceptance criterion | Priority |
|----|-------------|----------------------|----------|
| FR-1 | Create a short link | Given a valid API key and an http(s) URL, when the client POSTs /links, then the API returns 201 with a 7-character base62 code and the short URL | MUST |
| FR-2 | Redirect | Given an existing code, when a visitor GETs /{code}, then the API returns 302 with the original URL in Location; for an unknown code it returns 404 | MUST |
| FR-3 | Reject unsafe URLs | Given a URL with a non-http(s) scheme or a private/loopback host, when POSTed, then the API returns 422 with error code unsafe_url and stores nothing | MUST |
| FR-4 | Click statistics | Given the owner's API key, when GET /links/{code}/stats, then the response contains the total click count; another owner's key gets 404 | SHOULD |

## Non-functional requirements
| ID | Requirement | Acceptance criterion | Priority |
|----|-------------|----------------------|----------|
| NFR-1 | Redirect latency | p95 under 50 ms at 100 requests/s on the reference 2 vCPU VM | MUST |
| NFR-2 | Write protection | Write endpoints need an API key; keys are stored hashed; more than 60 writes/min/key returns 429 | MUST |
| NFR-3 | Operability | /healthz fails when the database is unreachable; shutdown drains in-flight requests within 10 s | MUST |

## Assumptions
- One region and one PostgreSQL primary are enough for v1
- API keys are issued by hand by an admin; there is no self-service signup
- Click counts may lag by a few seconds

## Out of scope
- Custom aliases, link expiry, QR codes, a web UI, multi-region

## Open questions
- None blocking
