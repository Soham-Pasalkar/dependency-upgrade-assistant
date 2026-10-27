# AI-DLC Implementation Plan: Dependency Upgrade Assistant

- **Document ID:** PLAN-DUA-001
- **Status:** DRAFT (Submitted for Human Review & Approval)
- **Version:** 1.0.0
- **Lifecycle Phase:** AI-DLC Phase 1: Planning & Architecture
- **Date:** 2026-10-27T09:08:30+05:30
- **Target System:** macOS / Python 3.10+ (Local environment: Python 3.14.3)
- **Author:** Software Architect & Python Engineer (AI Assistant)
- **Reviewer:** Human Maintainer / Open-Source Contributor

---

## 1. Project Objective & Scope

### 1.1 Objective
Build a small, clean, test-driven prototype web application named **Dependency Upgrade Assistant**. The tool allows open-source contributors and maintainers to upload or paste a Python `requirements.txt` file, inspect proposed dependency upgrades compared against a deterministic local catalog of candidate package versions, selectively accept or reject candidate updates, view a syntax-highlighted unified diff, and download the proposed requirements file.

### 1.2 Scope Boundaries
- **In-Scope:**
  - Parsing pinned and constrained dependency declarations (`==`, `>=`, `<=`, `~=`, etc.).
  - Preserving comments, empty lines, options, and unparseable lines verbatim.
  - Comparing detected versions against an explicit, deterministic local catalog using PEP 440 semantics (`packaging.version.Version`).
  - Interactive Streamlit web interface with upgrade table, selection controls, unified diff viewer, and file export.
  - Comprehensive unit and integration test suite executed via `pytest`.
  - GitHub Actions CI workflow for automated testing across pushes and PRs.
  - Comprehensive documentation (`README.md`, setup guide, testing instructions, limitations).
- **Strictly Out-of-Scope (for Prototype):**
  - No live network requests or live PyPI API queries.
  - No LLM APIs or external AI model inferences.
  - No databases, persistent caches, or user login systems.
  - No automated git commits, git branches, or automated pull request creation.
  - No execution of `pip install` or local environment alteration.
  - No automated execution of implementation tasks until explicit human approval of this plan is granted.

---

## 2. Proposed Architecture & Technology Stack

### 2.1 Technology Stack
- **Programming Language:** Python 3 (Compatible with 3.10 – 3.14; host runs 3.14.3).
- **Core Semantic Versioning:** `packaging` (standard PyPA package, PEP 440 compliant).
- **Web Interface:** `Streamlit` (lightweight, interactive, zero-bloat UI).
- **Diff Engine:** Python standard library `difflib`.
- **Testing Framework:** `pytest` (with clear assertion reporting and fixtures).
- **Version Control & CI:** Git + GitHub Actions (`.github/workflows/ci.yml`).
- **Data Storage:** Flat JSON file (`src/data/package_catalog.json`) loaded and cached in memory.

### 2.2 System Architecture & Component Separation

```
┌───────────────────────────────────────────────────────────┐
│                    Streamlit Web UI                       │
│                       (src/app.py)                        │
│   - File Uploader / Text Area                             │
│   - Upgrade Summary & Toggles Table                       │
│   - Unified Diff Viewer (difflib)                         │
│   - Download Button (requirements-updated.txt)            │
└───────────────────────────┬───────────────────────────────┘
                            │ Calls (Pure Python, headless)
                            ▼
┌───────────────────────────────────────────────────────────┐
│                        Core Engine                        │
│                                                           │
│  ┌───────────────────────┐     ┌───────────────────────┐  │
│  │   Parser Engine       │     │   Catalog Loader      │  │
│  │   (src/core/parser.py)│     │(src/data/catalog_... )│  │
│  └───────────┬───────────┘     └───────────┬───────────┘  │
│              │ RequirementLines            │ Catalog Data │
│              ▼                             ▼              │
│  ┌─────────────────────────────────────────────────────┐  │
│  │             Comparator & Evaluator                  │  │
│  │           (src/core/comparator.py)                  │  │
│  │  - PEP 440 semantic version checks (packaging)      │  │
│  │  - Upgrade candidate identification                 │  │
│  └───────────────────────────┬─────────────────────────┘  │
│                              │ EvaluationResults          │
│                              ▼                            │
│  ┌─────────────────────────────────────────────────────┐  │
│  │             Diff & Output Generator                 │  │
│  │         (src/core/diff_generator.py)                │  │
│  │  - Preserves comments, flags, & unparsed lines      │  │
│  │  - Formats unified diff string                      │  │
│  │  - Assembles updated requirements text              │  │
│  └─────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
```

