# AGENTS.md — Dependency Upgrade Assistant 2.0 Governance & Operating Manual

> **Scope:** This document establishes mandatory project governance, engineering methodologies, skill hierarchies, and operational rules for AI coding agents working in the **Dependency Upgrade Assistant** repository.
>
> **Authority:** All AI agents operating in this repository (including Antigravity, Claude Code, Gemini CLI, Cursor, Codex, and others) MUST strictly comply with this document across all development sessions.

---

## 1. Project Identity & Objective

### 1.1 Project Overview
**Dependency Upgrade Assistant 2.0** is an interactive, security-first developer platform that analyzes repositories, deterministically evaluates dependency upgrade candidates, surfaces source-code quality findings, executes safe verification in isolated environments, and exports production-grade deliverables.

The project is evolving from an existing Python-based prototype (`src/`, `tests/`) into a polished, full-stack developer platform.

### 1.2 Brownfield Baseline
This is an **existing brownfield codebase** with an established architecture, working features, and an active test suite:
- **Core capabilities:** Multi-manifest discovery (`requirements.txt`, PEP 621/735 / Poetry `pyproject.toml`), deterministic PyPI metadata evaluation, AST-based security scanning, local Ollama advisory analysis, containerized Docker sandboxing, and cryptographically verified deliverable generation (`.patch`, `.zip`, `.md`).
- **Baseline health:** 140+ passing unit and integration tests under `tests/`.
- **Preservation imperative:** Preserve existing working functionality, architectural patterns, and tests. A ground-up rewrite is strictly prohibited unless an explicit, human-approved requirement authorizes it.

---

## 2. Mandatory Skill Hierarchy & Conflict Resolution

The repository integrates two project-level skills located in `.agents/skills/`:
1. `ai-dlc-engineering` (`.agents/skills/ai-dlc-engineering/SKILL.md`)
2. `frontend-design` (`.agents/skills/frontend-design/SKILL.md`)

```
               ┌────────────────────────────────────────────────────────┐
               │              Human Decision & Intent                   │
               │   (Ultimate authority, explicit scope approvals, G1-G5)│
               └───────────────────────────┬────────────────────────────┘
                                           │
                                           ▼
               ┌────────────────────────────────────────────────────────┐
               │        Governing Skill: ai-dlc-engineering             │
               │   (Lifecycle, Architecture, Security, Gates, CLI, Ev.) │
               └───────────────────────────┬────────────────────────────┘
                                           │
                                           ▼
               ┌────────────────────────────────────────────────────────┐
               │       Specialized Skill: frontend-design               │
               │   (Visual Identity, Design Tokens, UX, Accessibility)  │
               │   *Invoked ONLY within approved AI-DLC milestones*      │
               └────────────────────────────────────────────────────────┘
```

### 2.1 Governing Skill: `ai-dlc-engineering` (Primary)
The `ai-dlc-engineering` skill is the **authoritative engineering framework** for this repository. It enforces the AI-Driven Development Life Cycle (AI-DLC), structured across three phases: Inception, Construction, and Operations.

* **Mandatory entry:** Read `.agents/skills/ai-dlc-engineering/SKILL.md` before executing any non-trivial work (Tier 2 or Tier 3).
* **Responsibilities:** Governs requirements elicitation, architectural design, threat modeling, milestone decomposition, human approval gates (G1–G5), test-driven construction, evidence gathering, and release auditing.
* **Tooling & State:** All state transitions and milestone tracking MUST run through the prescribed CLI:
  ```bash
  python .agents/skills/ai-dlc-engineering/scripts/aidlc.py <command>
  ```
  *(or `python3 .agents/skills/ai-dlc-engineering/scripts/aidlc.py <command>`)*

### 2.2 Specialized Supporting Skill: `frontend-design`
The `frontend-design` skill governs the visual identity, interface architecture, design token systems, typography, responsive layouts, and user experience for all client-facing surfaces.

* **Scope of Authority:** Invoked whenever frontend, UI, styling, or UX components are designed or modified.
* **Governance Boundary:** The `frontend-design` skill is **subordinate** to `ai-dlc-engineering`. It cannot authorize implementation, bypass lifecycle phases, modify system architecture, or weaken security restrictions. UI implementation occurs only within an approved AI-DLC milestone (e.g., during Construction).

