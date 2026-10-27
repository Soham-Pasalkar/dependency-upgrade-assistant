"""Unit tests for semantic version comparison and upgrade evaluation."""
from packaging.version import parse as parse_version
from src.core.comparator import compare_requirements
from src.core.models import UpgradeStatus
from src.core.parser import parse_requirements_content
from src.data.catalog_loader import Catalog


def test_pep440_semantic_ordering_vs_alphabetical(mock_catalog: Catalog):
    """Verify that version comparison uses PEP 440 semantics, NOT alphabetical string sorting.
    
    In alphabetical string comparison: '2.10.0' < '2.9.0' (incorrect).
    In PEP 440 semantic comparison: Version('2.10.0') > Version('2.9.0') (correct).
    """
    # Alphabetical check proving string comparison would fail
    assert "2.10.0" < "2.9.0"

    # Semantic check with packaging
    assert parse_version("2.10.0") > parse_version("2.9.0")

    # In mock_catalog, demo-semver candidate is 2.10.0
    content = "demo-semver==2.9.0\n"
    lines = parse_requirements_content(content)
    evaluations = compare_requirements(lines, mock_catalog)

    assert len(evaluations) == 1
    res = evaluations[0]
    assert res.status == UpgradeStatus.UPGRADE_AVAILABLE
    assert res.is_upgrade_candidate is True
    assert res.current_version == "2.9.0"
    assert res.candidate_version == "2.10.0"


def test_upgrade_available_for_outdated_package(mock_catalog: Catalog):
    """Verify that an outdated pinned package is identified as an upgrade candidate."""
    content = "requests==2.28.0\n"
    lines = parse_requirements_content(content)
    evaluations = compare_requirements(lines, mock_catalog)

    assert len(evaluations) == 1
    res = evaluations[0]
    assert res.status == UpgradeStatus.UPGRADE_AVAILABLE
    assert res.is_upgrade_candidate is True
    assert res.current_version == "2.28.0"
    assert res.candidate_version == "2.32.3"


def test_up_to_date_package(mock_catalog: Catalog):
    """Verify that a package at or newer than catalog candidate is marked Up to Date."""
    content = "requests==2.32.3\nflask==4.0.0\n"
    lines = parse_requirements_content(content)
    evaluations = compare_requirements(lines, mock_catalog)

    assert len(evaluations) == 2
    assert evaluations[0].status == UpgradeStatus.UP_TO_DATE
    assert evaluations[0].is_upgrade_candidate is False
    assert evaluations[1].status == UpgradeStatus.UP_TO_DATE
    assert evaluations[1].is_upgrade_candidate is False


def test_unknown_package(mock_catalog: Catalog):
    """Verify that packages not in local catalog are marked Unknown Package."""
    content = "my-custom-internal-library==1.0.0\n"
    lines = parse_requirements_content(content)
    evaluations = compare_requirements(lines, mock_catalog)

    assert len(evaluations) == 1
    assert evaluations[0].status == UpgradeStatus.UNKNOWN_PACKAGE
    assert evaluations[0].is_upgrade_candidate is False


def test_preserved_non_pinned_and_passthrough_lines(mock_catalog: Catalog):
    """Verify that non-pinned requirements and comments/flags are preserved without upgrade proposals."""
    content = (
        "# Core section\n"
        "requests>=2.28.0\n"
        "flask~=2.0.0\n"
        "-r base.txt\n"
        ">>> completely unparseable syntax <<<\n"
        "package===arbitrary_equality\n"
    )
    lines = parse_requirements_content(content)
    evaluations = compare_requirements(lines, mock_catalog)

    assert len(evaluations) == 6
    assert evaluations[0].status == UpgradeStatus.PRESERVED_PASSTHROUGH
    assert evaluations[1].status == UpgradeStatus.PRESERVED_NON_PIN
    assert evaluations[1].is_upgrade_candidate is False
    assert evaluations[2].status == UpgradeStatus.PRESERVED_NON_PIN
    assert evaluations[2].is_upgrade_candidate is False
    assert evaluations[3].status == UpgradeStatus.PRESERVED_PASSTHROUGH
    assert evaluations[4].status == UpgradeStatus.PRESERVED_PASSTHROUGH
    assert evaluations[5].status == UpgradeStatus.PRESERVED_NON_PIN

