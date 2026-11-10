"""Unit tests for surgical manifest modification across Python requirements.txt and Node.js package.json."""
from __future__ import annotations

import json
import pytest

from src.models.manifest import DependencyType, Ecosystem
from src.models.plan import PlannedUpgradeItem, UpgradeAction
from src.services.manifest_modifier import (
    ManifestMutationError,
    modify_manifest,
    modify_package_json_manifest,
    modify_requirements_manifest,
)


def test_requirements_single_pin_upgrade():
    """Verify single exact pin is updated while preserving newline endings."""
    original = "requests==2.28.0\nflask==2.2.5\n"
    items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        )
    ]
    proposed, diags = modify_manifest("requirements.txt", original, items)
    expected = "requests==2.32.3\nflask==2.2.5\n"
    assert proposed == expected
    assert any("Updated 'requests' to version 2.32.3" in d for d in diags)


def test_requirements_multiple_pin_upgrades():
    """Verify multiple dependencies can be updated in a single pass."""
    original = "requests==2.28.0\nflask==2.2.5\nurllib3==1.26.0\n"
    items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        ),
        PlannedUpgradeItem(
            package_name="flask",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.2.5",
            target_version="3.1.0",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        ),
    ]
    proposed, _ = modify_manifest("requirements.txt", original, items)
    expected = "requests==2.32.3\nflask==3.1.0\nurllib3==1.26.0\n"
    assert proposed == expected


def test_requirements_preserves_comments_whitespace_and_blank_lines():
    """Verify full-line comments, inline comments, leading whitespace, and blank lines are preserved."""
    original = (
        "# Core dependencies\n"
        "\n"
        "  requests==2.28.0  # Main HTTP client\n"
        "# Secondary\n"
        "flask==2.2.5\n"
    )
    items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        )
    ]
    proposed, _ = modify_manifest("requirements.txt", original, items)
    expected = (
        "# Core dependencies\n"
        "\n"
        "  requests==2.32.3 # Main HTTP client\n"
        "# Secondary\n"
        "flask==2.2.5\n"
    )
    assert proposed == expected


def test_requirements_preserves_extras_and_environment_markers():
    """Verify package extras and environment markers are intact after modification."""
    original = "requests[security,socks]==2.28.0; python_version < '3.11' # pinned\n"
    items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        )
    ]
    proposed, _ = modify_manifest("requirements.txt", original, items)
    expected = "requests[security,socks]==2.32.3; python_version < '3.11' # pinned\n"
    assert proposed == expected


def test_requirements_updates_range_and_compatible_release_constraints():
    """Verify ~= and >= operators are updated with target version."""
    original = "requests~=2.28.0\nurllib3>=1.26.0\n"
    items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        ),
        PlannedUpgradeItem(
            package_name="urllib3",
            ecosystem=Ecosystem.PYTHON,
            current_version="1.26.0",
            target_version="2.2.0",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        ),
    ]
    proposed, _ = modify_manifest("requirements.txt", original, items)
    assert "requests~=2.32.3\n" in proposed
    assert "urllib3>=2.2.0\n" in proposed


def test_requirements_duplicate_entries_disambiguated_by_line_number():
    """Verify duplicate packages on separate lines with environment markers are updated independently."""
    original = (
        "requests==2.28.0; python_version < '3.11'\n"
        "requests==2.31.0; python_version >= '3.11'\n"
    )
    # Only upgrade the second line (line 2)
    items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.31.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade python 3.11+ pin",
            line_number=2,
        )
    ]
    proposed, _ = modify_manifest("requirements.txt", original, items)
    expected = (
        "requests==2.28.0; python_version < '3.11'\n"
        "requests==2.32.3; python_version >= '3.11'\n"
    )
    assert proposed == expected