### 2.3 Precedence & Conflict Resolution
When guidelines or recommendations appear to diverge, apply the following strict hierarchy:
1. **Explicit Human Direction:** The human user's direct, written instructions and decisions always take precedence.
2. **AI-DLC Engineering Governance:** Controls lifecycle progression, milestone boundaries, architectural baselines, threat-model constraints, credential protection, and gate approvals.
3. **Frontend Design Guidance:** Controls visual design decisions, aesthetics, typography, interaction patterns, and layout structures *within* the approved architectural scope.
4. **Resolution Rule:** A visual-design or UX preference MUST NEVER override a security constraint, sandbox isolation rule, data-privacy policy, or approval requirement.

---

## 3. Mandatory AI-DLC Lifecycle Workflow

Every non-trivial engineering task (Tier 2 or Tier 3) must execute through the prescribed AI-DLC phases. Never skip phases or combine gates.

```
PHASE 1: INCEPTION
  Requirements Elicitation ──> [Gate G1: Requirements Approval]
         │
         ▼
  Architecture & Threat Model ─> [Gate G2: Design & Risk Approval]
         │
         ▼
  Milestone Plan & Sequence ───> [Gate G3: Plan Approval]
         │
         ▼
PHASE 2: CONSTRUCTION
  Milestone 0: Baseline & Seams (Brownfield Intake)
         │
  Milestone M1 ... Mn (One at a time)
    ├─ Test-driven implementation
    ├─ Security standard validation
    ├─ Real command evidence logging
    └─ [Gate G4: Milestone Approval] (per milestone)
         │
         ▼
PHASE 3: OPERATIONS
  Regression, Performance, Security Scans, Docs & Release Checklist
         │
         ▼
  [Gate G5: Final Release Approval]
```

### 3.1 Phase 1: Inception
1. **Requirements Elicitation:** Clarify requirements in a single batched interaction (`templates/clarifying-questions.md`). Record decisions in `docs/aidlc/requirements.md`. Present and obtain **Gate G1**.
2. **Architecture & Threat Modeling:** Document the target system architecture in `docs/aidlc/architecture.md` (highlighting the delta from the existing codebase). Perform threat modeling in `docs/aidlc/threat-model.md`. Present and obtain **Gate G2**.
3. **Milestone Planning:** Decompose construction into discrete, testable, verifiable milestones in `docs/aidlc/milestone-plan.md`. Present and obtain **Gate G3**.

### 3.2 Phase 2: Construction
* **Strict Single-Milestone Focus:** Work on exactly one milestone at a time (`in_progress`). Never implement features scheduled for future milestones.
* **Brownfield M0 Milestone:** The initial construction milestone in this repository must always be an intake and safety net: establish baseline test results, map seams, verify characterization tests, and identify pre-existing failures.
* **Test-First Implementation:** Write or update tests verifying the milestone's acceptance criteria before or alongside implementation.
* **Evidence Collection:** Run real verification commands. Capture deterministic outputs via:
  ```bash
  python .agents/skills/ai-dlc-engineering/scripts/aidlc.py milestone evidence <Mx> --cmd "<command>" --result "<output>"
  ```
* **Gate G4 Approval:** Submit milestone report (`docs/aidlc/reports/<Mx>.md`) via `milestone submit <Mx>`. Stop and await human approval before moving to the next milestone.

### 3.3 Phase 3: Operations
* **Systemic Verification:** Execute the full regression suite, run secret scanners (`scripts/scan_secrets.py`), perform artifact validation (`scripts/validate_artifacts.py`), and verify dependency constraints.
* **Documentation Synchronization:** Ensure all user documentation, developer manuals, and API specs reflect the landed state.
* **Gate G5 Approval:** Present `docs/aidlc/completion-report.md` via `gate present G5` and obtain final human authorization prior to deployment or merge.

---

## 4. Human Approval Protocol & Execution Boundaries

### 4.1 Unconditional Stop at Approval Gates
When an approval gate is reached (G1, G2, G3, G4 per milestone, G5):
1. **Self-Review:** Perform multi-lens self-review (Architecture, Security, QA, UX, SRE) and resolve all internal findings.
2. **Validate Artifacts:** Run `python .agents/skills/ai-dlc-engineering/scripts/validate_artifacts.py`.
3. **Present Gate:** Present the gate clearly using the compact format (`templates/gate-presentation.md`), highlighting:
   - Summary of decisions and trade-offs.
   - Specific risks and residual unknowns.
   - Exact paths to generated artifacts.
   - The required approval phrase.
4. **Log Presentation:** Execute `python .agents/skills/ai-dlc-engineering/scripts/aidlc.py gate present <GX>` (or `milestone submit <Mx>`).
5. **HALT TURN IMMEDIATELY:** The agent **MUST stop and call no further execution or modification tools**.

