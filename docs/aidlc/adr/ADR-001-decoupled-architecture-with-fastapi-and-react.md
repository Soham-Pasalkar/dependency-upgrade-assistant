# ADR-001: Decoupled Architecture with FastAPI and React

- Status: accepted
- Date: 2026-11-09

## Context
The existing Dependency Upgrade Assistant prototype is implemented as a monolithic Streamlit application (`src/app.py`, 727 lines). While Streamlit enabled rapid prototyping, it binds presentation directly to the Python runtime, re-executes the entire script on user interaction, and precludes rich developer-focused UI capabilities such as side-by-side Monaco diff inspection, granular hunk-level approvals, and real-time streaming of pipeline stage events. 

To transform the prototype into a professional developer platform (Dependency Upgrade Assistant 2.0), we need a modern, decoupled architecture while strictly preserving the existing Python domain logic and 146 passing tests.

## Options considered
1. **Retain Streamlit with Custom Components:** Keep Streamlit as the host and write bidirectional custom React components for Monaco diffing.
   - *Drawback:* Script rerun model creates state synchronization complexity, latency spikes during large diff rendering, and limits background event streaming.
2. **Next.js Full-Stack Framework with Python Microservice:** Rewrite frontend and API routes in Next.js/Node.js, calling Python via internal RPC.
   - *Drawback:* Introduces a two-runtime deployment footprint (Node.js + Python) for local desktop users, complicating installation and distribution.
3. **Decoupled Architecture with FastAPI Backend and React Single-Page Application (SPA):**
   - FastAPI serves high-performance REST and Server-Sent Events (SSE) endpoints on `127.0.0.1:8000`, cleanly wrapping the existing `PipelineOrchestrator`.
   - React + TypeScript + Vite + Tailwind CSS + shadcn/ui provides a high-density, professional UI with Monaco Editor for diff review.
   - In production or local bundle, FastAPI serves the compiled static SPA assets directly or runs alongside Vite during local development.

## Decision
We choose **Option 3: Decoupled Architecture with FastAPI Backend and React SPA**.
FastAPI provides native asynchronous endpoints, automatic OpenAPI schema generation, Pydantic validation, and SSE streaming, while directly importing and orchestrating the existing `src/` modules. The React frontend delivers the responsive, high-fidelity experience required for complex dependency graphs and diff inspection.

## Consequences
- **Positive:** Clean architectural separation of concerns; enables real-time progress streaming via SSE; supports rich Monaco Editor integration with hunk-level review; preserves all 146 existing unit/integration tests without modification.
- **Trade-offs:** Requires maintaining REST API contracts and Pydantic request/response models; local developer security requires explicit Host-header and Origin/CSRF middleware.
- **Future Revisit:** When evaluating hosted deployment, the FastAPI backend can be containerized independently of the static frontend CDN.