def test_requirements_preserves_pip_directives_and_urls():
    """Verify pip flags, options, and VCS URLs are preserved without modification."""
    original = (
        "-r base.txt\n"
        "--extra-index-url https://custom.pypi.org/simple\n"
        "git+https://github.com/psf/requests.git@main#egg=requests\n"
        "flask==2.2.5\n"
    )
    items = [
        PlannedUpgradeItem(
            package_name="flask",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.2.5",
            target_version="3.1.0",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade flask",
        )
    ]
    proposed, _ = modify_manifest("requirements.txt", original, items)
    assert "-r base.txt\n" in proposed
    assert "--extra-index-url https://custom.pypi.org/simple\n" in proposed
    assert "git+https://github.com/psf/requests.git@main#egg=requests\n" in proposed
    assert "flask==3.1.0\n" in proposed


def test_requirements_noop_when_no_upgrades():
    """Verify original content is preserved exactly when plan contains no upgrades."""
    original = "requests==2.28.0\n# comment\n"
    items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.28.0",
            action=UpgradeAction.RETAIN,
            rationale="Retained",
        )
    ]
    proposed, _ = modify_manifest("requirements.txt", original, items)
    assert proposed == original


def test_package_json_preserves_caret_and_tilde_range_prefixes():
    """Verify package.json version update preserves ^ and ~ prefixes."""
    original_dict = {
        "name": "my-app",
        "version": "1.0.0",
        "dependencies": {
            "react": "^18.2.0",
            "lodash": "~4.17.20",
            "axios": "1.4.0",
        },
    }
    original = json.dumps(original_dict, indent=2) + "\n"
    items = [
        PlannedUpgradeItem(
            package_name="react",
            ecosystem=Ecosystem.NPM,
            current_version="18.2.0",
            target_version="18.3.1",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade react",
        ),
        PlannedUpgradeItem(
            package_name="lodash",
            ecosystem=Ecosystem.NPM,
            current_version="4.17.20",
            target_version="4.17.21",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade lodash",
        ),
        PlannedUpgradeItem(
            package_name="axios",
            ecosystem=Ecosystem.NPM,
            current_version="1.4.0",
            target_version="1.7.9",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade axios",
        ),
    ]
    proposed, diags = modify_manifest("package.json", original, items)
    parsed = json.loads(proposed)

    assert parsed["dependencies"]["react"] == "^18.3.1"
    assert parsed["dependencies"]["lodash"] == "~4.17.21"
    assert parsed["dependencies"]["axios"] == "1.7.9"
    assert parsed["name"] == "my-app"
    assert parsed["version"] == "1.0.0"
    assert proposed.endswith("\n")


def test_package_json_updates_dev_dependencies():
    """Verify devDependencies section is modified while preserving scripts and other keys."""
    original_dict = {
        "name": "project",
        "scripts": {"test": "vitest"},
        "devDependencies": {
            "typescript": "^5.0.0",
            "vite": "^4.0.0",
        },
    }
    original = json.dumps(original_dict, indent=2) + "\n"
    items = [
        PlannedUpgradeItem(
            package_name="vite",
            ecosystem=Ecosystem.NPM,
            current_version="4.0.0",
            target_version="5.4.0",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade vite",
            dependency_type=DependencyType.DEVELOPMENT,
        )
    ]
    proposed, _ = modify_manifest("package.json", original, items)
    parsed = json.loads(proposed)
    assert parsed["devDependencies"]["vite"] == "^5.4.0"
    assert parsed["devDependencies"]["typescript"] == "^5.0.0"
    assert parsed["scripts"]["test"] == "vitest"


def test_package_json_malformed_fails_gracefully():
    """Verify malformed JSON raises ManifestMutationError without mutating content."""
    bad_json = "{\n  \"dependencies\": { bad\n"
    items = [
        PlannedUpgradeItem(
            package_name="react",
            ecosystem=Ecosystem.NPM,
            current_version="18.0.0",
            target_version="18.3.1",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        )
    ]
    with pytest.raises(ManifestMutationError, match="invalid JSON syntax"):
        modify_manifest("package.json", bad_json, items)


def test_unsupported_manifest_format_raises_error():
    """Verify unsupported manifest format raises descriptive ManifestMutationError."""
    with pytest.raises(ManifestMutationError, match="Unsupported manifest format"):
        modify_manifest("Cargo.toml", "[dependencies]", [])

