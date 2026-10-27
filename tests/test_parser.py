"""Unit tests for requirements parsing and lossless preservation."""
from src.core.models import LineType
from src.core.parser import parse_line, parse_requirements_content


def test_parse_simple_exact_pin():
    """Verify parsing of simple exact pin."""
    line = parse_line("requests==2.28.0\n", 1)
    assert line.line_type == LineType.EXACT_PIN
    assert line.package_name == "requests"
    assert line.version == "2.28.0"
    assert line.operator == "=="
    assert line.extras is None
    assert line.environment_marker is None
    assert line.inline_comment is None
    assert line.newline == "\n"


def test_parse_exact_pin_with_extras_marker_and_comment():
    """Verify parsing of exact pin with extras, marker, and inline comment."""
    raw = "requests[security,socks]==2.28.0; python_version < '3.11' # pinned for legacy\n"
    line = parse_line(raw, 2)
    assert line.line_type == LineType.EXACT_PIN
    assert line.package_name == "requests"
    assert line.extras == "[security,socks]"
    assert line.version == "2.28.0"
    assert line.environment_marker == "python_version < '3.11'"
    assert line.inline_comment == "# pinned for legacy"

    # Verify reconstruction with new candidate version
    reconstructed = line.reconstruct("2.32.3")
    expected = "requests[security,socks]==2.32.3; python_version < '3.11' # pinned for legacy\n"
    assert reconstructed == expected


def test_parse_comment_and_blank_lines():
    """Verify comment and empty lines are preserved with correct LineTypes."""
    c_line = parse_line("# Core dependencies\n", 1)
    assert c_line.line_type == LineType.COMMENT
    assert c_line.raw_text == "# Core dependencies\n"
    assert c_line.reconstruct() == "# Core dependencies\n"

    e_line = parse_line("   \n", 2)
    assert e_line.line_type == LineType.EMPTY
    assert e_line.raw_text == "   \n"


def test_parse_pip_flags_and_urls():
    """Verify pip options, URLs, and editable installs are classified as PIP_FLAG and preserved."""
    flag_line = parse_line("-r base-requirements.txt\n", 1)
    assert flag_line.line_type == LineType.PIP_FLAG
    assert flag_line.reconstruct("1.0.0") == "-r base-requirements.txt\n"

    index_line = parse_line("--extra-index-url https://pypi.org/simple\n", 2)
    assert index_line.line_type == LineType.PIP_FLAG

    git_line = parse_line("git+https://github.com/psf/requests.git@v2.28.0\n", 3)
    assert git_line.line_type == LineType.PIP_FLAG

    editable_line = parse_line("-e .\n", 4)
    assert editable_line.line_type == LineType.PIP_FLAG


def test_parse_non_pinned_and_compound_specifiers():
    """Verify non-pinned specifiers are classified as NON_PINNED_SPECIFIER."""
    gte_line = parse_line("requests>=2.28.0\n", 1)
    assert gte_line.line_type == LineType.NON_PINNED_SPECIFIER
    assert gte_line.package_name == "requests"
    assert gte_line.operator == ">=2.28.0"

    comp_line = parse_line("urllib3~=1.26.5\n", 2)
    assert comp_line.line_type == LineType.NON_PINNED_SPECIFIER
    assert comp_line.package_name == "urllib3"

    multi_line = parse_line("flask>=2.0.0,<3.0.0\n", 3)
    assert multi_line.line_type == LineType.NON_PINNED_SPECIFIER
    assert multi_line.package_name == "flask"

    # Wildcard is a prefix match, not exact single version pin
    wildcard_line = parse_line("numpy==1.24.*\n", 4)
    assert wildcard_line.line_type == LineType.NON_PINNED_SPECIFIER


def test_parse_bare_package():
    """Verify bare package names without constraints."""
    line = parse_line("pydantic\n", 1)
    assert line.line_type == LineType.BARE_PACKAGE
    assert line.package_name == "pydantic"


def test_parse_malformed_lines():
    """Verify unparseable lines do not crash and are marked UNPARSEABLE."""
    line = parse_line("requests==???invalid-ver\n", 1)
    assert line.line_type == LineType.UNPARSEABLE
    assert line.reconstruct("2.32.3") == "requests==???invalid-ver\n"

    gibberish = parse_line(">>> not a python requirement <<<\n", 2)
    assert gibberish.line_type == LineType.UNPARSEABLE


def test_parse_requirements_content_full():
    """Verify parsing a full multi-line document with mixed lines."""
    content = (
        "# Dependencies\n"
        "\n"
        "requests==2.28.0 # http\n"
        "flask>=2.0.0\n"
        "-r other.txt\n"
        "unknown-pkg==1.0.0\n"
    )
    lines = parse_requirements_content(content)
    assert len(lines) == 6
    assert lines[0].line_type == LineType.COMMENT
    assert lines[1].line_type == LineType.EMPTY
    assert lines[2].line_type == LineType.EXACT_PIN
    assert lines[3].line_type == LineType.NON_PINNED_SPECIFIER
    assert lines[4].line_type == LineType.PIP_FLAG
    assert lines[5].line_type == LineType.EXACT_PIN


def test_parse_empty_content():
    """Verify empty string returns empty list."""
    assert parse_requirements_content("") == []
