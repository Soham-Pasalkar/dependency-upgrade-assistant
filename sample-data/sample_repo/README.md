# Sample Test Repository Fixture

This directory provides a deterministic, controlled test repository for end-to-end verification of the Dependency Upgrade Assistant (AIDLC Phase 2).

## Expected Findings and Validation Outcomes

### 1. Dependency Manifests
- **`requirements.txt`**:
  - `requests==2.28.0` $\rightarrow$ **Outdated** (Candidate upgrade available: `2.32.3`).
  - `pytest==8.2.0` $\rightarrow$ **Up to Date** (Matches candidate version).
  - `urllib3>=1.26.0` $\rightarrow$ **Preserved Non-Pinned** (Policy preserves range specifiers).
- **`pyproject.toml`**:
  - Contains standard PEP 621 dependencies matching `requirements.txt`.

### 2. Source Code Inspection
- **`src/sample_app/main.py`**:
  - Priority scored high because it imports candidate package `requests` and is in `src/`.
  - Intentional Finding: Bare `except:` clause on line 12 (Issue Category: `CODE_SMELL`, Severity: `MEDIUM`).
- **`src/sample_app/utils.py`**:
  - Clean helper utility with zero code smells or security hotspots.

### 3. Test Suite
- **`tests/test_sample.py`**:
  - Deterministic test suite with 3 passing tests (`test_format_greeting`, `test_add_numbers`, `test_compute_square`).
  - Expected sandbox test execution status: `PASSED` when Docker sandbox is running; `NOT_RUNNABLE` when Docker sandbox is unavailable.

