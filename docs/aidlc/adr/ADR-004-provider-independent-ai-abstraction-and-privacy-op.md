# ADR-004: Provider-Independent AI Abstraction and Privacy Opt-In

- Status: proposed (submitted for Gate G2 review)
- Date: 2026-11-09

## Context
The prototype application hard-coded all AI interactions to a local Ollama REST client (`src/services/ollama_client.py`). In Version 2.0, we want to support both local inference (Ollama) and hosted cloud providers (Anthropic Claude, OpenAI, Google Gemini) via a vendor-independent abstraction layer.

However, transmitting proprietary repository code, AST snippets, or dependency lists to third-party hosted LLMs introduces significant privacy, intellectual property, and regulatory risks. Many developers work under strict compliance policies forbidding external code transmission.

## Options considered
1. **Direct SDK Integration per Provider without Centralized Privacy Controls:**
   - Integrate provider SDKs ad-hoc in controllers.
   - *Drawback:* Vendor lock-in, fragmented prompt logic, and severe risk of accidental telemetry or code leaks.
2. **Mandatory Cloud LLM Usage:**
   - Rely solely on a cloud provider for upgrade reasoning.
   - *Drawback:* Violates privacy-first philosophy and breaks offline local operation.
3. **Provider-Independent Protocol with Strict Repository-Scoped Opt-In:**
   - Define a pure Python Protocol `LLMProvider` with implementations `OllamaProvider` (default local) and `HostedLLMProvider` (pluggable cloud models via BYOK).
   - Enforce an explicit, repository-level opt-in flag (`cloud_llm_consent: bool`, defaulting to `False`).
   - If consent is false, cloud LLM calls are blocked at the service boundary.
   - If AI is unreachable or consent is denied, the application gracefully degrades to deterministic analysis mode (PyPI metadata + AST pattern matching), maintaining full functionality without failure.

## Decision
We choose **Option 3: Provider-Independent Protocol with Strict Repository-Scoped Opt-In**.
Local Ollama remains the default inference provider. Cloud providers require explicit user configuration (BYOK) and per-repository consent. The platform enforces a **Zero-Code-Exfiltration Local Privacy Policy**, ensuring zero unsolicited transmission of source code, AST findings, or manifests to external networks.

## Consequences
- **Positive:** Total privacy control for developers; deterministic analysis functions fully even in offline or local-only environments; clean provider swapping without touching core business logic.
- **Trade-offs:** Advisory risk explanations and migration notes are qualitative summaries and may vary between local and cloud model capabilities.
- **Future Revisit:** Evaluate local small-footprint embedded models (e.g. llama.cpp bindings) if local Ollama daemon installation friction proves high.
