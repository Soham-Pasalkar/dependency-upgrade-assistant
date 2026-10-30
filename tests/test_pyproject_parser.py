"""Unit tests for Python 3.10–3.14 compatible pyproject.toml parser."""
from pathlib import Path
import pytest

from src.analysis.pyproject_parser import (
    ParsedPyProjectDependency,
    PyProjectParseResult,
    parse_pyproject_toml,
)
from src.core.models import LineType


def test_parse_pep621_standard_dependencies(tmp_path: Path):
    """Verify parsing of standard [project.dependencies]."""
    content = """
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "sample-app"
version = "1.0.0"
requires-python = ">=3.10"
dependencies = [
    "requests==2.28.0",
    "urllib3>=1.26.0,<3.0.0",
    "pydantic[email]==2.5.0; python_version >= '3.10'",
    "flask",
]
"""
    f = tmp_path / "pyproject.toml"
    f.write_text(content, encoding="utf-8")

    result = parse_pyproject_toml(f)

    assert result.project_name == "sample-app"
    assert result.project_version == "1.0.0"
    assert result.build_backend == "setuptools.build_meta"
    assert len(result.dependencies) == 4

    by_name = {d.package_name: d for d in result.dependencies}

    # requests is exact pin
    req = by_name["requests"]
    assert req.version == "2.28.0"
    assert req.operator == "=="
    assert req.is_exact_pin is True
    rline = req.to_requirement_line()
    assert rline.line_type == LineType.EXACT_PIN
    assert rline.version == "2.28.0"

    # urllib3 is range
    url = by_name["urllib3"]
    assert url.is_exact_pin is False
    assert ">=1.26.0" in url.operator
    assert "<3.0.0" in url.operator

    # pydantic has extras and marker
    pyd = by_name["pydantic"]
    assert pyd.extras == "[email]"
    assert pyd.is_exact_pin is True
    assert pyd.version == "2.5.0"
    assert "python_version" in str(pyd.environment_marker)
    assert "3.10" in str(pyd.environment_marker)

    # flask is unpinned
    flask = by_name["flask"]
    assert flask.is_exact_pin is False
    assert flask.version is None


def test_parse_pep621_optional_dependencies(tmp_path: Path):
    """Verify parsing of [project.optional-dependencies] with group identification."""
    content = """
[project]
name = "my-lib"
dependencies = ["numpy>=1.20"]

[project.optional-dependencies]
test = [
    "pytest==8.0.0",
    "pytest-cov>=4.0.0",
]
docs = [
    "sphinx==7.1.0",
]
"""
    f = tmp_path / "pyproject.toml"
    f.write_text(content, encoding="utf-8")

    result = parse_pyproject_toml(f)
    assert len(result.dependencies) == 4

    by_name = {d.package_name: d for d in result.dependencies}
    pytest_dep = by_name["pytest"]
    assert pytest_dep.group_name == "test"
    assert pytest_dep.source_section == "project.optional-dependencies.test"
    assert pytest_dep.is_exact_pin is True
    assert pytest_dep.version == "8.0.0"

    sphinx_dep = by_name["sphinx"]
    assert sphinx_dep.group_name == "docs"
    assert sphinx_dep.is_exact_pin is True


def test_parse_pep735_dependency_groups(tmp_path: Path):
    """Verify parsing of PEP 735 [dependency-groups]."""
    content = """
[dependency-groups]
dev = [
    "ruff==0.3.0",
    {include-group = "test"},
]
test = [
    "pytest>=8.0.0",
]
"""
    f = tmp_path / "pyproject.toml"
    f.write_text(content, encoding="utf-8")

    result = parse_pyproject_toml(f)
    assert len(result.dependencies) == 2

    by_name = {d.package_name: d for d in result.dependencies}
    assert "ruff" in by_name
    assert by_name["ruff"].is_exact_pin is True
    assert by_name["ruff"].group_name == "dev"
    assert "pytest" in by_name


def test_parse_poetry_dependencies(tmp_path: Path):
    """Verify parsing of Poetry [tool.poetry.dependencies] and group tables."""
    content = """
[tool.poetry]
name = "poetry-app"

[tool.poetry.dependencies]
python = "^3.10"
requests = "^2.28.0"
certifi = "==2024.2.2"
pydantic = {version = "2.6.0", optional = true, extras = ["dotenv"]}

[tool.poetry.group.dev.dependencies]
black = "24.1.0"
"""
    f = tmp_path / "pyproject.toml"
    f.write_text(content, encoding="utf-8")

    result = parse_pyproject_toml(f)
    by_name = {d.package_name: d for d in result.dependencies}

    # Python entry must be filtered out
    assert "python" not in by_name

    # requests with caret
    assert "requests" in by_name
    assert by_name["requests"].specifier == "^2.28.0"

    # certifi exact pin
    assert "certifi" in by_name
    assert by_name["certifi"].is_exact_pin is True
    assert by_name["certifi"].version == "2024.2.2"

    # pydantic dict spec
    assert "pydantic" in by_name
    assert by_name["pydantic"].is_exact_pin is True
    assert by_name["pydantic"].version == "2.6.0"
    assert by_name["pydantic"].extras == "[dotenv]"

    # black from dev group
    assert "black" in by_name
    assert by_name["black"].group_name == "dev"
    assert by_name["black"].is_exact_pin is True


def test_unsupported_or_malformed_entries_reported(tmp_path: Path):
    """Verify invalid PEP 508 syntax is reported in unsupported_declarations without crash."""
    content = """
[project]
dependencies = [
    "valid-pkg==1.0.0",
    "invalid pkg syntax ??? == 123",
]
"""
    f = tmp_path / "pyproject.toml"
    f.write_text(content, encoding="utf-8")

    result = parse_pyproject_toml(f)
    assert len(result.dependencies) == 1
    assert result.dependencies[0].package_name == "valid-pkg"
    assert len(result.unsupported_declarations) == 1
    assert "Invalid PEP 508 requirement" in result.unsupported_declarations[0]


def test_file_not_found_and_invalid_toml(tmp_path: Path):
    """Verify FileNotFoundError and ValueError on missing or corrupt TOML."""
    with pytest.raises(FileNotFoundError):
        parse_pyproject_toml(tmp_path / "non_existent.toml")

    corrupt = tmp_path / "corrupt.toml"
    corrupt.write_text("[[[not valid toml :::", encoding="utf-8")
    with pytest.raises(ValueError, match="Failed to parse pyproject.toml"):
        parse_pyproject_toml(corrupt)
