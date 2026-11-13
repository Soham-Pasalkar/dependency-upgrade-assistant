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

# Python compatibility resolver (M12). Values outside 1..ceiling stop the backend from starting.
RESOLVER_TIMEOUT_SECONDS=180   # per run T (max 600); a job (two runs) is bounded by 2T
RESOLVER_MEMORY_MB=1024        # per resolver container (max 4096)
RESOLVER_CPUS=1.0              # (max 4)
RESOLVER_PIDS_LIMIT=256        # (max 1024)
RESOLVER_TMPFS_MB=512          # scratch space (max 2048)
RESOLVER_MAX_CONCURRENT=2      # concurrent jobs (max 4)
RESOLVER_MAX_REPORT_MB=10      # resolver report size cap (max 50)
RESOLVER_MAX_PACKAGES=500      # selected packages per run (max 2000)

# Browser origins allowed to make state-changing requests (never "*").
# In development, loopback frontends (localhost, 127.0.0.1, [::1]) on DEV_FRONTEND_PORTS are
# allowed automatically, so Vite works on 5173 or its fallback 5174 without extra settings.
DEV_FRONTEND_PORTS=[5173,5174,4173,3000]
# Required when ENVIRONMENT=production:
# CORS_ORIGINS=["https://dua.example.internal"]
```

If the UI reports "Request blocked: origin not allowed (…)", the page's origin is not in the allow-list.
Add the port to `DEV_FRONTEND_PORTS` (development) or the origin to `CORS_ORIGINS`, then restart the backend.

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

## 6a. Compatibility Checks (Python resolver)

Before a Python plan can be approved, validated, applied or exported, pip's own resolver must resolve the
**complete proposed dependency set**, and the server must verify the result. Details, configuration,
security model and troubleshooting: [docs/RESOLUTION_ENGINE.md](RESOLUTION_ENGINE.md).

- **Requires Docker.** Without it the check reports *Resolver unavailable* and approval stays locked.
  The pinned image is pulled on first use, or pull it ahead of time:
  `docker pull python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535`.
- **Network:** the resolver container has no direct network; it reaches only `pypi.org` and
  `files.pythonhosted.org` through a per-job egress proxy. The backend host itself fetches PyPI metadata
  for verification.
- **Only COMPATIBLE unlocks approval.** Conflicts, unsupported input (npm, source-only packages, `-r`,
  `-e`, URLs, index options), failures, time-outs and an unavailable resolver all keep it locked. There is
  no override.
- **Compatible is not safe.** It means the set resolves; it does not mean the packages are vetted or
  safe, and DUA does not perform malware detection. Review the packages the upgrade introduces.

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
   - In **Compatibility**, click **"Check compatibility"** (`POST /api/proposals/{id}/resolve`). Progress
     shows the current dependency set (1 of 2), then the proposed set (2 of 2); you can cancel.
   - When it reports **Compatible**, read the disclosure, review any **new packages introduced by this
     upgrade**, and expand the resolved lists to see exact versions and upload dates.
   - Check the **"I have read the diff above and approve these changes."** checkbox (offered only after a
     compatible result).
   - Click **"Approve changes"** to satisfy the human approval gate.
8. Scroll to **Sandboxed Container Validation**:
   - Notice the `✓ Proposal Human-Approved` badge.
   - Click **"Run Sandboxed Validation"** (`POST /api/validation/execute`).
   - Observe truthful status (`PASSED` with live Docker or `NOT_RUNNABLE` with diagnostic log).
9. Scroll to **Export Deliverables**:
   - Click **"Generate Verified Export Bundle"** (`POST /api/export`).
   - View SHA-256 checksums for each artifact.
   - Download `.patch`, `summary.json`, `execution.log`, `compatibility-report.md`, `checksums.sha256`, or
     the complete `.zip` bundle. The report and `summary.json` list both resolved sets, introduced packages,
     upload dates, fingerprints and the compatibility disclosure.

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
3. Repeat steps 4–7 to analyze dependencies, generate recommendations against npm and review the JSON diff.
4. The **Compatibility** check reports **Not supported**: npm compatibility verification is not yet
   supported (planned for M13). Approval, validation of the upgrade, apply and export stay locked for npm
   plans.

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

# 6. Live compatibility resolver and egress isolation tests (need Docker and internet access)
DUA_REQUIRE_DOCKER=1 python -m pytest -v tests/test_egress_live.py tests/test_resolution_live.py

# 7. Verify AI-DLC governance integrity
python3 .agents/skills/ai-dlc-engineering/scripts/aidlc.py verify
```
