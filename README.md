# Dependency Upgrade Assistant 📦

[![CI](https://github.com/soham/dependency-upgrade-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/soham/dependency-upgrade-assistant/actions/workflows/ci.yml)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A lightweight, interactive developer prototype that helps open-source contributors and maintainers identify outdated Python dependencies, inspect proposed upgrades against a deterministic local catalog, review syntax-highlighted unified diffs, and selectively accept changes before incorporating them into their projects.

Developed using an **AI-Driven Development Life Cycle (AI-DLC)** methodology with strict human-in-the-loop approval gating, comprehensive requirements traceability, and automated test coverage.

---

## Features

- **Interactive Ingestion:** Upload an existing `requirements.txt` file or paste content directly into the in-browser text area.
- **PEP 440 Semantic Comparison:** Accurately compares version numbers using `packaging.version.Version` rather than lexicographical string comparison (e.g., verifying `2.10.0` is recognized as newer than `2.9.0`).
- **Lossless Line Preservation:** Retains comments, section headers, blank lines, pip flags (`-r`, `-i`), URLs, editable installs (`-e .`), environment markers, and unparseable lines byte-for-byte in their original relative positions.
- **Strict Upgrade Scope:** Evaluates upgrade proposals exclusively for simple, exact-pinned dependencies (`==`). Preserves non-pinned constraints (`>=`, `<=`, `~=`, compound bounds) without modifying unrelated parts.
- **Selective Human Oversight:** Review each candidate upgrade with interactive toggles. Select or deselect individual updates to dynamically refresh the proposed output.
- **Unified Diff Inspection:** Generates a standard `git diff`-style unified diff showing exact deletions (`-`) and additions (`+`).
- **One-Click Export:** Download the validated `requirements-updated.txt` file immediately.
- **Pre-Built Demo Samples:** Quickly test the workflow using bundled sample manifests:
  - `sample-data/requirements-outdated.txt`
  - `sample-data/requirements-clean.txt`
  - `sample-data/requirements-complex.txt`

---

## Architecture Overview

The system adheres to a **Headless Core Architecture**, completely decoupling business logic from the presentation layer:

```
┌────────────────────────────────────────────────────────┐
│                   Streamlit Web UI                     │
│                     (src/app.py)                       │
│  - Dual Ingestion (Upload / Paste)                     │
│  - Interactive Checkbox Review Table                   │
│  - Unified Diff Preview (difflib)                      │
│  - Export / Download Button                            │
└───────────────────────────┬────────────────────────────┘
                            │ Calls pure Python APIs
                            ▼
┌────────────────────────────────────────────────────────┐
│                  Headless Core Engine                  │
│                                                        │
│  ┌───────────────────────┐   ┌──────────────────────┐  │
│  │   Parser Engine       │   │   Catalog Loader     │  │
│  │   (src/core/parser.py)│   │ (src/data/catalog... )│ │
│  └───────────┬───────────┘   └──────────┬───────────┘  │
│              ▼                          ▼              │
│  ┌──────────────────────────────────────────────────┐  │
│  │         Semantic Comparator & Evaluator          │  │
│  │             (src/core/comparator.py)             │  │
│  │  - PEP 440 semantic checks (packaging.version)   │  │
│  └───────────────────────────┬──────────────────────┘  │
│                              ▼                         │
│  ┌──────────────────────────────────────────────────┐  │
│  │              Diff & Output Generator             │  │
│  │           (src/core/diff_generator.py)           │  │
│  │  - Lossless line reconstruction                  │  │
│  │  - Unified diff formatting (difflib)             │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

---

## Technology Stack

- **Language:** Python 3.10+ (Tested through Python 3.14.3)
- **Web Frontend:** [Streamlit](https://streamlit.io/)
- **Version Semantics:** [packaging](https://packaging.pypa.io/) (PyPA PEP 440 compliant)
- **Testing:** [pytest](https://docs.pytest.org/)
- **Continuous Integration:** GitHub Actions (`.github/workflows/ci.yml`)
- **Data Catalog:** Flat, version-controlled JSON dataset (`src/data/package_catalog.json`)

---

## Getting Started

### Prerequisites

- Python 3.10 or newer installed on your machine (`python3 --version`).
- Git for version control.

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/soham/dependency-upgrade-assistant.git
   cd dependency-upgrade-assistant
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

---

## Usage Instructions

### Running the Web Application

Launch the Streamlit interface locally:

```bash
streamlit run src/app.py
```

The application will open automatically in your browser at `http://localhost:8501`.

### Step-by-Step Workflow

1. **Load Requirements:**
   - Either paste dependency text into the editor, upload an existing `requirements.txt`, or click one of the sample buttons in the left sidebar (e.g., *Load Outdated Sample*).
2. **Review Candidate Upgrades:**
   - Inspect the review table. Each outdated package displays its current version, proposed candidate version, and status badge.
   - Use the individual checkboxes (or *Select All* / *Deselect All*) to pick which updates you wish to apply.
3. **Inspect the Unified Diff:**
   - View the side-by-side diff comparing the original file against the proposed changes.
4. **Download the Result:**
   - Click **Download Proposed requirements.txt** to save your updated file.

---

## Automated Testing

All core components (parser, comparator, diff generator, catalog loader, and integration flows) are backed by an automated `pytest` test suite.

Run the full test suite with verbose output:

```bash
pytest -v
```

Run tests with short summary:

```bash
pytest -q
```

---

## Prototype Limitations & Provenance Notice

1. **Predefined Local Catalog:** Candidate upgrade versions are drawn from a curated local dataset of 20 popular open-source packages (`src/data/package_catalog.json`). They are **NOT verified against live PyPI**, and the tool does not perform live network requests.
2. **Exact-Pin Focus:** Upgrade proposals are generated strictly for exact-pinned requirements (`package==version`). Non-pinned constraints (`>=`, `<=`, `~=`), compound constraints, URLs, editable installs, and unparseable lines are preserved unchanged.
3. **No Automatic Code Execution or Git Commits:** The tool is read-only and will never execute `pip install`, modify your working tree, or create remote git commits without explicit human intervention.

---

## AI-DLC Governance & Audit Documentation

This project was built following the AI-Driven Development Life Cycle (AI-DLC). Complete project governance records are located in:

- [aidlc-docs/requirements/dependency-upgrade-assistant-requirements.md](file:///Users/soham/Github/Dependency%20Upgrade%20Assistant/aidlc-docs/requirements/dependency-upgrade-assistant-requirements.md) — Software Requirements Specification (SRS-DUA-001).
- [plan/dependency-upgrade-assistant-plan.md](file:///Users/soham/Github/Dependency%20Upgrade%20Assistant/plan/dependency-upgrade-assistant-plan.md) — Architectural and execution plan.
- [aidlc-docs/approvals/approval-log.md](file:///Users/soham/Github/Dependency%20Upgrade%20Assistant/aidlc-docs/approvals/approval-log.md) — Formal human review approval log.
- [aidlc-docs/prompts/](file:///Users/soham/Github/Dependency%20Upgrade%20Assistant/aidlc-docs/prompts/) — Prompt engineering audit records.

---

## License

This project is licensed under the MIT License.