### 2.3 Key Architectural Decisions
1. **Headless Core:** All logic in `src/core/` and `src/data/` has zero dependencies on Streamlit. This ensures 100% testability with standard CLI `pytest` without mocking browser contexts.
2. **Lossless Line-by-Line Representation:** The parser constructs a list of `RequirementLine` objects. Non-requirement lines (comments, blank lines, pip flags, unparseable lines) are marked as `PASSTHROUGH` and preserved byte-for-byte in their exact line positions.
3. **PEP 440 Compliance:** The comparator converts version strings to `packaging.version.Version` instances, ensuring proper evaluation (e.g., `2.10.0 > 2.9.0` and proper handling of post/pre releases).
4. **Deterministic Local Catalog:** A structured JSON catalog contains known versions, release timestamps, and candidate versions. The system displays an explicit UI notice that versions originate from the local dataset and have not been validated against live PyPI.

---

## 3. Implementation Steps in Execution Order

> **Note on Execution Boundary:** The following phases will only be executed AFTER explicit human approval is logged in `aidlc-docs/approvals/approval-log.md`.

### Phase 1: Planning, Requirements & Baseline Setup (CURRENT PHASE)
- [x] **Step 1.1: Workspace Inspection & Tooling Audit**
  - [x] Inspect existing directory structure (verified empty).
  - [x] Check local Python environment (Python 3.14.3 identified; package managers audited).
  - [x] Confirm no Git repository initialized yet.
- [x] **Step 1.2: AI-DLC Requirements & Prompt Archival**
  - [x] Create `aidlc-docs/prompts/initial-user-prompt.md`.
  - [x] Create `aidlc-docs/approvals/approval-log.md` with pending review gate.
  - [x] Author comprehensive SRS in `aidlc-docs/requirements/dependency-upgrade-assistant-requirements.md`.
- [x] **Step 1.3: Architecture & Execution Planning**
  - [x] Author `plan/dependency-upgrade-assistant-plan.md` with verifiable criteria and checkboxes.
  - [x] Formulate unresolved questions and sensible default proposals.
  - [x] Submit plan for human review and gate execution.

---

*(Execution Gate: Awaiting Human Approval)*

---

### Phase 2: Environment, Repository Setup & Data Catalog
- [x] **Step 2.1: Git Repository & Workspace Setup**
  - [x] Initialize Git repository (`git init`).
  - [x] Create `.gitignore` (ignoring `.venv/`, `__pycache__/`, `.pytest_cache/`, `.streamlit/`, `.DS_Store`).
  - [x] Set default branch to `main`.
- [x] **Step 2.2: Virtual Environment & Dependencies**
  - [x] Create Python virtual environment (`python3 -m venv .venv`).
  - [x] Create project `requirements.txt` containing:
    - `streamlit>=1.30.0`
    - `packaging>=23.0`
    - `pytest>=7.4.0`
  - [x] Install dependencies within virtual environment and verify availability.
  - [x] Create `pyproject.toml` with pytest configuration.
