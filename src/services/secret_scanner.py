"""Secret sanitization and verification scanner for pre-export safety checks.

Integrity and security guarantees:
- Scans proposed diffs, manifest changes, execution logs, and summaries for credentials and API keys.
- Detects AWS keys, private key blocks, GitHub tokens, Slack tokens, Google keys, Stripe keys,
  OpenAI/Anthropic keys, JWTs, and hard-coded credential assignments.
- Never prints or exposes raw secret tokens in logs, exceptions, or error responses (always masked).
- Provides deterministic redaction to safely sanitize execution console logs.
- Fails closed with SecretLeakageError before any deliverable is exported if unredacted secrets exist.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Tuple

logger = logging.getLogger(__name__)


class SecretLeakageError(RuntimeError):
    """Raised when unredacted credentials or sensitive tokens are detected in exported deliverables."""
    pass


@dataclass(frozen=True)
class SecretFinding:
    """Masked finding representing a detected credential or sensitive token."""
    pattern_name: str
    line_number: int
    masked_value: str
    source_name: str = "text"

    def summary(self) -> str:
        return f"[{self.source_name}:{self.line_number}] Detected potential {self.pattern_name}: {self.masked_value}"


# High-precision regular expression patterns for credential detection
SECRET_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("AWS Access Key ID", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("AWS Secret Key", re.compile(r"(?i)aws.{0,20}(?:secret|sk).{0,20}['\"][0-9a-zA-Z/+]{40}['\"]")),
    ("Private Key Block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")),
    ("GitHub Personal Access Token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{82})\b")),
    ("Slack API Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,48}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe Live API Key", re.compile(r"\b[sr]k_live_[0-9a-zA-Z]{24,}\b")),
    ("Anthropic API Key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("OpenAI API Key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("Hard-coded Password/Secret", re.compile(
        r"(?i)\b[\w\-]*(?:api[_-]?key|client[_-]?secret|auth[_-]?token|access[_-]?token|secret[_-]?key|secret|password|passwd)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
    )),
    ("Authorization Bearer Token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{24,}\b")),
]

# Patterns representing safe placeholders (never flagged as real secrets)
SAFE_PLACEHOLDER_PATTERN = re.compile(
    r"(?i)(?:example|placeholder|changeme|your[_-]|dummy|sample|<[^>]+>|\$\{|\{\{|test[_-]?token|mock|none|null)"
)


def mask_secret(value: str) -> str:
    """Mask a secret string to prevent credential exposure in logs and error messages."""
    clean = value.strip().strip("'\"")
    if len(clean) <= 6:
        return "***"
    if len(clean) <= 10:
        return f"{clean[:2]}...{clean[-2:]}"
    return f"{clean[:4]}...{clean[-3:]}"


def scan_text(text: str, source_name: str = "content") -> List[SecretFinding]:
    """Scan text for credential patterns. Returns list of masked SecretFinding objects."""
    if not text:
        return []

    findings: List[SecretFinding] = []
    lines = text.splitlines()

    for line_idx, line in enumerate(lines, start=1):
        # Ignore comments explicitly labeled as safe mock data
        if "aidlc:allow-secret" in line:
            continue

        for pattern_name, regex in SECRET_PATTERNS:
            match = regex.search(line)
            if match:
                matched_str = match.group(0)
                # Skip safe placeholders
                if SAFE_PLACEHOLDER_PATTERN.search(matched_str):
                    continue
                findings.append(
                    SecretFinding(
                        pattern_name=pattern_name,
                        line_number=line_idx,
                        masked_value=mask_secret(matched_str),
                        source_name=source_name,
                    )
                )
                # One finding per line is sufficient for safety
                break

    return findings


def redact_secrets(text: str) -> str:
    """Redact detected credentials from text, replacing with safe placeholder markers."""
    if not text:
        return ""

    sanitized = text
    for pattern_name, regex in SECRET_PATTERNS:
        def _replace_match(m: re.Match[str]) -> str:
            val = m.group(0)
            if SAFE_PLACEHOLDER_PATTERN.search(val):
                return val
            tag = pattern_name.upper().replace(" ", "_")
            return f"[REDACTED_{tag}]"

        sanitized = regex.sub(_replace_match, sanitized)

    return sanitized


def verify_zero_secrets(text: str, source_name: str = "artifact") -> None:
    """Verify that text contains zero unredacted secrets or credentials.
    
    Raises SecretLeakageError with masked summaries if any secrets are detected.
    """
    findings = scan_text(text, source_name=source_name)
    if findings:
        summary_lines = [f.summary() for f in findings[:5]]
        overflow = f" (+{len(findings) - 5} more)" if len(findings) > 5 else ""
        detail = "; ".join(summary_lines) + overflow
        logger.error(f"Secret leakage detected in '{source_name}': {detail}")
        raise SecretLeakageError(
            f"Pre-export secret verification failed for '{source_name}': "
            f"{len(findings)} potential credential(s) detected. Details: {detail}"
        )
