# Dependency Upgrade Assistant 2.0 — Setup & Interactive Demo Guide

This guide provides authoritative, step-by-step instructions for installing, configuring, running, and demonstrating **Dependency Upgrade Assistant 2.0**.

---

## 1. System Architecture & Workflow Summary

Dependency Upgrade Assistant 2.0 is an interactive, security-first developer platform that automates dependency upgrade evaluation while enforcing strict human approval, defense-in-depth container containment, and cryptographic artifact delivery.

```
┌────────────────────────────────────────────────────────────────────────┐
│                         React 19 Frontend UI                           │
│     (Vite on :5173, high-density token system, syntax diffs)           │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Proxied /api requests
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                         FastAPI Backend Server                         │
│                  (Uvicorn on :8000, Python 3.10–3.14)                  │
├────────────────────────────────────────────────────────────────────────┤
│  • /api/manifest/analyze      - Ingests requirements.txt & package.json│
│  • /api/recommendations       - Deterministic & Ollama advisory logic  │
│  • /api/plan/generate & apply - Plan synthesis & human approval gate   │
│  • /api/validation/execute    - Docker container runner (--network none)│
│  • /api/export                - Patch, summary, logs & SHA-256 ZIP     │
└────────────────────────────────────────────────────────────────────────┘
```

### Complete 10-Step Lifecycle:
1. **Manifest Ingestion:** Upload file or paste raw content (`requirements.txt` or `package.json`).
2. **Dependency Analysis:** Deterministic parsing against PyPI or npm registries with constraint checking.
3. **Upgrade Recommendations:** Advisory AI suggestions via local Ollama or automatic deterministic fallback with ground-truth registry validation.
4. **Upgrade Plan Synthesis:** Generates a structured proposal with version deltas and target specifiers.
5. **Unified Diff Inspection:** Syntax-highlighted diff rendering additions, deletions, context, and hunk headers.
6. **Explicit Human Approval Gate:** Cryptographic and API-enforced gate (HTTP 403 on unapproved mutations).
7. **Sandboxed Validation:** Containerized execution with `--network none`, non-root execution (`1000:1000`), and resource caps.
8. **Truthful Reporting:** Server-authoritative status reporting (`PASSED`, `FAILED`, `TIMED_OUT`, `NOT_RUNNABLE`).
9. **Export Bundle Generation:** Pre-export secret scanning and packaging into verified ZIP archive.
10. **Deliverable Downloads:** Direct streaming of `.patch`, `summary.json`, `execution.log`, `checksums.sha256`, and bundle `.zip`.

---

## 2. Prerequisites & Installation

### Host System Requirements
- **Operating System:** Linux, macOS, or Windows (WSL2 recommended)
- **Python:** Python 3.10, 3.11, 3.12, 3.13, or 3.14
- **Node.js:** Node.js 18+ (Node 20 or 22 recommended) and npm
- **Docker (Optional):** Docker Engine 24+ for live containerized sandbox testing
- **Ollama (Optional):** Local Ollama daemon for local AI advisory inference

### Quick Start Installation

```bash
# 1. Clone repository
git clone https://github.com/Soham-Pasalkar/dependency-upgrade-assistant.git
cd dependency-upgrade-assistant

# 2. Set up Python virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Set up Frontend dependencies
npm --prefix frontend install
```

---

## 3. Configuration & Environment Variables

Default settings run out-of-the-box on `http://127.0.0.1:8000` and `http://localhost:5173`. Create a `.env` file in the project root to override settings if desired:

```dotenv
# Core Server Settings
APP_NAME="Dependency Upgrade Assistant"
HOST="127.0.0.1"
PORT=8000
DEBUG=false

# Ollama Local AI Configuration
OLLAMA_BASE_URL="http://127.0.0.1:11434"
OLLAMA_DEFAULT_MODEL="llama3:latest"
OLLAMA_TIMEOUT_SECONDS=30.0
ALLOW_REMOTE_OLLAMA=false

# Docker Sandbox Resource Caps
DOCKER_TIMEOUT_SECONDS=60
DOCKER_MAX_MEMORY_MB=512
DOCKER_MAX_CPUS=1.0

# CORS Allowed Origins
CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173"]
```

---

## 4. Starting the Application

Open two terminal windows:

### Terminal 1: Backend Server (FastAPI)
```bash
source .venv/bin/activate
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --reload
```
*Backend health check:* `http://127.0.0.1:8000/api/health`  
*Interactive Swagger docs:* `http://127.0.0.1:8000/docs`

### Terminal 2: Frontend Development Server (React 19 + Vite)
```bash
npm --prefix frontend run dev
```
*Frontend UI is accessible at:* `http://localhost:5173`

---