### 4.2 Interpreting Human Input
* **Explicit Approval Required:** Only an affirmative, explicit statement from the human user directed at the specific gate (e.g., *"approved"*, *"approve G2"*, *"go ahead with the plan"*) authorizes advancement.
* **Insufficient Signals:** Silence, questions, general compliments (*"looks great"*), partial reviews, or requests to discuss alternatives DO NOT constitute approval.
* **No Self-Approval:** The agent is strictly prohibited from approving gates on its own behalf. The user's exact approval statement must be quoted in the `--note` argument:
  ```bash
  python .agents/skills/ai-dlc-engineering/scripts/aidlc.py gate approve <GX> --note "User approved: <quote>"
  ```

### 4.3 Enforcement Reality
In Antigravity and standard CLI harnesses without custom tool-interception daemons, enforcement is **guidance-based, audit-backed, and guarded by Git/CI**:
- The agent must practice absolute self-discipline by stopping at gates.
- Do not falsely claim that the Antigravity runtime mechanically prevents tool execution.
- Maintain the cryptographic integrity of `.aidlc/events.jsonl` via `aidlc.py verify`.
- Where configured, git pre-commit hooks (`scripts/gate_hook.py --git-pre-commit`) and CI pipelines serve as technical backstops.

---

## 5. Brownfield Engineering & Codebase Preservation

Because Dependency Upgrade Assistant is an existing, functioning application, agents must adhere to the brownfield preservation rules defined in `.agents/skills/ai-dlc-engineering/references/brownfield.md`:

### 5.1 Mandatory Inspection Before Action
Before proposing or executing architectural changes:
1. **Inspect Repo Structure:** Examine existing packages under `src/` (`analysis`, `core`, `data`, `pipeline`, `services`, `validation`, `app.py`).
2. **Review Tests:** Inspect existing test modules under `tests/` to understand existing behaviors and invariants.
3. **Check Git Status:** Run `git status` to identify current branch, active commits, and uncommitted modifications.
4. **Preserve Public Behaviors:** Preserve existing CLI flags, public APIs, file output formats, and user workflows unless explicitly revised in an approved requirement.

### 5.2 Prohibited Actions (Unless Explicitly Authorized at G1/G2)
* **No Ground-Up Rewrites:** Do not replace functioning modules with entirely new architectures when incremental adaptation is feasible.
* **No Arbitrary Framework Swaps:** Do not switch core dependencies or UI frameworks without an approved Architecture Decision Record (ADR).
* **No Silent Test Weakening:** Never delete, skip, loosen assertions, or modify existing tests to make a build pass. If an existing test fails, document it as a known baseline issue.
* **No Unrelated Code Churn:** Do not reformat, rename, or cosmetically clean up files outside the active milestone's scope.
* **Minimal Blast Radius:** Introduce changes behind clear seams (new modules, adapter interfaces, or feature flags) to isolate potential regressions.

---

## 6. Product & Engineering Principles

All architectural and implementation decisions for Dependency Upgrade Assistant 2.0 must reflect the following principles:

### 6.1 Deterministic Correctness Over LLM Confidence
* **Ground Truth:** Dependency versions, PyPI metadata, hash digests, compatibility constraints, and release dates must be sourced from deterministic, authoritative services (PyPI JSON API, local metadata caches, packaging wheels).
* **Advisory Role of LLMs:** Local or remote LLM outputs are purely advisory (e.g., assessing changelog risks, summarizing breaking changes, suggesting code diffs). LLM hallucination must never pollute dependency resolution graphs or version compatibility calculations.
* **Explicit Attribution:** Reports and UI surfaces must clearly distinguish verified factual findings from heuristic or AI-generated recommendations.

### 6.2 Security by Design
* **Untrusted Repositories:** Treat all target repositories, uploaded files, dependency manifests, and external package sources as untrusted inputs.
* **Credential Protection:** Personal Access Tokens (PATs) and credentials must remain strictly in memory and be routed exclusively through `GIT_ASKPASS`. They must NEVER be written to disk, injected into CLI command arguments, embedded in URLs, committed to Git, logged, or forwarded to external LLMs.
* **Sandbox Execution:** Untrusted code (e.g., candidate test suites, package builds, syntax verification) must execute inside locked-down, ephemeral Docker containers configured with `--network none`, non-root execution, resource limits, and read-only volume mounts.

