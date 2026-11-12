# ADR-005: Server-authoritative run state and approval lifecycle in SQLite

- Status: proposed
- Date: 2026-11-11

## Context
The v2.0 API is stateless: the client posts analyses, recommendations and `validation_status` back to the server, which trusts them (note N5). Approval exists only as a client boolean, and export is ungated. FR-15/FR-16 require the server to own pipeline state and enforce approval. The existing `src/api/database.py` already creates `repositories` and `analysis_runs` tables, and `proposal_registry` already implements an atomic proposal lifecycle in memory.

## Options considered
1. Keep the client-carried pipeline and add signatures (HMAC) over client payloads.
2. Store runs, recommendations, proposals and approvals server-side in SQLite (additive tables plus JSON payload columns), referenced by opaque IDs.

## Decision
Option 2. A `RunStore` module persists each run (source, manifests, analysis, recommendations) and each plan's approval record (proposal ID, diff SHA-256, timestamp) in SQLite using additive `CREATE TABLE IF NOT EXISTS` migrations. Routes accept IDs and selections only. `proposal_registry` remains the in-memory lifecycle for apply/consume, and is bound to the run. Existing request shapes stay accepted where tests rely on them, but server-held values override client values.

## Consequences
Forged client state can no longer change results; approval becomes enforceable for validation, apply and export. Runs survive a backend restart. Costs: a schema migration (additive only) and new route contracts consumed by the redesigned frontend. Revisit if multi-user or remote deployment is introduced (would need authentication and per-user scoping).