## 5. Ollama Setup & Automatic Fallback

The assistant integrates with locally hosted Ollama models for advisory breaking-change analysis without transmitting code or dependency lists to third-party APIs.

### If Ollama is Installed:
```bash
# Start local Ollama daemon
ollama serve

# Pull recommended model
ollama pull llama3:latest
```
The UI automatically detects active models and populates the model selector in the header.

### If Ollama is NOT Installed or Daemon is Offline:
No action is required. The platform **automatically and gracefully activates the deterministic recommendation engine** (`is_offline_fallback = True`). All upgrades are calculated using semantic versioning rules and validated against authoritative PyPI and npm registries. **Zero functionality is blocked by an absent Ollama instance.**

---

## 6. Docker Sandbox Isolation & Requirements

The sandbox validation subsystem executes test discovery, bytecode compilation (`py_compile`), and test suites (`pytest`, `npm test`) inside isolated ephemeral Docker containers.

### Containment Policy:
- **Network Isolation:** `--network none` (no external or outbound network access).
- **Execution User:** `1000:1000` (non-root execution).
- **Capabilities:** `--cap-drop=ALL` (drops all Linux kernel capabilities).
- **Read-Only Reference Mounts:** Tested repository references are mounted strictly read-only.
- **Resource Limits:** Default 512MB RAM cap, 1.0 CPU quota, and 64 maximum process IDs (`pids_limit`).

### If Docker Daemon is Offline:
The backend fail-closed policy truthfully returns status `NOT_RUNNABLE` with diagnostic reasons. It **never falls back to executing untrusted code directly on the host machine**.

---

## 7. Step-by-Step Interactive Demo Walkthrough

### Scenario A: Python Ecosystem (`requirements.txt`)
1. Open `http://localhost:5173`.
2. Ensure **requirements.txt** is selected in the format dropdown.
3. Click **"Load Sample"** to load production dependencies:
   ```txt
   requests==2.28.0
   fastapi==0.115.0
   pydantic==2.8.2
   pytest>=7.0.0
   uvicorn~=0.30.0
   ```
4. Click **"Analyze Dependencies"** (`POST /api/manifest/analyze`). Inspect parsed dependencies, status badges, and constraint categories.
5. In the recommendations panel, click **"Generate Upgrade Recommendations"** (`POST /api/recommendations/generate`). Observe recommended version bumps (e.g., `requests` bumping to `2.32.3`).
6. Click **"Generate Upgrade Plan & Unified Diff"** (`POST /api/plan/generate`).
7. In the **Upgrade Plan & Diff Review** panel:
   - Inspect planned items and version changes.
   - Expand the **Unified Patch Diff** viewer to see exact additions (`+`) and deletions (`-`).
   - Check the **"I have inspected the planned changes..."** checkbox.
   - Click **"Confirm Human Approval"** to satisfy the human approval gate.
8. Scroll to **Sandboxed Container Validation**:
   - Notice the `✓ Proposal Human-Approved` badge.
   - Click **"Run Sandboxed Validation"** (`POST /api/validation/execute`).
   - Observe truthful status (`PASSED` with live Docker or `NOT_RUNNABLE` with diagnostic log).
9. Scroll to **Export Deliverables**:
   - Click **"Generate Verified Export Bundle"** (`POST /api/export`).
   - View SHA-256 checksums for each artifact.
   - Download `.patch`, `summary.json`, `execution.log`, `checksums.sha256`, or the complete `.zip` bundle.

### Scenario B: Node.js Ecosystem (`package.json`)
1. Switch format dropdown to **package.json**.
2. Click **"Load Sample"** to load standard Node.js manifest:
   ```json
   {
     "name": "sample-service",
     "version": "1.0.0",
     "dependencies": {
       "express": "^4.18.0",
       "lodash": "4.17.21",
       "react": "18.2.0"
     }
   }
   ```
3. Repeat steps 4–9 to analyze dependencies, generate recommendations against npm, review the JSON diff, confirm approval, execute validation, and export deliverables.

---

## 8. Verification & Test Suite Execution

```bash
# 1. Run complete backend test suite (570 tests)
./.venv/bin/pytest -v

# 2. Run end-to-end API workflow tests
./.venv/bin/pytest tests/test_e2e_workflow_api.py -v

# 3. Run secret leakage scanner
python3 .agents/skills/ai-dlc-engineering/scripts/scan_secrets.py .

# 4. Run frontend unit tests (28 tests)
npm --prefix frontend test -- --run

# 5. Run frontend lint and production build
npm --prefix frontend run lint
npm --prefix frontend run build

# 6. Verify AI-DLC governance integrity
python3 .agents/skills/ai-dlc-engineering/scripts/aidlc.py verify
```
