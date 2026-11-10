# Threat model: url-shortener

## Assets
- The link table (integrity: attackers must not redirect existing links)
- API keys (confidentiality)
- Service availability and the reputation of the short domain

## Trust boundaries
- Internet visitor to API (anonymous)
- Link creator (API key) to API
- API to PostgreSQL

## Threats
| ID | Boundary | Threat | Category | Mitigation | Residual risk |
|----|----------|--------|----------|------------|---------------|
| T-1 | creator to API | Link to javascript:, file:, or an internal host used for phishing or SSRF | T, E | urlcheck allows only http(s) and rejects private, loopback and link-local hosts (enforced in urlcheck.validate, tested in test_urlcheck) | low |
| T-2 | visitor to API | Enumerating codes to harvest private links | I | 7-char random codes; no list endpoint; 404 for unknown (enforced in store.new_code) | low |
| T-3 | creator to API | Write flood or credential stuffing on keys | D | per-key limit 60/min returns 429; failed key lookups limited per IP (limiter, tested) | med |
| T-4 | creator to API | Leaked key used to create links | S | keys stored hashed; keys revocable by admin script; keys never logged (log filter tested) | med |
| T-5 | API to DB | SQL injection | T | parameterized queries only; lint rule bans string-built SQL | low |
| T-6 | owner to API | Reading another owner's stats (IDOR) | I | stats query filters by owner_id; other owner's code returns 404 (tested) | low |

## Abuse cases
- A user shortens a malware URL. Out of scope for v1; a reporting and takedown process is listed as a follow-up

## Out-of-scope threats
- Volumetric DDoS: handled by the platform edge