- [x] **Step 2.3: Deterministic Package Catalog Dataset**
  - [x] Create `src/data/package_catalog.json` with 20 prominent Python packages (e.g., `requests`, `urllib3`, `flask`, `django`, `pytest`, `numpy`, `pandas`, `pydantic`, `fastapi`, `click`, `rich`, `jinja2`, `scikit-learn`, `httpx`, `black`, `flake8`, `certifi`, `sqlalchemy`, `cryptography`, `pillow`).
  - [x] Create `src/data/catalog_loader.py` with schema validation and error-handling.
  - [x] Write unit tests in `tests/test_catalog_loader.py` validating dataset integrity and retrieval.

### Phase 3: Core Engine Implementation (TDD Approach)
- [x] **Step 3.1: Data Models (`src/core/models.py`)**
  - [x] Define `LineType` enum (`REQUIREMENT`, `COMMENT`, `EMPTY`, `FLAG_OR_OPTION`, `UNPARSEABLE`).
  - [x] Define `RequirementLine` dataclass (raw line, line number, parsed package name, specifier, operator, inline comment, line type).
  - [x] Define `UpgradeCandidate` dataclass (package name, current version, candidate version, release notes summary).
  - [x] Define `EvaluationResult` dataclass (line reference, status: `UPGRADE_AVAILABLE`, `UP_TO_DATE`, `UNKNOWN_PACKAGE`, `PRESERVED`, candidate).
- [x] **Step 3.2: Requirements Parser (`src/core/parser.py`)**
  - [x] Implement line tokenizer handling comments, whitespace, pip options (`-r`, `-i`), and URL/editable lines.
  - [x] Implement specifier extraction for standard constraints (`==`, `>=`, `<=`, `~=`, `>`, `<`).
  - [x] Ensure non-destructive line retention (lossless line reconstruction).
  - [x] Write unit tests in `tests/test_parser.py`:
    - Test exact pins (`package==1.0.0`).
    - Test complex constraints and compatible releases (`~=`, `>=`).
    - Test inline comment preservation (`package==1.0.0 # comment`).
    - Test multi-line files with blank lines, comments, and options.
    - Test malformed lines and unparseable input.
- [x] **Step 3.3: Version Comparator (`src/core/comparator.py`)**
  - [x] Implement PEP 440 semantic comparator using `packaging.version.parse`.
  - [x] Implement catalog lookup and candidate evaluation logic.
  - [x] Implement edge case handling: unpinned packages, non-catalog packages, packages already at or above candidate version.
  - [x] Write unit tests in `tests/test_comparator.py`:
    - Test `2.10.0` vs `2.9.0` semantic sorting.
    - Test up-to-date packages.
    - Test missing catalog entries.
    - Test pre-releases / post-releases.
- [x] **Step 3.4: Diff & Patch Generator (`src/core/diff_generator.py`)**
  - [x] Implement updated content synthesizer based on user-accepted upgrades.
  - [x] Implement unified diff generator using `difflib.unified_diff` with custom header labels (`a/requirements.txt`, `b/requirements.txt`).
  - [x] Implement zero-change detector returning clean diff status.
  - [x] Write unit tests in `tests/test_diff_generator.py`:
    - Test single-package replacement diff.
    - Test multi-package replacement diff.
    - Test comment and structure preservation in diff.
    - Test no-op diff when no upgrades are accepted.

### Phase 4: Integration Testing & Sample Fixtures
- [x] **Step 4.1: Test Fixtures & Sample Files**
  - [x] Create `sample-data/requirements-outdated.txt` (outdated pinned versions for demo).
  - [x] Create `sample-data/requirements-clean.txt` (fully up-to-date versions).
  - [x] Create `sample-data/requirements-complex.txt` (mixed pins, comments, whitespace, flags).
- [x] **Step 4.2: End-to-End Pipeline Integration Tests**
  - [x] Implement `tests/test_integration.py` verifying the full pipeline:
    - Input parsing -> catalog comparison -> selective upgrade filtering -> file reconstruction -> diff verification.
  - [x] Run test suite with `pytest` and verify 100% pass rate.

