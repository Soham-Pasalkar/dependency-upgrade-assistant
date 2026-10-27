"""Unit tests for diff generation and requirements synthesis."""
from src.core.comparator import compare_requirements
from src.core.diff_generator import generate_unified_diff, synthesize_updated_requirements
from src.core.parser import parse_requirements_content
from src.data.catalog_loader import Catalog


def test_synthesize_with_single_accepted_upgrade(mock_catalog: Catalog):
    """Verify that accepting an upgrade reconstructs the line while preserving surrounding lines."""
    content = (
        "# Setup\n"
        "requests==2.28.0\n"
        "flask==2.2.5\n"
    )
    lines = parse_requirements_content(content)
    evaluations = compare_requirements(lines, mock_catalog)

    # Line numbers: 1 is comment, 2 is requests, 3 is flask
    # Accept only requests (line 2)
    updated = synthesize_updated_requirements(evaluations, accepted_line_numbers={2})

    expected = (
        "# Setup\n"
        "requests==2.32.3\n"
        "flask==2.2.5\n"
    )
    assert updated == expected


def test_synthesize_with_selective_and_complex_lines(mock_catalog: Catalog):
    """Verify synthesis preserves comments, markers, extras, and non-pinned lines."""
    content = (
        "# Important note\n"
        "requests[security]==2.28.0; python_version < '3.11' # pinned\n"
        "urllib3>=1.26.0\n"
        "flask==2.2.5\n"
    )
    lines = parse_requirements_content(content)
    evaluations = compare_requirements(lines, mock_catalog)

    # Accept both requests (line 2) and flask (line 4)
    updated = synthesize_updated_requirements(evaluations, accepted_line_numbers={2, 4})

    expected = (
        "# Important note\n"
        "requests[security]==2.32.3; python_version < '3.11' # pinned\n"
        "urllib3>=1.26.0\n"
        "flask==3.1.0\n"
    )
    assert updated == expected


def test_generate_unified_diff_output(mock_catalog: Catalog):
    """Verify standard unified diff format is produced with '-' and '+' lines."""
    original = "requests==2.28.0\nflask==2.2.5\n"
    proposed = "requests==2.32.3\nflask==3.1.0\n"

    diff = generate_unified_diff(original, proposed)
    assert diff != ""
    assert "--- a/requirements.txt" in diff
    assert "+++ b/requirements.txt" in diff
    assert "-requests==2.28.0" in diff
    assert "+requests==2.32.3" in diff
    assert "-flask==2.2.5" in diff
    assert "+flask==3.1.0" in diff


def test_generate_unified_diff_when_no_changes():
    """Verify empty string is returned when original equals updated content."""
    original = "requests==2.32.3\n"
    assert generate_unified_diff(original, original) == ""


def test_synthesize_when_no_lines_accepted(mock_catalog: Catalog):
    """Verify that when the user accepts 0 lines, output is identical to original."""
    content = "requests==2.28.0\nflask==2.2.5\n"
    lines = parse_requirements_content(content)
    evaluations = compare_requirements(lines, mock_catalog)

    updated = synthesize_updated_requirements(evaluations, accepted_line_numbers=set())
    assert updated == content
    assert generate_unified_diff(content, updated) == ""
