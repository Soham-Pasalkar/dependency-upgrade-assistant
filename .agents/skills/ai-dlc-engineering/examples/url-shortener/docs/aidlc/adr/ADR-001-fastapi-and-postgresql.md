# ADR-001: Fastapi and postgresql

- Status: accepted
- Date: 2026-11-01

## Context
See architecture.md.

## Options considered
1. Option chosen below
2. Alternative noted in the decision

## Decision
We know Python and Postgres, the load is modest, and operating one container is simplest. Chosen over serverless plus DynamoDB for lower cost at this scale and easier local testing.

## Consequences
Revisit if sustained load exceeds 500 requests/s or links need global distribution.
