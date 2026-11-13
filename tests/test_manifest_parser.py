"""Unit tests for Python and Node.js manifest parsers."""
from __future__ import annotations

import pytest

from src.analysis.manifest_parser import (
    ManifestParseError,
    parse_manifest,
    parse_package_json_manifest,
    parse_requirements_manifest,
)
from src.models.manifest import DependencyType, Ecosystem


def test_parse_requirements_exact_pins():
    content = (
        "requests==2.28.0\n"
        "fastapi[all]==0.115.0 ; python_version >= '3.10' # inline comment\n"
        "pydantic==2.8.2\n"
    )
    deps, diags = parse_requirements_manifest(content)
    assert len(deps) == 3
    assert len(diags) == 0

    assert deps[0].name == "requests"
    assert deps[0].declared_specifier == "==2.28.0"
    assert deps[0].current_version == "2.28.0"
    assert deps[0].line_number == 1
    assert deps[0].is_supported is True

    assert deps[1].name == "fastapi"
    assert deps[1].declared_specifier == "==0.115.0"
    assert deps[1].current_version == "0.115.0"
    assert deps[1].extras == "[all]"
    assert deps[1].environment_marker == "python_version >= '3.10'"
    assert deps[1].line_number == 2


def test_parse_requirements_non_pinned_and_bare():
    content = (
        "# Core dependencies\n"
        "pytest>=7.0.0,<=8.0.0\n"
        "click\n"
        "uvicorn~=0.30.0\n"
    )
    deps, diags = parse_requirements_manifest(content)
    assert len(deps) == 3
    assert deps[0].name == "pytest"
    assert deps[0].declared_specifier == ">=7.0.0,<=8.0.0"
    assert deps[0].current_version is None
    assert deps[0].is_supported is True
    assert "Non-pinned constraint" in deps[0].diagnostic_message

    assert deps[1].name == "click"
    assert deps[1].declared_specifier == "*"
    assert deps[1].is_supported is True
    assert "Bare package" in deps[1].diagnostic_message

    assert deps[2].name == "uvicorn"
    assert deps[2].declared_specifier == "~=0.30.0"


def test_parse_requirements_pep508_direct_references_preserved():
    content = (
        "pkg-a @ https://example.com/packages/pkg-a-1.0.whl\n"
        "pkg-b @ git+https://github.com/org/pkg-b.git@v1.2.0\n"
        "pkg-c @ git+ssh://git@github.com/org/pkg-c.git#egg=pkg-c\n"
        "regular-pkg==2.0.0\n"
    )
    deps, diags = parse_requirements_manifest(content)
    assert len(deps) == 4
    assert len(diags) >= 3

    # Direct HTTPS reference
    assert deps[0].name == "pkg-a"
    assert deps[0].original_name == "pkg-a"
    assert deps[0].is_supported is False
    assert "@ https://example.com" in deps[0].declared_specifier
    assert "Preserved direct URL reference" in deps[0].diagnostic_message
    assert deps[0].line_number == 1

    # Direct Git reference
    assert deps[1].name == "pkg-b"
    assert deps[1].is_supported is False
    assert "git+https://" in deps[1].declared_specifier
    assert "Preserved direct URL reference" in deps[1].diagnostic_message
    assert deps[1].line_number == 2

    # Direct SSH reference
    assert deps[2].name == "pkg-c"
    assert deps[2].is_supported is False
    assert deps[2].line_number == 3

    # Regular package still parsed as supported
    assert deps[3].name == "regular-pkg"
    assert deps[3].current_version == "2.0.0"
    assert deps[3].is_supported is True


def test_parse_requirements_ordinary_pep508_complex_markers():
    content = (
        "colorama>=0.4.6 ; os_name == 'nt'\n"
        "importlib-metadata>=4.6 ; python_version < '3.10'\n"
    )
    deps, diags = parse_requirements_manifest(content)
    assert len(deps) == 2
    assert deps[0].name == "colorama"
    assert deps[0].is_supported is True
    assert "os_name == 'nt'" in deps[0].environment_marker

    assert deps[1].name == "importlib-metadata"
    assert deps[1].is_supported is True
    assert "python_version < '3.10'" in deps[1].environment_marker


def test_parse_requirements_flags_and_urls_preserved():
    content = (
        "-r base-requirements.txt\n"
        "-e git+https://github.com/psf/requests.git@main#egg=requests\n"
        "--index-url https://custom.pypi.org/simple\n"
        "https://example.com/wheels/custom_pkg-1.0-py3-none-any.whl\n"
        "valid-pkg==1.0.0\n"
    )
    deps, diags = parse_requirements_manifest(content)
    assert len(deps) == 5
    assert len(diags) >= 4

    assert deps[0].is_supported is False
    assert deps[1].is_supported is False
    assert deps[2].is_supported is False
    assert deps[3].is_supported is False

    assert deps[4].name == "valid-pkg"
    assert deps[4].current_version == "1.0.0"
    assert deps[4].is_supported is True


