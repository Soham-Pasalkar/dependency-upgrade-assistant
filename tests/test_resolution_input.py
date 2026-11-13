"""Supervisor input classification (FR-23, FR-26; M12 criterion 4 classification part, criterion 2 constraints)."""
from __future__ import annotations

import os

import pytest

from src.resolution.models import UnsupportedReason as R
from src.resolution.python_input import (
    MAX_CONSTRAINTS_BYTES,
    MAX_INPUT_BYTES,
    classify_plan,
    classify_requirements,
)

H = "a" * 64


def reasons(result):
    return [u.reason for u in result.unsupported]


def test_plain_requirements_are_sanitized():
    content = (
        "# comment\n"
        "requests==2.28.0  # pinned\n"
        "Django>=4.2,<5  \n"
        "\n"
        'pandas[performance]>=1.5 ; python_version >= "3.9"\n'
        f"six==1.16.0 --hash=sha256:{H}\n"
        "numpy==1.24.0 \\\n"
        f"    --hash sha256:{H.upper()}\n"
    )
    r = classify_requirements(content)
    assert r.supported
    assert r.requirements == (
        "requests==2.28.0",
        "Django<5,>=4.2",
        'pandas[performance]>=1.5; python_version >= "3.9"',
        f"six==1.16.0 --hash=sha256:{H}",
        f"numpy==1.24.0 --hash=sha256:{H}",
    )
    assert r.direct_names == ("requests", "django", "pandas", "six", "numpy")
    assert r.sanitized_requirements.endswith("\n")


@pytest.mark.parametrize(
    "line,reason",
    [
        ("-r other.txt", R.NESTED_REQUIREMENTS),
        ("--requirement=other.txt", R.NESTED_REQUIREMENTS),
        ("-rother.txt", R.NESTED_REQUIREMENTS),
        ("-e .", R.EDITABLE),
        ("--editable git+https://github.com/x/y", R.EDITABLE),
        ("--index-url https://evil.example/simple", R.PIP_OPTION),
        ("-i https://evil.example/simple", R.PIP_OPTION),
        ("--extra-index-url https://evil.example/simple", R.PIP_OPTION),
        ("--find-links /tmp", R.PIP_OPTION),
        ("--trusted-host evil.example", R.PIP_OPTION),
        ("--pre", R.PIP_OPTION),
        ("--no-binary :all:", R.PIP_OPTION),
        ("--require-hashes", R.PIP_OPTION),
        ("requests==2.0 --global-option=--x", R.PIP_OPTION),
        ("requests==2.0 --config-settings=a=b", R.PIP_OPTION),
        ("git+https://github.com/psf/requests", R.DIRECT_REFERENCE),
        ("requests @ https://example.com/requests.whl", R.DIRECT_REFERENCE),
        ("https://example.com/pkg-1.0.tar.gz", R.DIRECT_REFERENCE),
        ("./local/pkg", R.DIRECT_REFERENCE),
        ("/abs/path/pkg", R.DIRECT_REFERENCE),
        ("~/pkg", R.DIRECT_REFERENCE),
        ("file:///tmp/pkg", R.DIRECT_REFERENCE),
        ("pkg-1.0-py3-none-any.whl", R.DIRECT_REFERENCE),
        ("dir/pkg", R.DIRECT_REFERENCE),
        ("C:\\pkgs\\x.whl", R.DIRECT_REFERENCE),
        ("requests==${VERSION}", R.ENV_VARIABLE),
        ("${PKG}", R.ENV_VARIABLE),
        ("requests>=1,<", R.UNPARSEABLE),
        ("not a requirement!!", R.UNPARSEABLE),
        ("six==1.0 --hash=md5:abc", R.UNPARSEABLE),
        ("six==1.0 --hash=sha256:abc", R.UNPARSEABLE),
    ],
)
def test_unsupported_lines(line, reason):
    r = classify_requirements(f"requests==2.28.0\n{line}\n")
    assert reasons(r) == [reason]
    item = r.unsupported[0]
    assert item.line_number == 2 and item.source == "manifest" and item.text
    assert not r.supported


def test_every_unsupported_line_is_listed():
    r = classify_requirements("-r a.txt\n-e .\n--pre\nok==1.0\n")
    assert [(u.line_number, u.reason) for u in r.unsupported] == [
        (1, R.NESTED_REQUIREMENTS), (2, R.EDITABLE), (3, R.PIP_OPTION)]


