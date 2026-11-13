# ADR-008: Near-black editorial frontend design system

- Status: proposed
- Date: 2026-11-11

## Context
FR-19 requires a complete redesign of the React frontend, inspired by zrk.technology: cinematic near-black, editorial typography, burnt-orange accent, premium spacing, minimal navigation, precise technical surfaces, subtle motion. The current UI is a single `App.css` with internal milestone labels. AGENTS.md section 7 forbids generic SaaS tropes and requires a documented token system.

## Options considered
1. Adopt a component library (e.g. a Tailwind kit).
2. Hand-built CSS custom-property token system on the existing React 19 + Vite stack, no new UI framework.

## Decision
Option 2. Tokens: surfaces `#090A0B` / `#111315` / `#17191B`, hairline borders `#26292C`, text `#F2F2F0` / muted `#A0A0A0`, accent `#F05A28`, plus semantic success/warn/danger tuned to AA on the dark surfaces. Typography: one editorial display/sans family and one monospace for versions, paths and diffs (web fonts loaded from Google Fonts, or self-hosted if the build requires). Layout: single-column journey with a sticky step rail, generous whitespace, hairline rules instead of card grids. Motion: short opacity/translate transitions on user action only, disabled under `prefers-reduced-motion`. Existing `api/client.ts` is retained; components are restructured. The product opens on a landing page (navigation, hero, product visual built from real interface components, workflow and capability sections) that leads into the application in the same visual language (FR-19). Orange is reserved for emphasis: the primary action, the active step and key state. The installed `.claude/skills/frontend-design` guidance is applied: a two-pass token plan reviewed against the brief before coding, one memorable element with restraint elsewhere, numbered markers only where content is a real sequence (the workflow), and screenshot-driven critique.

## Consequences
No framework swap and no new runtime dependencies beyond fonts. Visual quality depends on careful CSS; verified by real-browser screenshots at 1440 px and 390 px. Existing component tests will be updated where markup changes; behaviour assertions are kept.
