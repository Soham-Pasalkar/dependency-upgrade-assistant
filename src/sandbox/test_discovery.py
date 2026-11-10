"""Explicit test discovery to prevent false-positive validation outcomes."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class TestDiscoveryStatus(str, Enum):
    """Categorization of test discovery outcome."""
    __test__ = False
    DISCOVERED = "DISCOVERED"
    NO_TESTS_FOUND = "NO_TESTS_FOUND"
    UNSUPPORTED = "UNSUPPORTED"
    DISCOVERY_ERROR = "DISCOVERY_ERROR"


@dataclass
class TestDiscoveryResult:
    """Detailed outcome of test suite discovery in a repository snapshot."""
    __test__ = False
    status: TestDiscoveryStatus
    framework: str  # "pytest", "npm_test", "unittest", "none"
    test_files: List[str] = field(default_factory=list)
    test_command: List[str] = field(default_factory=list)
    description: str = ""

    @property
    def has_runnable_tests(self) -> bool:
        return self.status == TestDiscoveryStatus.DISCOVERED and len(self.test_command) > 0


class TestDiscoveryManager:
    """Discovers test suites and verifies that runnable tests actually exist before execution."""
    __test__ = False

    @staticmethod
    def discover_tests(
        snapshot_dir: Path | str,
        ecosystem: str = "python",
        custom_cmd: Optional[List[str]] = None,
    ) -> TestDiscoveryResult:
        """Inspect snapshot filesystem and return explicit test discovery result.

        Guarantees:
        - Python: inspects tests/ directories and test_*.py files; distinguishes empty suites.
        - Node.js: inspects package.json scripts.test; rejects missing scripts or default npm errors.
        - Never reports DISCOVERED when zero test files or scripts exist.
        """
        root = Path(snapshot_dir).resolve()
        if not root.is_dir():
            return TestDiscoveryResult(
                status=TestDiscoveryStatus.DISCOVERY_ERROR,
                framework="none",
                description=f"Snapshot directory does not exist: {root}",
            )

        if custom_cmd:
            return TestDiscoveryResult(
                status=TestDiscoveryStatus.DISCOVERED,
                framework="custom",
                test_command=custom_cmd,
                description=f"User-specified test command: {' '.join(custom_cmd)}",
            )

        eco_clean = ecosystem.strip().lower()

        # 1. Node.js Ecosystem
        if "node" in eco_clean or "npm" in eco_clean or (root / "package.json").exists():
            pkg_json_file = root / "package.json"
            if not pkg_json_file.exists():
                return TestDiscoveryResult(
                    status=TestDiscoveryStatus.NO_TESTS_FOUND,
                    framework="none",
                    description="No package.json manifest found in snapshot.",
                )

            try:
                data = json.loads(pkg_json_file.read_text(encoding="utf-8"))
                scripts = data.get("scripts", {})
                test_script = scripts.get("test", "").strip()

                if not test_script:
                    return TestDiscoveryResult(
                        status=TestDiscoveryStatus.NO_TESTS_FOUND,
                        framework="none",
                        description="package.json does not define a 'scripts.test' entry.",
                    )

                # Check for standard npm init placeholder:
                # "echo \"Error: no test specified\" && exit 1"
                if "no test specified" in test_script.lower() or "exit 1" in test_script:
                    return TestDiscoveryResult(
                        status=TestDiscoveryStatus.NO_TESTS_FOUND,
                        framework="none",
                        description="Default npm placeholder detected ('no test specified'); no runnable test suite.",
                    )

                return TestDiscoveryResult(
                    status=TestDiscoveryStatus.DISCOVERED,
                    framework="npm_test",
                    test_command=["npm", "test"],
                    description=f"Found package.json test script: '{test_script}'.",
                )
            except Exception as e:
                return TestDiscoveryResult(
                    status=TestDiscoveryStatus.DISCOVERY_ERROR,
                    framework="none",
                    description=f"Error parsing package.json for test discovery: {e}",
                )

        # 2. Python Ecosystem
        discovered_files: List[str] = []

        # Check for tests/ directory
        tests_dir = root / "tests"
        if tests_dir.is_dir():
            for p in tests_dir.rglob("*.py"):
                if p.is_file() and not p.name.startswith("."):
                    discovered_files.append(str(p.relative_to(root)))

        # Check for root-level test_*.py or *_test.py
        for p in root.glob("test_*.py"):
            if p.is_file() and str(p.relative_to(root)) not in discovered_files:
                discovered_files.append(str(p.relative_to(root)))
        for p in root.glob("*_test.py"):
            if p.is_file() and str(p.relative_to(root)) not in discovered_files:
                discovered_files.append(str(p.relative_to(root)))

        if not discovered_files:
            return TestDiscoveryResult(
                status=TestDiscoveryStatus.NO_TESTS_FOUND,
                framework="none",
                test_files=[],
                test_command=[],
                description="Zero Python test files discovered (checked tests/ directory and test_*.py patterns).",
            )

        return TestDiscoveryResult(
            status=TestDiscoveryStatus.DISCOVERED,
            framework="pytest",
            test_files=discovered_files,
            test_command=["pytest", "-q"],
            description=f"Discovered {len(discovered_files)} Python test file(s) for pytest execution.",
        )