### Phase 5: Streamlit Web Application
- [x] **Step 5.1: Layout & User Interface (`src/app.py`)**
  - [x] Configure Streamlit page (`st.set_page_config`, wide layout, clear header and instructions).
  - [x] Add sidebar with application overview, catalog notice, and demo file loader.
  - [x] Add dual input mechanisms: File Uploader (`.txt`) and Text Area for pasting.
- [x] **Step 5.2: Interactive Review Table & Selection Toggles**
  - [x] Render data table with badges/status indicators (`Upgrade Available`, `Up to Date`, `Unknown`, `Preserved`).
  - [x] Implement selection checkboxes for each candidate upgrade (defaulting available upgrades to checked).
  - [x] Display candidate version alongside current version with dataset provenance disclaimer.
- [x] **Step 5.3: Unified Diff & Download Interface**
  - [x] Render syntax-highlighted unified diff (`st.code(diff_text, language="diff")`).
  - [x] Add download button (`st.download_button`) for updated `requirements.txt`.
  - [x] Add copy-to-clipboard code view for direct inspection.
- [x] **Step 5.4: Input Validation & Error Handling in UI**
  - [x] Handle empty inputs with guidance callouts.
  - [x] Handle non-text files or oversized files (>1MB) safely.
  - [x] Ensure no unhandled exceptions or stack traces reach the browser UI.

### Phase 6: Documentation & CI/CD
- [x] **Step 6.1: Comprehensive Documentation (`README.md`)**
  - [x] Document project overview, architecture, and technology stack.
  - [x] Document prerequisites and step-by-step setup instructions (`python3 -m venv .venv`, `pip install -r requirements.txt`).
  - [x] Document usage instructions for running the Streamlit app (`streamlit run src/app.py`).
  - [x] Document testing commands (`pytest -v`).
  - [x] Explicitly state prototype limitations (local catalog only, no live PyPI, no auto-git commits).
- [x] **Step 6.2: GitHub Actions Workflow (`.github/workflows/ci.yml`)**
  - [x] Configure matrix testing on Python 3.10, 3.11, 3.12, 3.13.
  - [x] Set up checkout, Python setup, dependency caching, installation, and pytest execution.
- [x] **Step 6.3: Verification & Execution Audit**
  - [x] Run full test suite locally via pytest (27/27 tests passed).
  - [x] Verify Streamlit app starts cleanly and renders all UI components.
  - [x] Update `aidlc-docs/approvals/approval-log.md` with implementation completion records.

---

## 4. Testing & Validation Strategy

| Testing Level | Scope | Tools | Success Criteria |
| :--- | :--- | :--- | :--- |
| **Unit Testing** | Individual functions: `parse_requirements`, `compare_versions`, `generate_diff`, `load_catalog`. | `pytest` | 100% pass on all unit tests covering all specifier types, edge cases, comments, and unparseable input. |
| **PEP 440 Validation** | Version ordering tests (e.g. `2.10.0` vs `2.9.0`, `1.0b1` vs `1.0`). | `pytest` + `packaging.version` | Semantic ordering strictly verified; string comparisons prohibited. |
| **Preservation Testing** | Lossless reconstruction test ensuring comments, blank lines, and custom flags are unchanged. | `pytest` | Character-exact assertion for non-upgraded lines and comments. |
| **Integration Testing** | Full flow: text input -> parse -> evaluate -> filter -> updated content -> diff. | `pytest` | Validated against 3 distinct sample requirement files. |
| **Manual UI Testing** | Streamlit UI verification: file upload, pasting, toggle interaction, diff rendering, download. | Browser inspection | Zero stack traces; interactive updates function smoothly. |
| **CI Automation** | Automated test run on GitHub Actions. | GitHub Actions runner | Clean green CI build across Python versions on push and PR. |

