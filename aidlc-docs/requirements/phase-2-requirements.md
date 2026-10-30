# Software Requirements Specification (SRS)
## Phase 2: AI-Powered Repository Analysis & Improvement Platform

- **Document ID:** SRS-DUA-002
- **Status:** APPROVED WITH REQUIRED AMENDMENTS
- **Version:** 2.1.0
- **Lifecycle Phase:** AI-DLC Phase 2: Requirements & Architecture Specification
- **Approval Timestamp:** 2026-10-30T08:18:41+05:30
- **Author:** Software Architect & Python Engineer (AI Assistant)
- **Reviewer:** Human Maintainer / Open-Source Contributor
- **Base Document:** `aidlc-docs/requirements/dependency-upgrade-assistant-requirements.md` (SRS-DUA-001)

---

## 1. Executive Summary & Problem Statement

### 1.1 Problem Statement
The Phase 1 prototype of the Dependency Upgrade Assistant established a deterministic tool for parsing individual `requirements.txt` files, evaluating versions against a local catalog, generating unified diffs, and exporting updated files. However, real-world open-source software maintenance requires broader capabilities:
1. **Repository Context Inaccessibility:** Maintainers work with full repositories containing `pyproject.toml`, source code, tests, and CI configurations.
2. **Opaque Compatibility Risks:** Version jumps introduce breaking API changes, deprecations, and altered defaults that cannot be understood from version strings alone.
3. **Source Code Drift & Quality Debt:** Application code accumulates code smells, deprecated idioms, and security vulnerabilities alongside dependency obsolescence.
4. **Validation Friction & Untrusted Execution Risks:** Validating candidate upgrades by running untrusted repository test suites on the host machine presents severe security and contamination risks. Subprocesses with timeouts are insufficient; genuine sandbox isolation is mandatory.
5. **Safety Concerns with Autonomous Tools:** Unconstrained tools often push unwanted commits, open unauthorized pull requests, or leak credentials into command lines, disk logs, or diffs.

### 1.2 Project Intent & Scope (Phase 2)
Phase 2 transforms the Dependency Upgrade Assistant into an AI-powered **Repository Analysis and Improvement Platform**. 

The system accepts a GitHub repository URL, clones the repository into a temporary dual-copy workspace (`reference/` pristine vs. `working/` mutable), discovers dependency manifests (`requirements.txt` and `pyproject.toml`), performs deterministic version evaluations, leverages a locally hosted **Ollama runtime** for advisory compatibility risk explanations and code improvements, validates changes through isolated execution (syntax pre-checks, linting, and sandboxed test execution), and delivers downloadable artifacts (Git-validated unified `.patch`, verified project `.zip`, and an analysis report) — all orchestrated via a single **"Analyze & Improve Repository"** one-click workflow.

The platform preserves all Phase 1 capabilities, supports Python 3.10–3.14, never pushes to remote repositories, and strictly isolates untrusted execution.

---

## 2. Target Users & Stakeholders

| User Persona | Role & Needs | Expected Value in Phase 2 |
| :--- | :--- | :--- |
| **Open-Source Maintainer** | Triage dependencies and modernize code without host pollution or risk of regressions. | Clones full repo, receives deterministic dependency analysis with local AI risk commentary, verified code improvements, and a validated `.patch`. |
| **Open-Source Contributor** | Modernize an existing library or prepare an update safely. | Clones upstream repo safely in temporary sandbox, inspects proposed AI code fixes, verifies tests pass inside an isolated container, and downloads a patch. |
| **Security & Quality Auditor** | Air-gapped / local analysis tool to inspect code without cloud leakage. | Uses local Ollama instance (`llama3.1:8b`, `gemma3:4b`), ensuring zero telemetry, zero cloud data leakage, and full credential isolation. |

---

## 3. Scope Boundaries & Mandatory Protections

