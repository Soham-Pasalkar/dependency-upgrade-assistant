"""End-to-end integration tests for Dependency Upgrade Assistant pipeline."""
from src.core.comparator import compare_requirements
from src.core.diff_generator import generate_unified_diff, synthesize_updated_requirements
from src.core.models import UpgradeStatus
from src.core.parser import parse_requirements_content
from src.data.catalog_loader import Catalog, load_catalog


def test_full_pipeline_with_production_catalog():
    """Verify complete pipeline using default 20-package local catalog."""
    catalog = load_catalog()

    sample_input = (
        "# --- Application Requirements ---\n"
        "-i https://pypi.org/simple\n"
        "\n"
        "requests==2.28.0\n"
        "urllib3==1.26.15\n"
        "flask>=3.0.0\n"
        "pytest==8.3.3\n"
        "my-private-pkg==0.9.1\n"
        "# End of file\n"
    )

    # 1. Parse
    lines = parse_requirements_content(sample_input)
    assert len(lines) == 9

    # 2. Compare against catalog
    evaluations = compare_requirements(lines, catalog)
    assert len(evaluations) == 9

    # Map by line number
    by_line = {e.line.line_number: e for e in evaluations}

    # requests is outdated (line 4)
    assert by_line[4].status == UpgradeStatus.UPGRADE_AVAILABLE
    assert by_line[4].is_upgrade_candidate is True
    assert by_line[4].candidate_version == "2.32.3"

    # urllib3 is outdated (line 5)
    assert by_line[5].status == UpgradeStatus.UPGRADE_AVAILABLE
    assert by_line[5].is_upgrade_candidate is True
    assert by_line[5].candidate_version == "2.2.3"

    # flask is non-pinned (line 6)
    assert by_line[6].status == UpgradeStatus.PRESERVED_NON_PIN
    assert by_line[6].is_upgrade_candidate is False

    # pytest is up-to-date (line 7)
    assert by_line[7].status == UpgradeStatus.UP_TO_DATE
    assert by_line[7].is_upgrade_candidate is False

    # my-private-pkg is unknown (line 8)
    assert by_line[8].status == UpgradeStatus.UNKNOWN_PACKAGE
    assert by_line[8].is_upgrade_candidate is False

    # 3. Simulate accepting only requests (line 4)
    updated_single = synthesize_updated_requirements(evaluations, accepted_line_numbers={4})
    diff_single = generate_unified_diff(sample_input, updated_single)

    assert "-requests==2.28.0" in diff_single
    assert "+requests==2.32.3" in diff_single
    assert "urllib3==1.26.15" in updated_single  # untouched

    # 4. Simulate accepting all available upgrades ({4, 5})
    all_candidates = {e.line.line_number for e in evaluations if e.is_upgrade_candidate}
    assert all_candidates == {4, 5}

    updated_all = synthesize_updated_requirements(evaluations, accepted_line_numbers=all_candidates)
    diff_all = generate_unified_diff(sample_input, updated_all)

    assert "-requests==2.28.0" in diff_all
    assert "+requests==2.32.3" in diff_all
    assert "-urllib3==1.26.15" in diff_all
    assert "+urllib3==2.2.3" in diff_all
    assert "# --- Application Requirements ---" in updated_all
    assert "-i https://pypi.org/simple" in updated_all
    assert "flask>=3.0.0" in updated_all
    assert "my-private-pkg==0.9.1" in updated_all
    assert "# End of file" in updated_all


def test_pipeline_with_comment_only_file():
    """Verify handling of files containing only comments and empty lines."""
    catalog = load_catalog()
    content = "# Just a header\n\n# Another note\n"
    lines = parse_requirements_content(content)
    evaluations = compare_requirements(lines, catalog)

    assert all(e.status == UpgradeStatus.PRESERVED_PASSTHROUGH for e in evaluations)
    updated = synthesize_updated_requirements(evaluations, accepted_line_numbers=set())
    assert updated == content
    assert generate_unified_diff(content, updated) == ""


def test_sample_files_load_and_evaluate_cleanly():
    """Verify that all files in sample-data/ can be loaded, parsed, and evaluated."""
    from src.app import SAMPLE_DATA_DIR, load_sample_file

    sample_files = ["requirements-outdated.txt", "requirements-clean.txt", "requirements-complex.txt"]
    catalog = load_catalog()

    for s_file in sample_files:
        content = load_sample_file(s_file)
        assert len(content) > 0
        lines = parse_requirements_content(content)
        assert len(lines) > 0
        evaluations = compare_requirements(lines, catalog)
        assert len(evaluations) == len(lines)

