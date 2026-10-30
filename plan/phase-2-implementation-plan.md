# AI-DLC Implementation Plan: Phase 2 - Repository Analysis and Improvement Platform

- **Document ID:** PLAN-DUA-002
- **Status:** APPROVED WITH REQUIRED AMENDMENTS
- **Version:** 2.1.0
- **Lifecycle Phase:** AI-DLC Phase 2: Architecture & Implementation Execution
- **Approval Timestamp:** 2026-10-30T08:18:41+05:30
- **Author:** Software Architect & Python Engineer (AI Assistant)
- **Reviewer:** Human Maintainer / Open-Source Contributor
- **Base Plan:** `plan/dependency-upgrade-assistant-plan.md` (PLAN-DUA-001)

---

## 1. Executive Summary & Objective

Phase 2 transforms the existing Dependency Upgrade Assistant into an **AI-powered Repository Analysis and Improvement Platform**. 

The platform enables users to submit a GitHub repository URL (public or private), clone it into a temporary dual-copy workspace, discover and analyze dependency manifests (`requirements.txt` and `pyproject.toml`), connect to a locally running **Ollama** runtime to evaluate compatibility risks and propose source code improvements, validate all modifications inside an isolated environment (syntax checking, linting, and dedicated sandboxed tests), and generate verified downloadable artifacts (unified `.patch` validated via `git apply --check`, complete improved project `.zip` verified via `testzip()`, and detailed analysis report) — orchestrated through a single **"Analyze & Improve Repository"** one-click workflow.

### Core Architectural Amendments Incorporated:
1. **Host Environment Protection & Dedicated Sandbox:** Host Python environment will **never** install untrusted repository dependencies (`pip install` forbidden on host). Repository tests execute strictly inside a dedicated Docker sandbox (non-root, unprivileged, no network, resource limits, no Docker socket mount). If Docker/isolation is unavailable, tests are classified as `NOT_RUNNABLE` (with explicit reasons) and never run on the host.
2. **Configurable Source Code Inspection Scope:** Inspection limit defaults to 10 files (configurable), prioritizing core modules and files associated with upgraded dependencies, and explicitly reporting which files were analyzed vs. excluded.
3. **Secure Credential Handling:** Public repos prioritized initially. Private repo PATs reside in-memory only, **never** embedded in Git URLs or CLI arguments (using `GIT_ASKPASS` / credential streaming), and enabled only after passing dedicated security tests.
4. **Dual-Tab UI Navigation:** Top-level tabs: (1) *Repository Analysis & Improvement (Phase 2)*, (2) *Single Manifest Review (Phase 1)*, preserving all Phase 1 features.
5. **Deterministic Compatibility Distinction:** Unambiguous distinction between Candidate Versions (metadata), Resolver Results (constraints), AI Risk Assessments (advisory only from Ollama), and Empirical Test Validation.
6. **Cross-Version Python Support:** Python 3.10–3.14 support with `tomli` fallback when `tomllib` is absent.
7. **Artifact Integrity Verification:** Patches validated using `git apply --check` with binary change reporting; ZIP archives validated with `testzip()`.
8. **Milestone-by-Milestone Execution:** Iterative delivery starting strictly with Milestone 1, verifying test baselines after each milestone.

---

## 2. Existing Project State & Baseline Assessment

### 2.1 Git Status & History
- **Current Branch:** `main` (synchronized with `origin/main`).
- **Uncommitted Changes:** `src/app.py` has minor cosmetic label changes that are **preserved intact**.
- **Phase 1 Baseline:** 27/27 unit and integration tests passing in 0.12s.

### 2.2 Installed Runtime & Discovered Models
- **Python Version:** 3.14.3.
- **Ollama CLI:** `/usr/local/bin/ollama` (v0.35.0) at `http://127.0.0.1:11434`.
- **Installed Models:** `gemma3:4b` (3.3 GB), `llama3.1:8b` (4.9 GB).

---

## 3. Proposed System Architecture