def test_parse_requirements_unparseable_syntax_contained():
    content = (
        "good-pkg==1.0.0\n"
        "???@@@invalid-syntax-line\n"
        "another-good==2.0.0\n"
    )
    deps, diags = parse_requirements_manifest(content)
    assert len(deps) == 3
    assert len(diags) == 1
    assert deps[0].is_supported is True
    assert deps[1].is_supported is False
    assert "Unparseable requirement syntax" in deps[1].diagnostic_message
    assert deps[2].is_supported is True


def test_parse_package_json_valid():
    content = """{
      "name": "sample-project",
      "version": "1.0.0",
      "dependencies": {
        "react": "^19.0.0",
        "lodash": "4.17.21"
      },
      "devDependencies": {
        "vitest": "~2.1.0",
        "@types/node": ">=20.0.0"
      }
    }"""
    deps, diags = parse_package_json_manifest(content)
    assert len(deps) == 4
    assert len(diags) == 0

    react_dep = next(d for d in deps if d.original_name == "react")
    assert react_dep.dependency_type == DependencyType.RUNTIME
    assert react_dep.declared_specifier == "^19.0.0"
    assert react_dep.is_supported is True

    lodash_dep = next(d for d in deps if d.original_name == "lodash")
    assert lodash_dep.dependency_type == DependencyType.RUNTIME
    assert lodash_dep.declared_specifier == "4.17.21"
    assert lodash_dep.current_version == "4.17.21"
    assert lodash_dep.is_supported is True


def test_parse_package_json_unsupported_npm_specifiers_preserved():
    content = """{
      "dependencies": {
        "pkg-workspace": "workspace:*",
        "pkg-file": "file:../local-pkg",
        "pkg-github": "github:user/repo",
        "pkg-alias": "npm:other-package@^1.0.0",
        "normal-pkg": "^2.0.0"
      }
    }"""
    deps, diags = parse_package_json_manifest(content)
    assert len(deps) == 5
    assert len(diags) == 4

    ws_dep = next(d for d in deps if d.original_name == "pkg-workspace")
    assert ws_dep.is_supported is False
    assert "workspace protocol" in ws_dep.diagnostic_message

    file_dep = next(d for d in deps if d.original_name == "pkg-file")
    assert file_dep.is_supported is False
    assert "local filesystem" in file_dep.diagnostic_message

    gh_dep = next(d for d in deps if d.original_name == "pkg-github")
    assert gh_dep.is_supported is False
    assert "GitHub" in gh_dep.diagnostic_message

    alias_dep = next(d for d in deps if d.original_name == "pkg-alias")
    assert alias_dep.is_supported is False
    assert "npm package alias" in alias_dep.diagnostic_message

    normal_dep = next(d for d in deps if d.original_name == "normal-pkg")
    assert normal_dep.is_supported is True


def test_parse_package_json_empty_or_missing_sections():
    content = '{"name": "minimal-project"}'
    deps, diags = parse_package_json_manifest(content)
    assert len(deps) == 0
    assert len(diags) == 0


def test_parse_package_json_malformed_syntax_fails_gracefully():
    malformed_content = '{"name": "broken", "dependencies": { "react": "^18.0.0", }}'
    with pytest.raises(ManifestParseError) as exc_info:
        parse_package_json_manifest(malformed_content)
    assert "Invalid JSON syntax" in str(exc_info.value)
    assert exc_info.value.diagnostics is not None


def test_parse_package_json_non_dict_root():
    with pytest.raises(ManifestParseError) as exc_info:
        parse_package_json_manifest('["an", "array"]')
    assert "root must be a JSON object" in str(exc_info.value)


def test_parse_manifest_unified_dispatcher():
    # Requirements
    deps, diags, eco = parse_manifest("requirements.txt", "requests==2.31.0\n")
    assert eco == Ecosystem.PYTHON
    assert len(deps) == 1

    # Package.json
    deps2, diags2, eco2 = parse_manifest("package.json", '{"dependencies": {"express": "^4.18.0"}}')
    assert eco2 == Ecosystem.NPM
    assert len(deps2) == 1

    # Unsupported format
    with pytest.raises(ManifestParseError) as exc_info:
        parse_manifest("Cargo.toml", "[dependencies]")
    assert "Unsupported manifest type" in str(exc_info.value)