### 3.1 In-Scope Capabilities
1. **Repository Acquisition:** Public and private GitHub URL cloning into temporary scratch directories; secure, in-memory credential handling (no CLI arg or URL embedding); preservation of untouched reference clone.
2. **Local Ollama Integration:** Live REST connection to local Ollama daemon; dynamic discovery and selection of installed models; structured JSON schema communication; model response validation.
3. **Multi-Manifest Dependency Analysis:** Automatic discovery of `requirements.txt` and `pyproject.toml` (PEP 621 and Poetry styles); deterministic version resolution; AI-assisted compatibility risk explanations.
4. **Source Code Inspection & Improvement:** Configurable source file scanning (default 10 files) prioritizing core modules and dependency-affected code; AST-guided issue identification; structured AI improvement proposals applied strictly to the working copy.
5. **Dedicated Sandbox Validation:** Syntax verification (`ast.parse`), static linting (Ruff/AST), and safe test execution (`pytest`) executed strictly inside a dedicated isolated sandbox (Docker preferred with non-root, no privilege, no network, no docker socket mount). If a suitable sandbox is unavailable, tests are classified as `NOT_RUNNABLE` without polluting the host environment.
6. **Delivery Artifacts with Integrity Verification:** Generation of downloadable unified `.patch` validated with `git apply --check`, complete improved project `.zip` verified with `testzip()`, and detailed Markdown analysis report.
7. **One-Click Orchestration:** Unified execution workflow via "Analyze & Improve Repository" button with real-time stage progress reporting and fault-tolerant intermediate result retention.
8. **Dual-Tab UI:** Tab 1 for Phase 2 Repository Platform; Tab 2 for Phase 1 Single Manifest Review (full backward compatibility).

### 3.2 Explicitly Out-of-Scope (Boundaries)
1. **No Host Dependency Installation:** Never run `pip install` on the host machine for untrusted cloned repository dependencies.
2. **No Unsheltered Test Execution:** If sandbox isolation cannot be established, do not run untrusted repository tests.
3. **No External Git Operations:** Never execute `git push`, never create branches on the remote repository, never create GitHub forks, and never submit automated GitHub pull requests.
4. **No Arbitrary Command Execution:** Never execute shell scripts or system commands generated by the LLM.
5. **No Cloud LLM APIs & No Model Downloads:** Exclusively relies on local Ollama runtime without downloading models during execution.

---

## 4. Functional Requirements (FR)

### 4.1 Repository Acquisition & Workspace Management
- **FR-REPO-01 (Repository URL Ingestion):** The system shall accept standard GitHub repository URLs in HTTPS formats:
  - `https://github.com/<owner>/<repo>`
  - `https://github.com/<owner>/<repo>.git`
- **FR-REPO-02 (Public Repository Priority):** Public repositories shall be fully supported out-of-the-box without requiring authentication.
- **FR-REPO-03 (Secure Private Repository Authorization):** The system shall support private repositories via in-memory Personal Access Token (PAT):
  - Credentials must **never** be embedded in Git URLs (e.g. `https://token@github.com/...` is strictly prohibited).
  - Credentials must **never** be passed as command-line arguments to `git clone` or subprocesses (preventing exposure in `ps` output).
  - Credentials must be supplied via secure IPC mechanisms (such as `GIT_ASKPASS` or standard input streaming).
  - Credentials must **never** be written to disk, logs, diffs, reports, or exposed to Ollama or untrusted subprocesses.
  - Private repository support shall be enabled only after passing dedicated unit tests for secure credential handling.
- **FR-REPO-04 (Dual-Copy Workspace Isolation):** Upon cloning, the system shall maintain two distinct filesystem trees:
  1. `reference/`: Pristine, untouched clone of the repository at the cloned commit.
  2. `working/`: Independent working copy created by duplicating the reference tree, dedicated exclusively to applying proposed upgrades, code improvements, and running validation checks.
- **FR-REPO-05 (Shallow Clone Optimization):** Cloning shall use shallow clone parameters (`--depth 1 --single-branch`) to minimize bandwidth and disk consumption.
- **FR-REPO-06 (Non-Destructive Guarantee):** The system shall strictly forbid any git command targeting the upstream remote (`git push`, `git remote set-url`, etc.).
- **FR-REPO-07 (Automatic Workspace Cleanup):** The system shall support automatic cleanup of temporary workspace directories upon workflow completion or session termination using robust `try...finally` context handlers.

