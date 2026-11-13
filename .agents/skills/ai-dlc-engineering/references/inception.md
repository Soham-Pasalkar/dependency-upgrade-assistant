# Phase 1: Inception

Goal: agree on **what** and **how** before writing implementation code. Outputs live in `docs/aidlc/`.
Nothing in this phase writes source code except throwaway spikes (label them and delete them).

## 0. Orient
- Greenfield: restate the goal in two sentences. Brownfield: first read `brownfield.md`.
- Check what the user already provided: docs, code, constraints, deadlines, stack preferences.
- Pick the tier (`tiers.md`) and `init` if tier 2 or 3.

## 1. Clarify (one batch)
Use `references/requirements-elicitation.md`. Ask only what blocks design. Every question carries a
recommended default. Everything else becomes an Assumption in the requirements.

## 2. Requirements (G1)
Fill `docs/aidlc/requirements.md`:
- **Intent**: problem and desired outcome in one paragraph
- **Functional requirements**: `FR-n`, each with a testable Given/When/Then
- **Non-functional requirements**: `NFR-n` with measurable thresholds (security, privacy, performance,
  availability, observability, cost). Write "not applicable because ..." rather than omit
- **Assumptions**, **Out of scope**, **Open questions**

Lens review: product, QA, security. Then `gate present G1` and wait.

## 3. Architecture and threat model (G2)
Fill `architecture.md` and `threat-model.md` following `architecture-standards.md` and
`threat-modeling.md`.
- Choose the simplest design that satisfies the requirements. Name components, boundaries, data,
  interfaces and failure modes.
- Every non-obvious choice becomes an ADR (`AIDLC adr "Title"`), at least one before G2.
- Threat model: assets, trust boundaries, at least 3 threats (5 for tier 3), each with a mitigation that
  names where it is enforced.

Lens review: architect, security, SRE. Then `gate present G2` and wait.

## 4. Milestone plan (G3)
Fill `milestone-plan.md` and register each milestone: `AIDLC milestone add M1 --title ... --accept ...`.

Sizing rules:
- **M1 is a walking skeleton**: the thinnest end-to-end slice running through every layer, with test
  harness, lint and CI hooks. It surfaces integration risk early.
- **Vertical slices over horizontal layers.** "Create link end to end" beats "build the data layer".
- **Risk first.** Put the riskiest unknown (new integration, performance claim, sandbox) in M1 or M2.
- **Each milestone is demonstrable and independently verifiable**, with 1 to 5 acceptance criteria
  that name an observable behaviour or measurable result.
- **Reviewable in one sitting.** If a milestone would produce a diff the user cannot realistically review,
  split it.
- Declare dependencies. Keep the graph shallow.
- Security work (authn/z, input validation, secrets) is part of the slice that introduces it, never a
  late "hardening" milestone alone.

Lens review: tech lead, QA, SRE. Then `gate present G3` and wait. `milestone start` is blocked by the
CLI until G1, G2 and G3 are approved.

## Inception exit checklist
- [ ] Every FR and NFR has a testable criterion
- [ ] Every assumption is visible to the user
- [ ] Every threat has a mitigation and a place it is enforced
- [ ] Every milestone has criteria, a test approach and a risk
- [ ] The plan can be explained in five minutes
