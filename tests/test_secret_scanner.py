"""Unit tests for the pre-export secret sanitization and verification scanner."""
import pytest

from src.services.secret_scanner import (
    SecretFinding,
    SecretLeakageError,
    mask_secret,
    redact_secrets,
    scan_text,
    verify_zero_secrets,
)

# Dynamically construct synthetic tokens to avoid triggering GitHub remote push protection
DUMMY_AWS_KEY = "AKIA" + "1234567890ABCDEF"  # aidlc:allow-secret
DUMMY_GH_TOKEN = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"  # aidlc:allow-secret
DUMMY_SLACK_TOKEN = "xoxb" + "-123456789012-1234567890123-abcdefghijklmnopqrstuvwx"  # aidlc:allow-secret


def test_mask_secret():
    """Verify that masking protects sensitive credentials from leaking into logs."""
    assert mask_secret("short") == "***"
    assert mask_secret("AKIA" + "IOSFODNN7EXAMPLE") == "AKIA...PLE"  # aidlc:allow-secret
    assert mask_secret("'ghp_" + "1234567890abcdef1234567890abcdef'") == "ghp_...def"  # aidlc:allow-secret


def test_scan_text_clean_content():
    """Verify that clean source code and logs produce zero findings."""
    clean_sample = (
        "import os\n"
        "import requests\n"
        "\n"
        "def fetch_data():\n"
        "    url = 'https://api.example.com/v1/data'\n"
        "    timeout = 30\n"
        "    return requests.get(url, timeout=timeout)\n"
    )
    findings = scan_text(clean_sample)
    assert findings == []
    # verify_zero_secrets should pass without exception
    verify_zero_secrets(clean_sample, source_name="clean_sample.py")


def test_scan_text_safe_placeholders_ignored():
    """Verify that documentation placeholders and template tokens are not falsely flagged."""
    placeholder_sample = (
        "API_KEY = 'your_api_key_here'\n"
        "GITHUB_TOKEN = '<INSERT_GITHUB_TOKEN>'\n"
        "AUTH = 'dummy_token_placeholder'\n"
        "AWS_KEY = 'AKIA_EXAMPLE_KEY_HOLDER'\n"
    )
    findings = scan_text(placeholder_sample)
    assert findings == []
    verify_zero_secrets(placeholder_sample, source_name="docs.py")


def test_detect_aws_access_key():
    """Verify detection of AWS Access Key ID."""
    sample = f"AWS_ACCESS_KEY_ID = '{DUMMY_AWS_KEY}'\n"  # aidlc:allow-secret
    findings = scan_text(sample)
    assert len(findings) == 1
    assert findings[0].pattern_name == "AWS Access Key ID"
    assert "AKIA" in findings[0].masked_value

    with pytest.raises(SecretLeakageError) as exc_info:
        verify_zero_secrets(sample, source_name="config.py")
    assert "Pre-export secret verification failed" in str(exc_info.value)
    assert DUMMY_AWS_KEY not in str(exc_info.value)  # Raw secret must never be exposed # aidlc:allow-secret


def test_detect_private_key_block():
    """Verify detection of private key headers."""
    sample = (
        "-----BEGIN RSA PRIVATE KEY-----\n"  # aidlc:allow-secret
        "MIIEowIBAAKCAQEA0YgX...[truncated]\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    findings = scan_text(sample)
    assert len(findings) >= 1
    assert any("Private Key Block" in f.pattern_name for f in findings)

    with pytest.raises(SecretLeakageError):
        verify_zero_secrets(sample, source_name="id_rsa")


def test_detect_github_token():
    """Verify detection of GitHub personal access tokens."""
    sample = f"gh_token = '{DUMMY_GH_TOKEN}'\n"  # aidlc:allow-secret
    findings = scan_text(sample)
    assert len(findings) == 1
    assert findings[0].pattern_name == "GitHub Personal Access Token"

    with pytest.raises(SecretLeakageError):
        verify_zero_secrets(sample)


def test_detect_hardcoded_passwords_and_secrets():
    """Verify detection of hardcoded credential assignments."""
    sample = "db_password = 'SuperSecretProductionPassword123!'\n"
    findings = scan_text(sample)
    assert len(findings) == 1
    assert findings[0].pattern_name == "Hard-coded Password/Secret"

    with pytest.raises(SecretLeakageError):
        verify_zero_secrets(sample)


def test_detect_slack_token():
    """Verify detection of Slack API tokens."""
    sample = f"SLACK_BOT_TOKEN = '{DUMMY_SLACK_TOKEN}'\n"  # aidlc:allow-secret
    findings = scan_text(sample)
    assert len(findings) == 1
    assert findings[0].pattern_name == "Slack API Token"


def test_redact_secrets_in_logs():
    """Verify that redact_secrets cleanly sanitizes credentials in log outputs."""
    raw_log = (
        f"2026-11-11 10:00:00 [INFO] Connecting with token {DUMMY_GH_TOKEN}\n"  # aidlc:allow-secret
        f"2026-11-11 10:00:01 [DEBUG] Using key {DUMMY_AWS_KEY} for bucket access\n"  # aidlc:allow-secret
        "2026-11-11 10:00:02 [INFO] Connection established successfully.\n"
    )
    sanitized = redact_secrets(raw_log)

    assert DUMMY_GH_TOKEN not in sanitized  # aidlc:allow-secret
    assert DUMMY_AWS_KEY not in sanitized  # aidlc:allow-secret
    assert "[REDACTED_GITHUB_PERSONAL_ACCESS_TOKEN]" in sanitized
    assert "[REDACTED_AWS_ACCESS_KEY_ID]" in sanitized
    assert "Connection established successfully." in sanitized

    # Sanitized log must now pass verify_zero_secrets
    verify_zero_secrets(sanitized, source_name="sanitized.log")


def test_allow_secret_comment_exemption():
    """Verify that aidlc:allow-secret marker bypasses check when intentional."""
    line_with_allow = f"TEST_KEY = '{DUMMY_AWS_KEY}'  # aidlc:allow-secret for fixture\n"
    findings = scan_text(line_with_allow)
    assert findings == []