def test_oversized_manifest_is_unsupported():
    r = classify_requirements("a==1\n" * (MAX_INPUT_BYTES // 5 + 1))
    assert reasons(r) == [R.INPUT_TOO_LARGE]


def test_overlong_line_is_unsupported():
    assert reasons(classify_requirements("a" * 5000 + "==1\n")) == [R.UNPARSEABLE]


def test_text_is_truncated():
    r = classify_requirements("-r " + "x" * 1000 + "\n")
    assert len(r.unsupported[0].text) == 200


@pytest.mark.parametrize(
    "manifest_type,ecosystem,file_path,reason",
    [
        ("package.json", "npm", "package.json", R.NON_PYTHON),
        ("requirements.txt", "npm", "requirements.txt", R.NON_PYTHON),
        ("pyproject.toml", "python", "pyproject.toml", R.PYPROJECT_ONLY),
        ("requirements.txt", "python", "sub/pyproject.toml", R.PYPROJECT_ONLY),
        ("Pipfile", "python", "Pipfile", R.NON_PYTHON),
    ],
)
def test_unsupported_manifest_kinds_block_both_roles(manifest_type, ecosystem, file_path, reason):
    plan = classify_plan(manifest_type, ecosystem, file_path, "a==1\n", "a==2\n")
    assert reasons(plan.original) == [reason] and reasons(plan.proposed) == [reason]


def test_npm_message_states_not_yet_supported():
    plan = classify_plan("package.json", "npm", "package.json", "{}", "{}")
    assert "npm compatibility verification is not yet supported" in plan.proposed.unsupported[0].message


def test_roles_are_classified_independently():
    plan = classify_plan("requirements.txt", "python", "requirements.txt", "-e .\na==1\n", "a==2\n")
    assert not plan.original.supported and plan.proposed.supported


# -- constraints files (FR-26) -------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "repo"
    (ws / "svc").mkdir(parents=True)
    (ws / "svc" / "constraints.txt").write_text("numpy<2\n# c\nscipy<1.11\n")
    (ws / "top.txt").write_text("pandas<2\n")
    return ws


def test_repository_local_constraints_are_accepted(workspace):
    r = classify_requirements("numpy\n-c constraints.txt\n--constraint=../top.txt\n",
                              manifest_path="svc/requirements.txt", workspace_dir=str(workspace))
    assert r.supported
    assert [c.path for c in r.constraints] == ["svc/constraints.txt", "top.txt"]
    assert r.constraints[0].content == "numpy<2\nscipy<1.11\n"
    assert len(r.constraints[0].sha256) == 64
    assert r.requirements == ("numpy",)  # -c lines are passed as arguments, not in the file


def test_duplicate_constraints_reference_is_deduplicated(workspace):
    r = classify_requirements("-c constraints.txt\n-cconstraints.txt\n", manifest_path="svc/requirements.txt",
                              workspace_dir=str(workspace))
    assert r.supported and len(r.constraints) == 1


def test_constraints_need_a_repository_workspace():
    assert reasons(classify_requirements("-c constraints.txt\n")) == [R.CONSTRAINTS_OUTSIDE_WORKSPACE]


@pytest.mark.parametrize("ref", ["../../outside.txt", "/etc/passwd", "https://example.com/c.txt",
                                 "file:///etc/passwd"])
def test_constraints_outside_repository_are_refused(workspace, ref):
    (workspace.parent / "outside.txt").write_text("a<1\n")
    r = classify_requirements(f"-c {ref}\n", manifest_path="svc/requirements.txt", workspace_dir=str(workspace))
    assert reasons(r) == [R.CONSTRAINTS_OUTSIDE_WORKSPACE]


def test_constraints_symlink_escape_is_refused(workspace):
    secret = workspace.parent / "secret.txt"
    secret.write_text("a<1\n")
    os.symlink(secret, workspace / "svc" / "link.txt")
    r = classify_requirements("-c link.txt\n", manifest_path="svc/requirements.txt", workspace_dir=str(workspace))
    assert reasons(r) == [R.CONSTRAINTS_OUTSIDE_WORKSPACE]


def test_missing_or_oversized_constraints_are_invalid(workspace):
    (workspace / "big.txt").write_text("a<1\n" * (MAX_CONSTRAINTS_BYTES // 4 + 1))
    r = classify_requirements("-c missing.txt\n-c big.txt\n", manifest_path="requirements.txt",
                              workspace_dir=str(workspace))
    assert reasons(r) == [R.CONSTRAINTS_INVALID, R.CONSTRAINTS_INVALID]


def test_constraints_content_is_classified(workspace):
    (workspace / "bad.txt").write_text("a<1\n-c other.txt\n--index-url https://evil.example\ngit+https://x/y\n")
    r = classify_requirements("-c bad.txt\n", manifest_path="requirements.txt", workspace_dir=str(workspace))
    assert reasons(r) == [R.CONSTRAINTS_INVALID, R.PIP_OPTION, R.DIRECT_REFERENCE]
    assert {u.source for u in r.unsupported} == {"bad.txt"}
    assert r.constraints == ()


def test_too_many_constraints_files(workspace):
    for i in range(9):
        (workspace / f"c{i}.txt").write_text("a<1\n")
    r = classify_requirements("".join(f"-c c{i}.txt\n" for i in range(9)), manifest_path="requirements.txt",
                              workspace_dir=str(workspace))
    assert reasons(r) == [R.CONSTRAINTS_INVALID] and len(r.constraints) == 8