### 6.3 Privacy by Design
* **Local-First Processing:** Preserve the option for 100% air-gapped / local-model execution (e.g., via local Ollama models).
* **Consent for Data Transmission:** Never transmit private repository source code, AST dumps, or dependency lists to third-party hosted AI APIs without explicit user authorization.

### 6.4 Maintainability & Clean Architecture
* **Separation of Concerns:** Maintain clean boundaries between presentation (UI), application coordination (pipeline), domain logic (dependency analysis, AST parsing), and infrastructure (Git, PyPI client, Docker runner).
* **No Premature Complexity:** Avoid redundant abstraction layers, microservices, or complex state stores until proven necessary by verified requirements.

### 6.5 Evidence-Driven Verification
* **No Unverified Claims:** Never claim a feature works, an upgrade succeeded, or a bug was fixed without executing a concrete command and capturing its stdout/stderr.
* **Honest Limitations:** Every milestone report and completion report must explicitly state what was tested, what could not be run, and remaining known risks.

---

## 7. Frontend Design & User Interface Standards

When frontend design or implementation is authorized within an AI-DLC milestone, agents must apply `.agents/skills/frontend-design/SKILL.md`:

### 7.1 Distinctive Visual Identity
* **Subject-Matter Grounding:** Design specifically for a developer tooling platform. The UI must feel robust, high-density, legible, and purpose-built for engineering workflows—not a generic marketing landing page.
* **Avoid Generic AI SaaS Tropes:**
  - DO NOT use the generic "warm cream background with serif display and terracotta clay accents".
  - DO NOT default to a near-black screen with acid-green neon accents.
  - DO NOT partition all content into identical rounded cards with identical soft drop shadows.
  - DO NOT litter headings with tracked-out all-caps eyebrow labels or redundant middle-dot separators.

### 7.2 Design Token System (Two-Pass Planning)
Before writing frontend code, establish and document a compact token system:
1. **Palette:** 4–6 deliberate, semantic hex colors (primary surface, secondary surface, borders, text primary/muted, semantic accent).
2. **Typography:** Maximum 1–2 distinct font families (e.g., an engineered sans-serif paired with a high-legibility monospace for code and versions). Set purposeful scale, weight, and line heights ($\le 80$ characters per line).
3. **Layout & Wireframes:** Document layout structures using ASCII wireframes and alignment rules.
4. **Principles:** State the core visual theme differentiating this interface.

### 7.3 Frontend Quality & Accessibility Floor
* **State Completeness:** Design intentional views for all states: loading/skeleton, empty states (with clear invitations to act), error states (diagnostic and actionable, no vague apologies), success states, and diff previews.
* **Accessibility (a11y):** Ensure full keyboard navigation, high-contrast visible focus rings, compliant color contrast ratios (WCAG AA minimum), and respect for `prefers-reduced-motion`.
* **Motion Discipline:** Animations must be purposeful and responsive to user actions (e.g., expanding analysis trees, drawer reveals). Avoid decorative, unprompted auto-animations on page load.
* **Copywriting:** Use plain, active, user-centric language (e.g., *"Analyze repository"*, not *"Initiate pipeline execution sequence"*).

---

## 8. Security, Testing & Verification Standards

### 8.1 Testing Hierarchy
1. **Unit Tests:** Fast, isolated tests covering AST visitors, version parsing, manifest serializers, and constraint solvers.
2. **Integration Tests:** Verifying end-to-end workflows with mock network services, local fixture repositories, and synthesized package catalogs.
3. **Sandbox Validation:** Verifying container execution logic, timeout enforcement, memory capping, and non-root execution.
4. **Regression Safety:** Ensure the entire suite of pre-existing tests remains 100% green across all changes.

### 8.2 Security Audit Gates
* **Secret Scanning:** Run `python .agents/skills/ai-dlc-engineering/scripts/scan_secrets.py .` prior to milestone submissions.
* **Static Analysis:** Ensure AST scanners flag risky constructs (`eval`, `exec`, `subprocess(shell=True)`) in analyzed code.
* **Integrity Auditing:** Periodically run `python .agents/skills/ai-dlc-engineering/scripts/aidlc.py verify` to ensure the state machine and cryptographic event logs have not been corrupted or manually manipulated.

---

## 9. Git Discipline & Change Management

### 9.1 Pre-Modification Checks
Prior to touching any file on disk:
1. Run `git status` to verify working tree status and branch context.
2. Ensure no untracked or uncommitted changes from previous sessions are unintentionally overwritten.
3. Confirm that the current work directly corresponds to the active, approved AI-DLC milestone.

