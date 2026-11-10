"""Unit tests for patch generation, application, and reversal."""
from __future__ import annotations

from src.services.patch_service import (
    apply_unified_diff,
    generate_unified_diff,
    reverse_unified_diff,
)


def test_generate_unified_diff_single_change():
    """Verify standard unified diff format for a single modification."""
    original = "requests==2.28.0\n"
    proposed = "requests==2.32.3\n"
    diff = generate_unified_diff(original, proposed, file_path="requirements.txt")

    assert diff != ""
    assert "--- a/requirements.txt" in diff
    assert "+++ b/requirements.txt" in diff
    assert "@@ -1 +1 @@" in diff
    assert "-requests==2.28.0" in diff
    assert "+requests==2.32.3" in diff


def test_generate_unified_diff_multiple_changes():
    """Verify unified diff handles multiple modifications with context lines."""
    original = (
        "package-a==1.0.0\n"
        "package-b==2.0.0\n"
        "package-c==3.0.0\n"
        "package-d==4.0.0\n"
    )
    proposed = (
        "package-a==1.1.0\n"
        "package-b==2.0.0\n"
        "package-c==3.1.0\n"
        "package-d==4.0.0\n"
    )
    diff = generate_unified_diff(original, proposed, file_path="requirements.txt")

    assert "-package-a==1.0.0" in diff
    assert "+package-a==1.1.0" in diff
    assert "-package-c==3.0.0" in diff
    assert "+package-c==3.1.0" in diff
    assert " package-b==2.0.0" in diff


def test_generate_unified_diff_noop_returns_empty_string():
    """Verify empty string is returned when original and proposed contents are identical."""
    content = "requests==2.32.3\nflask==3.1.0\n"
    assert generate_unified_diff(content, content, file_path="requirements.txt") == ""


def test_generate_unified_diff_deterministic():
    """Verify identical inputs produce identical diff strings."""
    original = "a==1\nb==2\nc==3\n"
    proposed = "a==2\nb==2\nc==4\n"
    diff1 = generate_unified_diff(original, proposed, file_path="requirements.txt")
    diff2 = generate_unified_diff(original, proposed, file_path="requirements.txt")
    assert diff1 == diff2


def test_apply_unified_diff_single_and_multi_hunk():
    """Verify applying unified diff to original content reproduces proposed content."""
    original = (
        "# Config\n"
        "dep1==1.0.0\n"
        "dep2==2.0.0\n"
        "# Middle\n"
        "dep3==3.0.0\n"
        "dep4==4.0.0\n"
    )
    proposed = (
        "# Config\n"
        "dep1==1.5.0\n"
        "dep2==2.0.0\n"
        "# Middle\n"
        "dep3==3.5.0\n"
        "dep4==4.0.0\n"
    )
    diff = generate_unified_diff(original, proposed, file_path="requirements.txt")
    applied = apply_unified_diff(original, diff)
    assert applied == proposed


def test_apply_unified_diff_with_empty_diff_returns_original():
    """Verify applying an empty diff returns original content untouched."""
    content = "package==1.0.0\n"
    assert apply_unified_diff(content, "") == content


def test_reverse_unified_diff_restores_original_content():
    """Verify reversing a patch and applying it to proposed content restores original content byte-for-byte."""
    original = (
        "{\n"
        '  "dependencies": {\n'
        '    "react": "^18.2.0",\n'
        '    "lodash": "^4.17.20"\n'
        "  }\n"
        "}\n"
    )
    proposed = (
        "{\n"
        '  "dependencies": {\n'
        '    "react": "^18.3.1",\n'
        '    "lodash": "^4.17.21"\n'
        "  }\n"
        "}\n"
    )
    diff = generate_unified_diff(original, proposed, file_path="package.json")
    assert diff != ""

    # Reverse the diff
    reversed_diff = reverse_unified_diff(diff)
    assert reversed_diff != ""

    # Apply reversed diff to proposed content
    restored = apply_unified_diff(proposed, reversed_diff)
    assert restored == original


def test_reverse_unified_diff_empty_returns_empty():
    """Verify reverse of empty diff is empty."""
    assert reverse_unified_diff("") == ""