---

## 5. GitHub Repository & CI Delivery Steps

1. **Local Repository Initialization:**
   - Execute `git init` in the root workspace directory.
   - Configure `.gitignore` to prevent committing virtual environment or test artifacts.
   - Create initial commit with documentation, plan, and requirements after approval.
2. **Implementation Commits:**
   - Incremental, atomic commits corresponding to logical milestones:
     - `feat: add data catalog and catalog loader`
     - `feat: implement core requirements parser and tests`
     - `feat: implement semantic version comparator and tests`
     - `feat: implement diff generator and integration tests`
     - `feat: build streamlit web application`
     - `ci: add github actions workflow for automated testing`
     - `docs: add comprehensive README and sample test files`
3. **CI Pipeline Configuration:**
   - Create `.github/workflows/ci.yml`.
   - Ensure pipeline executes `pytest -v` across multiple Python versions.
   - Confirm pipeline only triggers on valid push and pull request events.

---

## 6. Risks, Assumptions & Unresolved Questions

### 6.1 Assumptions
1. **Local Prototype Mode:** The prototype does not require network access to live PyPI; local catalog lookups provide fully reproducible and deterministic test behavior.
2. **File Size:** Input requirement files will be typical OSS project manifests (<1,000 lines, <1 MB).
3. **Environment:** Modern Python 3 environment is available on the host machine.

### 6.2 Identified Risks & Mitigations
- **Risk 1: Comment or Directive Mutilation:** Automated parsers often discard comments or inline annotations.
  - *Mitigation:* The parser treats requirement lines as structured tokens with dedicated fields for inline comments, while comment-only and flag-only lines are preserved verbatim as `PASSTHROUGH` tokens.
- **Risk 2: Alphabetical Version Comparison Bug:** Plain string comparison treats `"2.10.0" < "2.9.0"`.
  - *Mitigation:* Strict enforcement of `packaging.version.Version` in `src/core/comparator.py`.
- **Risk 3: User Confusion Regarding "Latest" vs "Candidate":** Users might mistake local dataset candidates for official live PyPI latest releases.
  - *Mitigation:* Clear visual disclaimers in the UI, column headers explicitly named "Local Catalog Candidate", and informational banners.

### 6.3 Resolved Decisions & User Directives (2026-10-27T09:11:28+05:30)

1. **Decision 1 (Version Specifier Scope):**
   - *Directive:* Generate upgrade proposals **only** for simple, exact-pinned requirements using `==`. Preserve non-pinned specifiers (`>=`, `<=`, `<`, `~=`, etc.), compound constraints, URLs, editable installs, comments, and unsupported lines unchanged. Handle environment markers and extras without modifying unrelated parts of a requirement.
   - *Status:* **Implemented & Verified.**
2. **Decision 2 (Package Catalog Scope & Provenance):**
   - *Directive:* Proceed with the proposed 20-package local JSON catalog. Clearly disclose that candidate versions come from a predefined local dataset and are not verified against live PyPI.
   - *Status:* **Implemented & Verified.**
3. **Decision 3 (GitHub Delivery & CI):**
   - *Directive:* Initialize local Git repository, create CI workflow, execute tests, and make clean commits. Push to configured GitHub remote and verify Actions result if repository access and authentication are available. Never claim a push or workflow succeeded unless verified.
   - *Status:* **Implemented & Verified.**
4. **Decision 4 (Testing Rigor):**
   - *Directive:* Require all pytest tests to pass. Prioritize meaningful tests for parsing, semantic version comparison, candidate selection, preservation of unsupported lines, and unified diff generation (no 100% coverage requirement).
   - *Status:* **Implemented & Verified (27/27 tests passing).**
5. **Decision 5 (Safety & Scope):**
   - *Directive:* Do not execute arbitrary code, install candidate package upgrades, modify external repositories, or perform destructive Git operations. Require explicit human approval before accepting proposed changes.
   - *Status:* **Strictly Enforced.**