### 3.1 Architectural Schematic

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                Streamlit Web Interface                                 │
│                                      (src/app.py)                                      │
│  ┌──────────────────────────────────────────────────┐ ┌──────────────────────────────┐ │
│  │    Tab 1: Repository Platform (Phase 2)          │ │  Tab 2: Single Manifest      │ │
│  │  - GitHub URL & Secure In-Memory PAT             │ │         (Phase 1 Preserved)  │ │
│  │  - Dynamic Ollama Model Selector                 │ │  - Text Paste / File Upload  │ │
│  │  - Configurable File Inspection Limit (Default 10│ │  - Local Catalog Evaluation  │ │
│  │  - Primary: "Analyze & Improve Repository"       │ │  - Requirements Diff         │ │
│  │  - Live Pipeline Progress & Accordions           │ │  - Download Updated File     │ │
│  │  - Download Actions (.patch, .zip, Report)       │ └──────────────────────────────┘ │
│  └───────────────────────────┬──────────────────────┘                                  │
└──────────────────────────────┼─────────────────────────────────────────────────────────┘
                               │ Dispatches Orchestration
                               ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                           Pipeline Orchestration Layer                                 │
│                           (src/pipeline/orchestrator.py)                               │
│     Stage 1: Clone ──► Stage 2: Discover ──► Stage 3: Outdated ──► Stage 4: AI Risks   │
│     Stage 8: Export ◄── Stage 7: Validate ◄── Stage 6: Diff ◄── Stage 5: AI Code Fix  │
└──────┬───────────────────────┬─────────────────────────┬───────────────────────┬───────┘
       │                       │                         │                       │
       ▼                       ▼                         ▼                       ▼
┌──────────────┐       ┌──────────────┐          ┌──────────────┐        ┌──────────────┐
│  Workspace   │       │  Dependency  │          │  AI Engine   │        │  Validation  │
│  & Git Svcs  │       │  & Manifest  │          │   (Ollama)   │        │   & Sandbox  │
│              │       │              │          │              │        │              │
│ - git clone  │       │ - discover   │          │ - HTTP REST  │        │ - ast.parse  │
│   (--depth 1)│       │   manifests  │          │ - /api/tags  │        │ - py_compile │
│ - In-memory  │       │ - req.txt    │          │ - JSON schema│        │ - ruff lint  │
│   PAT auth   │       │   (Phase 1)  │          │ - risk prompt│        │ - DEDICATED  │
│ - reference/ │       │ - pyproject  │          │ - code fix   │        │   DOCKER     │
│   (pristine) │       │   (tomllib/  │          │   prompt     │        │   SANDBOX    │
│ - working/   │       │    tomli)    │          │ - validation │        │   (or mark   │
│   (mutable)  │       │ - comparator │          │ - zero shell │        │   NOT_       │
│ - cleanup    │       │   & metadata │          │   execution  │        │   RUNNABLE)  │
└──────────────┘       └──────────────┘          └──────────────┘        └──────────────┘
       │                                                                         │
       └───────────────────────────────────┬─────────────────────────────────────┘
                                           ▼
                       ┌──────────────────────────────────────┐
                       │           Export Service             │
                       │        (src/services/exporter.py)    │
                       │  - Unified .patch (git apply --check)│
                       │  - Project .zip (testzip() checked)  │
                       │  - Comprehensive Markdown Report     │
                       └──────────────────────────────────────┘
```

---

## 4. Updated Folder Structure

```text
Dependency Upgrade Assistant/
├── .github/workflows/ci.yml
├── aidlc-docs/
│   ├── approvals/
│   │   ├── approval-log.md                 # Phase 1 approval log (preserved)
│   │   └── phase-2-approval-log.md         # Phase 2 approval log & amendments
│   ├── prompts/
│   │   ├── initial-user-prompt.md          # Phase 1 prompt (preserved)
│   │   ├── approval-and-authorization.md   # Phase 1 authorization (preserved)
│   │   └── phase-2-initial-prompt.md       # Phase 2 user prompt
│   └── requirements/
│       ├── dependency-upgrade-assistant-requirements.md # Phase 1 SRS (preserved)
│       └── phase-2-requirements.md         # Phase 2 SRS (SRS-DUA-002 v2.1.0)
├── plan/
│   ├── dependency-upgrade-assistant-plan.md # Phase 1 plan (preserved)
│   └── phase-2-implementation-plan.md      # Phase 2 execution plan (this file)
├── sample-data/
│   ├── requirements-clean.txt              # Phase 1 sample (preserved)
│   ├── requirements-complex.txt            # Phase 1 sample (preserved)
│   ├── requirements-outdated.txt           # Phase 1 sample (preserved)
│   └── sample_repo/                        # Test fixture repository for Phase 2
├── src/
│   ├── __init__.py
│   ├── app.py                              # Streamlit app (Dual-tab: Repo + File)
│   ├── core/                               # PRESERVED: Phase 1 core engine
│   │   ├── comparator.py
│   │   ├── diff_generator.py
│   │   ├── models.py
│   │   └── parser.py
│   ├── data/                               # PRESERVED: Phase 1 catalog
│   │   ├── catalog_loader.py
│   │   └── package_catalog.json
│   ├── services/                           # Headless infrastructure services
│   │   ├── __init__.py
│   │   ├── git_service.py                  # Secure shallow clone, in-memory PAT
│   │   ├── workspace_manager.py            # Dual-workspace (reference/working) & cleanup
│   │   ├── ollama_client.py                # Ollama REST client & model discovery
│   │   └── exporter.py                     # .patch, .zip, and markdown report generator
│   ├── analysis/                           # Dependency & Code Analysis engines
│   │   ├── __init__.py
│   │   ├── manifest_scanner.py             # Multi-manifest discovery (req.txt, pyproject)
│   │   ├── pyproject_parser.py             # PEP 621 & Poetry pyproject.toml parser (tomllib/tomli)
│   │   ├── dependency_analyzer.py          # Unified dependency evaluator + AI risk queries
│   │   ├── code_inspector.py               # Configurable AST scanner & issue detection
│   │   └── code_improver.py                # AI improvement synthesis & AST pre-validation
│   ├── validation/                         # Automated validation & sandbox engine
│   │   ├── __init__.py
│   │   ├── validator.py                    # Multi-stage validation coordinator
│   │   ├── sandbox_manager.py              # Dedicated Docker isolation & capability check
│   │   └── test_runner.py                  # Sandboxed pytest runner & classifier
│   └── pipeline/                           # One-click workflow orchestrator
│       ├── __init__.py
│       └── orchestrator.py                 # 8-stage state machine & result aggregation
├── tests/                                  # 27 baseline tests + Phase 2 test suite
│   ├── conftest.py
│   ├── test_*.py                           # Existing Phase 1 tests (preserved)
│   ├── test_git_service.py                 # Milestone 1: Git clone and secure auth tests
│   ├── test_workspace_manager.py           # Milestone 1: Dual-workspace creation & cleanup tests
│   ├── test_ollama_client.py               # Milestone 2: Ollama REST discovery & JSON tests
│   ├── test_manifest_scanner.py            # Milestone 3: Manifest scanner & pyproject tests
│   ├── test_code_inspector.py              # Milestone 4: Configurable AST inspection tests
│   ├── test_code_improver.py               # Milestone 4: AI patch application & AST validation tests
│   ├── test_sandbox_manager.py             # Milestone 5: Docker sandbox isolation & fallback tests
│   ├── test_validator.py                   # Milestone 5: Validation coordinator tests
│   ├── test_exporter.py                    # Milestone 6: Patch git apply check & zip integrity tests
│   └── test_orchestrator_integration.py    # Milestone 7: End-to-end repository pipeline test
├── README.md
├── pyproject.toml
└── requirements.txt
```

---

## 5. Implementation Milestones (Iterative Delivery)

> [!IMPORTANT]
> **Strict AI-DLC Iterative Protocol:** Implementation begins with **Milestone 1 only**. After completing Milestone 1 and verifying that all tests pass, results will be reported to the user before requesting authorization to proceed to Milestone 2.

```
[x] Milestone 1: Workspace Management & Git Acquisition Service (COMPLETED & VERIFIED)
    [x] 1.1 Implement src/services/git_service.py:
            - GitHub URL validation.
            - Public repo shallow clone (--depth 1).
            - Secure in-memory PAT authentication (using GIT_ASKPASS/IPC; never in URL or CLI args).
            - Absolute non-push guarantee (no remote push or mutation).
    [x] 1.2 Implement src/services/workspace_manager.py:
            - Dual-copy temporary workspace (reference/ pristine, working/ mutable).
            - Context manager lifecycle with guaranteed try...finally cleanup.
    [x] 1.3 Implement tests:
            - tests/test_git_service.py (URL parsing, public clone, secure credential handling).
            - tests/test_workspace_manager.py (workspace creation, file isolation, cleanup).
    [x] 1.4 Execute test suite (.venv/bin/pytest -v) and verify 27 baseline + new M1 tests pass (43/43 passing).

[x] Milestone 2: Ollama Client & Dynamic Model Discovery (COMPLETED & VERIFIED)
    [x] 2.1 Implement src/services/ollama_client.py (REST client, /api/tags, /api/chat JSON mode).
    [x] 2.2 Implement model discovery, health checks, and JSON response schema validation.
    [x] 2.3 Implement tests/test_ollama_client.py (mocked REST responses, error handling).

[x] Milestone 3: Manifest Scanner & Dependency Engine Integration (COMPLETED & VERIFIED)
    [x] 3.1 Implement src/analysis/manifest_scanner.py (multi-file requirements.txt discovery, nested -r/-c resolution, circular detection).
    [x] 3.2 Implement src/analysis/pyproject_parser.py (tomllib / tomli fallback parser, PEP 621, PEP 735, Poetry).
    [x] 3.3 Implement src/analysis/dependency_analyzer.py (deterministic PyPI metadata + Ollama advisory risk prompts).
    [x] 3.4 Implement tests/test_manifest_scanner.py, tests/test_pyproject_parser.py, and tests/test_dependency_analyzer.py.

[x] Milestone 4: Source Code Inspector & AI Improvement Engine (COMPLETED & VERIFIED)
    [x] 4.1 Implement src/analysis/code_inspector.py (configurable limit, default 10, priority scoring, inclusion/exclusion list).
    [x] 4.2 Implement src/analysis/code_improver.py (Ollama code improvements, AST pre-validation, working-copy application).
    [x] 4.3 Implement tests/test_code_inspector.py and tests/test_code_improver.py.

[x] Milestone 5: Dedicated Sandbox Validation Engine (COMPLETED & VERIFIED)
    [x] 5.1 Implement src/validation/sandbox_manager.py (Docker detection, non-root, no privilege, no network, no socket mount).
    [x] 5.2 Implement src/validation/test_runner.py (Docker execution or NOT_RUNNABLE classification).
    [x] 5.3 Implement src/validation/validator.py (syntax compilation, Ruff linting, test aggregation).
    [x] 5.4 Implement tests/test_sandbox_manager.py and tests/test_validator.py.

[x] Milestone 6: Artifact Exporter with Integrity Validation (COMPLETED & VERIFIED)
    [x] 6.1 Implement src/services/exporter.py:
            - Unified .patch generator validated via git apply --check.
            - Improved project .zip archive excluding .git/caches/secrets, validated via testzip().
            - Comprehensive Markdown analysis report with tier distinctions.
    [x] 6.2 Implement tests/test_exporter.py.

[x] Milestone 7: Pipeline Orchestrator & Streamlit UI Integration (COMPLETED & VERIFIED)
    [x] 7.1 Implement src/pipeline/orchestrator.py (8-stage state machine, fault tolerance).
    [x] 7.2 Update src/app.py with two top-level tabs (Repository Analysis vs Single Manifest).
    [x] 7.3 Implement tests/test_orchestrator_integration.py.

[x] Milestone 8: End-to-End Verification, CI Matrix & Documentation (COMPLETED & VERIFIED)
    [x] 8.1 Create sample repo fixture under sample-data/sample_repo/.
    [x] 8.2 Verify 100% pass on all 27 Phase 1 tests + all new Phase 2 tests (146/146 passing).
    [x] 8.3 Update README.md with Phase 2 documentation and usage guide.
```

---

## 6. Acceptance Criteria Checklist

- [x] **AC-01:** Platform accepts public GitHub URLs and shallow-clones into temporary dual-workspace (`reference/` and `working/`).
- [x] **AC-02:** Platform supports private repo credentials in-memory without exposing PAT in URLs, CLI args, logs, or disk.
- [x] **AC-03:** Platform connects to local Ollama runtime and dynamically discovers installed models (`gemma3:4b`, `llama3.1:8b`).
- [x] **AC-04:** Platform discovers both `requirements.txt` and `pyproject.toml` manifests across Python 3.10–3.14 (`tomllib`/`tomli`).
- [x] **AC-05:** Outdated dependencies are identified using deterministic metadata and PEP 440 comparisons (`packaging`).
- [x] **AC-06:** Ollama generates advisory structured JSON compatibility risk explanations without claiming proof of compatibility.
- [x] **AC-07:** Python source files are inspected up to configurable limit (default 10) with priority scoring and reporting of included/excluded files.
- [x] **AC-08:** Proposed AI improvements are validated with `ast.parse` before application; syntax-invalid changes are rejected.
- [x] **AC-09:** Modifications applied strictly to `working/`; `reference/` remains untouched.
- [x] **AC-10:** Repository tests run strictly inside dedicated Docker sandbox (non-root, no privilege, no network, no socket mount); if unavailable, marked `NOT_RUNNABLE`.
- [x] **AC-11:** Validation results clearly categorize passed vs. failed vs. not runnable tests.
- [x] **AC-12:** Generated `.patch` file validated using `git apply --check` against clean original repository copy.
- [x] **AC-13:** Generated `.zip` archive excludes `.git`/caches/secrets and is validated via `testzip()` before download.
- [x] **AC-14:** Comprehensive Markdown analysis report includes manifest findings, file inclusion/exclusion list, diffs, and validation logs.
- [x] **AC-15:** "Analyze & Improve Repository" button orchestrates the 8-stage pipeline with live progress tracking.
- [x] **AC-16:** System never executes `git push`, remote branch creation, or PR creation.
- [x] **AC-17:** System never executes arbitrary shell commands generated by the LLM.
- [x] **AC-18:** All 27 existing Phase 1 tests pass with zero regressions.
- [x] **AC-19:** All new Phase 2 unit and integration tests pass cleanly.
- [ ] **AC-20:** UI provides two top-level tabs: Phase 2 Repository Platform and Phase 1 Single Manifest Review (fully operational).
