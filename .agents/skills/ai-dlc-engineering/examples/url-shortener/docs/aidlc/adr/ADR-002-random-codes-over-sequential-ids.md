# ADR-002: Random codes over sequential ids

- Status: accepted
- Date: 2026-11-01

## Context
See architecture.md.

## Options considered
1. Option chosen below
2. Alternative noted in the decision

## Decision
Sequential IDs make links enumerable. Random 7-character base62 gives about 3.5 trillion codes; collisions are retried.

## Consequences
Revisit if sustained load exceeds 500 requests/s or links need global distribution.