### 4.2 Ollama Runtime Integration
- **FR-OLLAMA-01 (Daemon Connectivity):** The system shall connect to the local Ollama daemon via its HTTP REST API (default endpoint: `http://127.0.0.1:11434`).
- **FR-OLLAMA-02 (Dynamic Model Discovery):** The system shall query `GET /api/tags` to retrieve all installed local models. It shall not assume or hardcode model names.
- **FR-OLLAMA-03 (Model Selection UI):** The interface shall render a dropdown selector allowing the user to choose from detected installed models (e.g., `llama3.1:8b`, `gemma3:4b`). If Ollama is unreachable, a clear status indicator and troubleshooting instructions shall be displayed.
- **FR-OLLAMA-04 (Structured JSON Prompting):** All analytical requests sent to Ollama (risk assessments, bug inspection, code improvement generation) shall enforce structured JSON responses (utilizing Ollama's `format: "json"` parameter and strict schema instructions).
- **FR-OLLAMA-05 (Response Validation & Sanitization):** Model responses shall be deserialized and strictly validated against domain dataclasses/schemas. If a model response is malformed, truncated, or fails schema validation, the system shall log the warning, attempt graceful recovery or partial extraction, and avoid failing the entire pipeline.
- **FR-OLLAMA-06 (Execution Immunity):** Under no circumstance shall any shell commands, scripts, or binary executions suggested or output by the LLM be executed by the system.

### 4.3 Dependency Analysis & Manifest Discovery
- **FR-DEP-01 (Manifest Discovery):** The scanner shall inspect the repository tree and automatically detect:
  1. `requirements.txt` (and common variants such as `requirements-dev.txt`, `requirements/*.txt`).
  2. `pyproject.toml` (project table under PEP 621, or `tool.poetry.dependencies`).
- **FR-DEP-02 (Cross-Version PyProject TOML Support):** The system shall parse `pyproject.toml` dependencies supporting Python 3.10 through 3.14:
  - Utilize standard library `tomllib` in Python 3.11+.
  - Utilize `tomli` fallback when executing on Python 3.10 (`try: import tomllib except ImportError: import tomli as tomllib`).
- **FR-DEP-03 (Deterministic Version Evaluation):** The system shall evaluate dependencies using deterministic package metadata and PEP 440 semantic logic (`packaging.version.Version`).
- **FR-DEP-04 (Preservation of Existing Pipeline):** The existing `src/core/parser.py`, `comparator.py`, `models.py`, and `diff_generator.py` modules shall remain fully functional and utilized for `requirements.txt` evaluation.
- **FR-DEP-05 (Advisory AI Risk Analysis):** For each identified upgrade candidate, the system shall prompt Ollama with package details, old version, candidate version, and known release characteristics to generate an advisory risk summary (Breaking Changes, Deprecations, Migration Precautions).
- **FR-DEP-06 (Clear Distinction of Compatibility Tiers):** The UI and reports shall unambiguously distinguish:
  1. *Candidate Versions:* Outdated vs. newer versions available in metadata.
  2. *Resolver Results:* Deterministic dependency constraint satisfaction.
  3. *AI Risk Assessment:* Advisory qualitative evaluation from Ollama (never proof of compatibility).
  4. *Test Validation:* Empirical verification from sandboxed test execution.

### 4.4 Source Code Analysis & Improvement
- **FR-CODE-01 (Source Code Scanning & Filtering):** The system shall recursively discover Python source files (`.py`) in the repository, strictly excluding virtual environments (`.venv`, `env`), version control (`.git`), caches (`__pycache__`, `.pytest_cache`), and build artifacts (`dist`, `build`, `*.egg-info`).
- **FR-CODE-02 (Configurable Inspection Scope):** The system shall support a configurable file inspection limit (default: 10 files). Files shall be prioritized:
  1. Core application modules (entrypoints, library root files).
  2. Source files importing or directly associated with identified dependency upgrades.
  3. Other application modules.
  The analysis report must explicitly declare which files were inspected and which files were excluded due to the scope ceiling.
- **FR-CODE-03 (Static Issue Identification):** The system shall analyze Python source files to identify potential bugs, code smells, maintainability issues, and security concerns.
- **FR-CODE-04 (AI Improvement Generation):** For identified issues, the system shall prompt Ollama to generate targeted, minimal code modifications formatted as file path, original snippet, replacement snippet, rationale, and issue severity.
- **FR-CODE-05 (Working-Copy Confinement):** All accepted code improvements shall be applied strictly to files within the `working/` workspace. The `reference/` copy shall remain unmodified.
- **FR-CODE-06 (Syntax Pre-Validation):** Before applying any AI-generated code change, the system shall parse the proposed code using `ast.parse()`. If a syntax error is detected, the change shall be rejected with an explanation, preventing code corruption.

### 4.5 Dedicated Sandbox Validation Engine
- **FR-VAL-01 (Syntax Verification):** The system shall execute syntax checks across all modified Python files in the working copy using `py_compile` or `ast.parse`.
- **FR-VAL-02 (Linting & Code Quality):** If Ruff or a comparable linter is available, non-destructive linting checks shall run against modified working copy files.
- **FR-VAL-03 (Dedicated Sandbox Architecture):** Untrusted repository code tests shall execute **only** inside a properly isolated sandbox manager:
  - **Preferred Sandbox (Docker):**
    - Non-root user execution.
    - No privileged mode (`--privileged` forbidden).
    - Network disabled by default (`--network none`).
    - CPU and memory resource limits enforced (`--memory`, `--cpus`).
    - Restricted filesystem access (read-only mount where possible, isolated temporary volume for working copy).
    - Strict prohibition on Docker socket mounting (never mount `/var/run/docker.sock`).
  - **Fallback when Sandbox is Unavailable:** If Docker or equivalent container isolation is not detected or operational, the engine shall **not** execute repository tests on the host environment. Repository tests shall be recorded as `NOT_RUNNABLE`.
- **FR-VAL-04 (Result Classification):** The validation engine shall unambiguously categorize test outcomes:
  - **Passed:** Tests ran in sandbox and all passed (exit code 0).
  - **Failed:** Tests ran in sandbox and one or more assertions failed (exit code 1).
  - **Not Runnable / Skipped:** Tests could not be executed due to unavailable sandbox environment, missing dependencies, or execution timeout. The system must explicitly state the reason and never misclassify missing infrastructure as a code regression.
- **FR-VAL-05 (Execution Output Capture):** The system shall capture standard output, standard error, exit codes, and duration for all executed validation commands.

### 4.6 Final Delivery & Artifact Integrity Verification
- **FR-EXP-01 (Git-Compatible Unified Patch):** The system shall generate a standard unified patch (`.patch`) encompassing all modifications (added, modified, deleted text files) between the pristine `reference/` tree and the `working/` tree.
- **FR-EXP-02 (Patch Integrity Pre-Validation):** Before offering the `.patch` file for download, the system shall validate patch integrity by executing `git apply --check` against a clean copy of the original repository. If validation fails or unsupported binary changes are detected, the system shall report these explicitly in the UI and report.
- **FR-EXP-03 (Improved Project ZIP Archive):** The system shall compress the complete working directory into a downloadable `.zip` archive:
  - Strictly exclude `.git`, virtual environments (`.venv`, `env`), caches (`__pycache__`, `.pytest_cache`), credentials, secrets, and temporary outputs.
- **FR-EXP-04 (ZIP Integrity Pre-Validation):** Before offering the `.zip` archive for download, the system shall execute `zipfile.ZipFile.testzip()` and test extraction in memory to verify archive integrity.
- **FR-EXP-05 (Comprehensive Analysis Report):** The system shall compile a detailed analysis report (rendered in UI and downloadable in Markdown format) containing:
  - Repository metadata (URL, commit SHA, analysis timestamp).
  - Manifest and dependency findings with tier differentiation (candidate, resolver, AI risk, test).
  - Inspected vs. excluded source files list.
  - Ollama model used and compatibility risk analyses.
  - Source code issues detected and resolved.
  - Unified diffs of all changed files.
  - Validation results with sandbox execution details.
- **FR-EXP-06 (Strict Non-Push Enactment):** The system shall provide no mechanisms to push commits or open pull requests to remote repositories.

### 4.7 One-Click Orchestration & Workflow
- **FR-ORCH-01 (Primary Action Button):** The UI shall feature a prominent primary action button: **"Analyze & Improve Repository"**.
- **FR-ORCH-02 (Stage-Based Pipeline):** Triggering the button shall execute an 8-stage sequential pipeline:
  1. *Stage 1: Repository Acquisition* (Clone to temporary dual workspace).
  2. *Stage 2: Manifest & Dependency Discovery* (Scan for `requirements.txt` & `pyproject.toml`).
  3. *Stage 3: Deterministic Version Evaluation* (Evaluate outdated dependencies with PyPI/packaging metadata).
  4. *Stage 4: AI Compatibility Risk Analysis* (Query Ollama for advisory upgrade risks).
  5. *Stage 5: Source Code Inspection & AI Improvement* (Scan prioritized Python files & generate fixes).
  6. *Stage 6: Working Copy Modification & Diff Computation* (Apply fixes & compute Git diffs).
  7. *Stage 7: Automated Validation* (Syntax check, lint, and sandboxed test execution).
  8. *Stage 8: Delivery Packaging* (Build validated `.patch`, `.zip`, and analysis report).
- **FR-ORCH-03 (Visual Progress & Status Tracking):** The UI shall render real-time stage progress indicators using Streamlit status widgets (`st.status`, progress bar, state badges).
- **FR-ORCH-04 (Fault Tolerance & Partial Result Preservation):** If a downstream stage encounters an error, the pipeline shall record the failure, retain all successful upstream results, and allow download of the available patches and reports.

### 4.8 User Interface & Top-Level Tabs
- **FR-UI-01 (Two Top-Level Tabs):** The application shall organize the main interface into two tabs:
  - **Tab 1: Repository Analysis & Improvement (Phase 2):** Repository URL ingestion, optional PAT, Ollama model selector, one-click analysis, multi-stage progress, diff inspector, and artifact downloads.
  - **Tab 2: Single Manifest Review (Phase 1):** Paste or upload individual `requirements.txt` file (preserving Phase 1 functionality verbatim).
- **FR-UI-02 (Ollama Connection Panel):** UI displaying Ollama daemon status (Connected / Unreachable), detected models dropdown, and latency.
- **FR-UI-03 (Stage Accordions & Summary Metrics):** Expandable panels presenting metrics for each completed stage.
- **FR-UI-04 (Interactive Patch & Diff Viewer):** Code diff viewer with syntax highlighting for each altered file.

---

## 5. Non-Functional Requirements (NFR)

### 5.1 Security & Sandbox Isolation (SEC)
- **NFR-SEC-01 (Host Environment Protection):** Untrusted repository code shall never be imported into the host process, and untrusted dependencies shall never be installed on the host. Tests execute only inside an isolated Docker sandbox or are categorized as `NOT_RUNNABLE`.
- **NFR-SEC-02 (Credential Sanitization):** Personal Access Tokens shall remain strictly in memory during active Git operations, never written to disk, never placed in command-line arguments, never embedded in URLs, and never logged or exposed to LLMs.
- **NFR-SEC-03 (Air-Gapped Privacy):** Inferences execute exclusively via local Ollama. Zero code, metadata, or tokens shall be transmitted to external cloud APIs.
- **NFR-SEC-04 (Subprocess Command Whitelisting):** Subprocess invocations shall avoid `shell=True` and invoke only approved binaries (`git`, `docker`, `pytest`, `python`, `ruff`).

### 5.2 Performance & Responsiveness (PERF)
- **NFR-PERF-01 (Shallow Clone Speed):** Shallow clone (`--depth 1`) completes within 15 seconds for typical repositories.
- **NFR-PERF-02 (Bounded LLM Inferences):** Code analysis limited to prioritized files (default 10) to keep execution time under 3 minutes on standard hardware.

### 5.3 Modularity & Maintainability (MOD)
- **NFR-MOD-01 (Decoupled Headless Services):** Core services reside in `src/` modules independent of Streamlit.
- **NFR-MOD-02 (Phase 1 Preservation):** Existing modules `src/core/*` and `src/data/*` remain intact and 100% functional.
- **NFR-MOD-03 (Cross-Version Python Support):** Compatible with Python 3.10 through 3.14.

---

## 6. Verifiable Acceptance Criteria (AC)

| ID | Criterion Statement | Verification Method |
| :--- | :--- | :--- |
| **AC-01** | Platform accepts public GitHub URLs and shallow-clones into temporary dual-workspace (`reference/` and `working/`). | Automated unit test + live clone test on public repo. |
| **AC-02** | Platform supports private repo credentials in-memory without exposing PAT in URLs, CLI args, logs, or disk. | Security unit test asserting credentials never leak to CLI args/URLs. |
| **AC-03** | Platform connects to local Ollama runtime, discovers models dynamically (`gemma3:4b`, `llama3.1:8b`), and populates UI selector. | Live Ollama query test. |
| **AC-04** | Discovers both `requirements.txt` and `pyproject.toml` manifests across Python 3.10–3.14 (`tomllib`/`tomli`). | Manifest scanner unit test across test fixtures. |
| **AC-05** | Outdated dependencies identified using deterministic PyPI metadata and PEP 440 comparisons (`packaging`). | Unit test confirming candidate evaluation. |
| **AC-06** | Ollama generates advisory structured JSON compatibility risk explanations without claiming proof of compatibility. | Ollama client test verifying schema and advisory labeling. |
| **AC-07** | Python source files are inspected up to configurable limit (default 10) with priority scoring and reporting of included/excluded files. | Code inspector test verifying priority ordering and limit. |
| **AC-08** | Proposed AI improvements are validated with `ast.parse` before application; syntax-invalid changes are rejected. | Unit test verifying invalid syntax rejection. |
| **AC-09** | Modifications applied strictly to `working/`; `reference/` remains untouched. | File hash comparison test between reference and clone. |
| **AC-10** | Repository tests run strictly inside dedicated Docker sandbox (non-root, no privilege, no network, no socket mount); if unavailable, marked `NOT_RUNNABLE`. | Sandbox manager test verifying isolation flags and fallback. |
| **AC-11** | Validation results distinguish between passed, failed, and not runnable tests. | Runner test asserting distinct status classifications. |
| **AC-12** | Generated `.patch` file validated using `git apply --check` against clean original repository copy. | Exporter test running `git apply --check`. |
| **AC-13** | Generated `.zip` archive excludes `.git`/caches/secrets and is validated via `testzip()` before download. | Exporter test asserting excluded paths and `testzip() == None`. |
| **AC-14** | Comprehensive Markdown analysis report includes manifest findings, file inclusion/exclusion list, diffs, and validation logs. | Report generator test verifying all sections. |
| **AC-15** | "Analyze & Improve Repository" button orchestrates the 8-stage pipeline with live status tracking. | Orchestrator walkthrough test. |
| **AC-16** | System never executes `git push`, remote branch creation, or PR creation. | Codebase inspection verifying absence of push/remote operations. |
| **AC-17** | System never executes arbitrary shell commands generated by the LLM. | Code audit confirming strict command whitelisting. |
| **AC-18** | All 27 existing Phase 1 tests pass with zero regressions. | Execution of `pytest` confirming 27/27 passing tests. |
| **AC-19** | New Phase 2 unit and integration tests pass cleanly. | Execution of full test suite with `pytest`. |
| **AC-20** | UI provides two top-level tabs: Phase 2 Repository Platform and Phase 1 Single Manifest Review (fully operational). | UI walkthrough verifying both tabs. |