### 9.2 Scoped Changes
* Keep pull requests and diffs tightly scoped to the single active milestone.
* Do not perform out-of-scope refactoring or dependency updates. Record out-of-scope opportunities via `aidlc.py note add --kind decision|risk` for future milestone planning.
* Commit operations, branch creation, and remote pushes must only occur with explicit user consent.

---

## 10. Documentation Standards & State Architecture

### 10.1 Two-Tiered Project Records
Project knowledge and progress are tracked in two dedicated, Git-committed directories:

```
.aidlc/                           <-- Machine-managed state (NEVER hand-edit)
  state.json                      <-- Active state machine data (atomic writes)
  events.jsonl                    <-- Append-only, hash-chained audit log
  STATE.md                        <-- Human-readable status view, auto-generated

docs/aidlc/                       <-- Human-readable artifacts (authored via AI-DLC)
  requirements.md                 <-- Elicited project requirements (G1)
  architecture.md                 <-- Target architecture & delta specifications (G2)
  threat-model.md                 <-- Systemic threat model & mitigations (G2)
  milestone-plan.md               <-- Milestone roadmap & acceptance criteria (G3)
  completion-report.md            <-- Operations release audit & sign-off (G5)
  adr/                            <-- Architecture Decision Records (ADR-001-*.md)
  reports/                        <-- Verified milestone reports (M0.md, M1.md...)
```

### 10.2 Strict Machine State Invariant
**NEVER manually edit files inside `.aidlc/`.** All modifications to `state.json`, `events.jsonl`, and `STATE.md` must be performed through the `aidlc.py` CLI tool. Any manual tampering will cause `aidlc.py verify` to fail with hash mismatch errors.

---

## 11. Fresh Agent Session Initialization Protocol

Every AI agent beginning a new session in this repository MUST execute the following 10-step initialization protocol before performing any development work:

1. **Verify Repository Root:** Confirm current working directory is the repository root (`Dependency Upgrade Assistant/`).
2. **Read Governance Rules:** Read this `AGENTS.md` file in full.
3. **Verify `ai-dlc-engineering` Skill:** Verify that `.agents/skills/ai-dlc-engineering/SKILL.md` is discoverable and readable.
4. **Verify `frontend-design` Skill:** Verify that `.agents/skills/frontend-design/SKILL.md` is discoverable and readable if frontend work is contemplated.
5. **Inspect Git Context:** Run `git status` and `git branch` to inspect the working branch and any uncommitted files.
6. **Inspect AI-DLC State:** Check if `.aidlc/state.json` exists.
   - If present: Run `python .agents/skills/ai-dlc-engineering/scripts/aidlc.py status` and `python .agents/skills/ai-dlc-engineering/scripts/aidlc.py next`.
   - If absent: Note that the project is awaiting initialization (`aidlc.py init --project "Dependency Upgrade Assistant 2.0" --tier 3 --mode brownfield --strict --scaffold`).
7. **Verify Audit Chain:** If `.aidlc/` exists, run `python .agents/skills/ai-dlc-engineering/scripts/aidlc.py verify` to ensure log integrity.
8. **Review Active Artifacts:** Read relevant artifacts in `docs/aidlc/` corresponding to the current phase or milestone.
9. **Formulate Next Single Action:** Identify the exact next required action dictated by `aidlc.py next` or human instruction.
10. **Report Status & Await Instructions:** Output a concise 2–3 line status summary to the user indicating current phase, active milestone, and next step. **Await user instruction before executing changes.**

---

## 12. Standardized Reporting Protocol

At the conclusion of every substantial development step or milestone submission, provide a structured report containing:

1. **Summary of Changes:** Concise explanation of what was modified and why.
2. **File Modifications:** Explicit list of created, modified, or deleted files.
3. **Execution & Evidence Log:** Exact commands run and verbatim stdout/stderr excerpts.
4. **Test Verification Results:** Test suite execution commands, passed/failed counts, and coverage.
5. **Security & Validation Results:** Outputs from `validate_artifacts.py`, `scan_secrets.py`, or linters.
6. **Task Status Table:** Categorize components into:
   - `Completed`
   - `Partially Completed`
   - `Not Started`
   - `Blocked`
   - `Not Runnable`
   - `Not Verified`
7. **Known Limitations & Residual Risks:** Explicit disclosure of unverified assumptions, stubbed code, or deferred fixes.
8. **AI-DLC Phase Status:** Active phase, gate status, and next pending action.

> **Absolute Rule:** Never claim a test passed or a feature functions without concrete execution evidence. If a command was not run, explicitly mark it as `Not Runnable` or `Not Verified`.

