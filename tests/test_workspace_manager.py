"""Unit tests for WorkspaceManager and temporary dual-copy isolation."""
from pathlib import Path

import pytest

from src.services.workspace_manager import WorkspaceSession


def test_workspace_session_initialization(tmp_path: Path):
    """Verify initialization of workspace directory skeleton."""
    session = WorkspaceSession(base_temp_dir=tmp_path)
    
    try:
        assert session.is_active is True
        assert session.root_dir.exists()
        assert session.reference_dir.exists()
        assert session.working_dir.parent == session.root_dir
        assert "dua_ws_" in str(session.root_dir.name)
    finally:
        session.cleanup()

    assert session.is_active is False
    assert not session.root_dir.exists()


def test_prepare_working_copy_success(tmp_path: Path):
    """Verify deep-copy of reference/ into working/."""
    with WorkspaceSession(base_temp_dir=tmp_path) as session:
        # Populate reference with sample files
        (session.reference_dir / "README.md").write_text("# Test Repo", encoding="utf-8")
        src_dir = session.reference_dir / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("print('hello')", encoding="utf-8")

        # Prepare working copy
        working_path = session.prepare_working_copy()

        assert working_path == session.working_dir
        assert session.working_dir.exists()
        assert (session.working_dir / "README.md").read_text(encoding="utf-8") == "# Test Repo"
        assert (session.working_dir / "src" / "main.py").read_text(encoding="utf-8") == "print('hello')"


def test_working_copy_isolation(tmp_path: Path):
    """Verify that edits to working/ do NOT alter the pristine reference/ copy."""
    with WorkspaceSession(base_temp_dir=tmp_path) as session:
        test_file = session.reference_dir / "app.py"
        test_file.write_text("original_content = 1\n", encoding="utf-8")

        session.prepare_working_copy()

        # Mutate working copy
        working_file = session.working_dir / "app.py"
        working_file.write_text("modified_content = 2\n", encoding="utf-8")

        # Assert reference copy remains untouched
        assert test_file.read_text(encoding="utf-8") == "original_content = 1\n"
        assert working_file.read_text(encoding="utf-8") == "modified_content = 2\n"


def test_prepare_working_copy_fails_if_reference_empty(tmp_path: Path):
    """Verify that preparing working copy on empty reference raises FileNotFoundError."""
    with WorkspaceSession(base_temp_dir=tmp_path) as session:
        with pytest.raises(FileNotFoundError, match="Reference directory is empty or missing"):
            session.prepare_working_copy()


def test_list_working_files_filters_properly(tmp_path: Path):
    """Verify that list_working_files excludes .git, .venv, caches, etc."""
    with WorkspaceSession(base_temp_dir=tmp_path) as session:
        # Create valid source files
        src = session.reference_dir / "src"
        src.mkdir()
        (src / "app.py").write_text("code", encoding="utf-8")
        (session.reference_dir / "setup.py").write_text("setup", encoding="utf-8")
        (session.reference_dir / "data.json").write_text("{}", encoding="utf-8")

        # Create files inside excluded directories
        venv_dir = session.reference_dir / ".venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "library.py").write_text("lib", encoding="utf-8")

        cache_dir = session.reference_dir / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "app.cpython-314.py").write_text("bytecode", encoding="utf-8")

        git_dir = session.reference_dir / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        (git_dir / "hook.py").write_text("hook", encoding="utf-8")

        session.prepare_working_copy()
        python_files = session.list_working_files(extension=".py")

        file_names = [f.name for f in python_files]
        assert "app.py" in file_names
        assert "setup.py" in file_names
        assert "data.json" not in file_names
        assert "library.py" not in file_names
        assert "app.cpython-314.py" not in file_names
        assert "hook.py" not in file_names
        assert len(python_files) == 2


def test_workspace_context_manager_cleanup(tmp_path: Path):
    """Verify that exiting context manager deletes the workspace directory."""
    captured_root: Path

    with WorkspaceSession(base_temp_dir=tmp_path) as session:
        captured_root = session.root_dir
        assert captured_root.exists()

    assert not captured_root.exists()

