"""Shared helpers for the M12 supervisor tests.

Recorded data only (see tests/fixtures/resolution/CAPTURE.json and capture_fixtures.py): real pip
reports and logs, and real, trimmed PyPI responses. ``FakePyPI`` serves those bytes through the
``http_get`` seam of the real ``PyPIMetadataClient``, so URL checks, size limits, parsing and every
verification step run unchanged. Nothing here proves Docker isolation or a live resolver.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.resolution.fingerprint import resolution_environment
from src.resolution.metadata_client import Deadline, MetadataFetchError, PyPIMetadataClient
from src.resolution.models import ResolutionRole
from src.resolution.python_input import classify_requirements
from src.resolution.target import marker_environment
from src.resolution.verifier import ResolutionContext

FIX = Path(__file__).resolve().parent / "fixtures" / "resolution"
CAPTURE = json.loads((FIX / "CAPTURE.json").read_text())
RETRIEVED_AT = "2026-11-12T18:00:00Z"
ENV = resolution_environment("amd64")  # fixtures were captured for x86_64


class FakePyPI:
    """Serves recorded PyPI responses by URL. Unknown URLs answer 404."""

    def __init__(self) -> None:
        self.overrides: Dict[str, Tuple[int, bytes]] = {}
        self.errors: Dict[str, MetadataFetchError] = {}
        self.calls: List[Tuple[str, float, int]] = []

    def body(self, url: str) -> Optional[bytes]:
        if url.startswith("https://pypi.org/simple/"):
            path = FIX / "pypi" / f"simple_{url.rstrip('/').rsplit('/', 1)[1]}.json"
        elif url.startswith("https://files.pythonhosted.org/") and url.endswith(".metadata"):
            path = FIX / "pypi" / url.rsplit("/", 1)[1]
        elif url.startswith("https://pypi.org/pypi/"):
            _, _, _, _, name, version, _ = url.split("/")
            path = FIX / "pypi" / f"json_{name}_{version}.json"
        else:
            return None
        return path.read_bytes() if path.exists() else None

    def __call__(self, url: str, headers: Dict[str, str], timeout: float, max_bytes: int) -> Tuple[int, bytes]:
        self.calls.append((url, timeout, max_bytes))
        if url in self.errors:
            raise self.errors[url]
        if url in self.overrides:
            return self.overrides[url]
        body = self.body(url)
        return (200, body) if body is not None else (404, b"")

    # Helpers to derive modified index data in tests (each modification is explicit in the test).
    def index(self, project: str) -> dict:
        return json.loads(self.body(f"https://pypi.org/simple/{project}/"))

    def set_index(self, project: str, data: dict) -> None:
        self.overrides[f"https://pypi.org/simple/{project}/"] = (200, json.dumps(data).encode())

    def edit_index_file(self, project: str, filename: str, edit: Callable[[dict], None]) -> None:
        data = self.index(project)
        for entry in data["files"]:
            if entry["filename"] == filename:
                edit(entry)
        self.set_index(project, data)


def client(fake: Optional[FakePyPI] = None) -> PyPIMetadataClient:
    return PyPIMetadataClient(http_get=fake or FakePyPI(), now=lambda: RETRIEVED_AT)


def deadline() -> Deadline:
    return Deadline()


def report_json(run: str) -> dict:
    """A recorded report with its host 'environment' block replaced by the trusted target's.

    pip ran on the capture host with target emulation, so it recorded the host environment. The
    install list is never modified here.
    """
    data = json.loads((FIX / "reports" / f"{run}.stdout.json").read_bytes())
    data["environment"] = {**marker_environment(ENV), "platform_release": "6.0", "platform_version": "#1 SMP"}
    return data


def report_bytes(run: str, mutate: Optional[Callable[[dict], None]] = None) -> bytes:
    data = report_json(run)
    if mutate:
        mutate(data)
    return json.dumps(data).encode()


def stderr(run: str) -> str:
    return (FIX / "reports" / f"{run}.stderr.txt").read_text()


def manifest_text(run: str) -> str:
    return "".join(r + "\n" for r in CAPTURE["runs"][run]["requirements"])


def context(content: str, role: ResolutionRole = ResolutionRole.PROPOSED, proposal_id: str = "prop_1",
            env=ENV, original: Optional[str] = None, diff: str = "--- a\n+++ b\n",
            workspace_dir: Optional[str] = None, manifest_path: Optional[str] = None) -> ResolutionContext:
    manifest = classify_requirements(content, manifest_path=manifest_path, workspace_dir=workspace_dir)
    if ResolutionRole(role) == ResolutionRole.ORIGINAL:
        return ResolutionContext(proposal_id=proposal_id, role=role, env=env, manifest=manifest,
                                 original_content=content)
    return ResolutionContext(proposal_id=proposal_id, role=role, env=env, manifest=manifest,
                             original_content=original if original is not None else content,
                             proposed_content=content, unified_diff=diff)


def item(data: dict, name: str) -> dict:
    return next(i for i in data["install"] if i["metadata"]["name"].lower() == name.lower())


def clone(data: dict) -> dict:
    return copy.deepcopy(data)