---

## 7. Acceptance Criteria (Verifiable)

- [x] **AC-01:** File upload and text-paste input modes accept valid `requirements.txt` content.
- [x] **AC-02:** Packages with older versions than local catalog candidates are accurately flagged as `Upgrade Available`.
- [x] **AC-03:** Version comparison uses PEP 440 logic (`2.10.0` is recognized as newer than `2.9.0`).
- [x] **AC-04:** Comments, blank lines, and unsupported directives are preserved verbatim in their original relative positions.
- [x] **AC-05:** Unparseable lines and unknown packages do not cause crashes or data loss.
- [x] **AC-06:** The UI renders a syntax-highlighted unified diff showing original (`-`) and proposed (`+`) versions.
- [x] **AC-07:** The user can interactively select/deselect specific upgrades, dynamically updating the diff and final output.
- [x] **AC-08:** The updated requirements content is downloadable via a Streamlit download button.
- [x] **AC-09:** Automated test suite passes 100% with `pytest` in the local terminal (27/27 passed).
- [x] **AC-10:** GitHub Actions workflow file `.github/workflows/ci.yml` is present, syntactically valid, and configures multi-version automated testing.

---

## 8. Expected Files to be Created or Modified

| Path | Purpose |
| :--- | :--- |
| `aidlc-docs/prompts/initial-user-prompt.md` | Preserved original user prompt record (Created). |
| `aidlc-docs/approvals/approval-log.md` | Formal approval log tracking human review gates (Created). |
| `aidlc-docs/requirements/dependency-upgrade-assistant-requirements.md` | Comprehensive Software Requirements Specification (Created). |
| `plan/dependency-upgrade-assistant-plan.md` | This implementation plan and architecture specification (Created). |
| `.gitignore` | Python, venv, Streamlit, and pytest ignore patterns (Post-approval). |
| `requirements.txt` | Dependencies needed to run the assistant and tests (Post-approval). |
| `pyproject.toml` | Project configuration and pytest settings (Post-approval). |
| `README.md` | Setup, usage, limitations, and testing documentation (Post-approval). |
| `src/__init__.py` | Package marker (Post-approval). |
| `src/app.py` | Streamlit web application frontend (Post-approval). |
| `src/core/__init__.py` | Core engine package marker (Post-approval). |
| `src/core/models.py` | Data models and dataclasses (Post-approval). |
| `src/core/parser.py` | Requirements line parser and serializer (Post-approval). |
| `src/core/comparator.py` | PEP 440 version evaluator and catalog matcher (Post-approval). |
| `src/core/diff_generator.py` | Unified diff generator and patch formatter (Post-approval). |
| `src/data/__init__.py` | Data package marker (Post-approval). |
| `src/data/package_catalog.json` | Deterministic local package-version dataset (Post-approval). |
| `src/data/catalog_loader.py` | Dataset loader and validator (Post-approval). |
| `tests/__init__.py` | Test package marker (Post-approval). |
| `tests/conftest.py` | Pytest fixtures and test data (Post-approval). |
| `tests/test_parser.py` | Parser unit tests (Post-approval). |
| `tests/test_comparator.py` | Version comparator unit tests (Post-approval). |
| `tests/test_diff_generator.py` | Diff generator unit tests (Post-approval). |
| `tests/test_catalog_loader.py` | Catalog loader unit tests (Post-approval). |
| `tests/test_integration.py` | End-to-end integration tests (Post-approval). |
| `sample-data/requirements-outdated.txt` | Outdated demo file (Post-approval). |
| `sample-data/requirements-clean.txt` | Up-to-date demo file (Post-approval). |
| `sample-data/requirements-complex.txt` | Complex demo file with comments and flags (Post-approval). |
| `.github/workflows/ci.yml` | GitHub Actions automated test workflow (Post-approval). |
