# ADR-010: Controlled registry egress through an allow-listing proxy

- Status: proposed (pending G2)
- Date: 2026-11-12
- Requirements: FR-25, NFR-16. Threats: T-27, T-28, T-29, T-36, T-37.

## Context
Live resolution needs current PyPI index data and metadata, so `--network none` would leave pip with nothing to resolve against. Pre-fetching metadata on the host would replace pip's resolver with an imitation of it. Even with no third-party code running (ADR-009), resolver traffic must not reach internal resources (SSRF), be redirected off the allow-list, or be subject to DNS rebinding.

Traffic capture (2026-11-12, local probe): pip contacted only `pypi.org` and `files.pythonhosted.org`. npm lockfile-only resolution (for M13) contacted only `registry.npmjs.org`.

## Options considered
1. **Default bridge networking with host firewall rules.** Not portable to Docker Desktop, and needs host privileges.
2. **An existing proxy image (squid or tinyproxy).** Another image to pin and trust, and it can't easily connect to the exact address it validated.
3. **A minimal in-house `CONNECT` proxy in the already-pinned Python image, on a per-job internal network plus a dedicated egress bridge.**

## Decision
Option 3.
- **Topology:**
  - `dua-egress` is a user-defined bridge, created once and labelled. Only proxy containers join it.
  - Per job, `dua-res-<job>` is an `--internal` network (no gateway) with a /29 from a reserved pool: proxy at `.2`, resolver at `.3`.
  - Each resolver container (original run, then proposed run) joins only `dua-res-<job>`, with `--dns 127.0.0.1`.
  - The proxy joins both networks, binds only `.2:3128`, and serves both runs of its job.
- **Proxy behaviour** (`src/resolution/egress_proxy.py`, stdlib asyncio):
  - accepts `CONNECT host:443` only, for the allow-list `pypi.org` and `files.pythonhosted.org` (version `egress-allowlist/1`);
  - resolves the name itself and requires `ip.is_global` for the address it uses, which excludes loopback, RFC 1918, link-local including 169.254.169.254, CGNAT, multicast, reserved addresses and the Docker gateway;
  - opens the TCP connection to that exact validated address;
  - answers 403 to everything else (plain HTTP, other methods, ports or hosts, non-global results) and writes a JSON denial line (job, host, port, reason) to stdout, which the supervisor records (NFR-16).
- **Redirects:** TLS is end to end, so a redirect to another host becomes a new `CONNECT` and is checked again.
- **Proxy container:** non-root, read-only, capabilities dropped, limits applied, removed with the job.
- **Allow-list changes:** only on traffic evidence. The allow-list version is part of both fingerprints (ADR-011).
- **Supervisor traffic:** the supervisor's own metadata fetches (TB-R4) do not use this proxy. They go through the existing hardened registry client to fixed PyPI hosts over HTTPS, with bounded responses.

## Consequences
- **Gained:** native resolution with live data, no route from the resolver to internal resources, and auditable denials.
- **Cost:** one extra container and one network per job, created once for both runs.
- **New attack surface:** the proxy code. Mitigated by keeping it minimal, plus unit tests and live tests (M12 criterion 8).
- **Not yet verified:** this topology is a design. It counts as verified only when the live Docker egress tests pass in CI.
- **Docker Desktop on macOS:** `--internal` behaviour must be checked there. CI on Linux is the authoritative environment.
