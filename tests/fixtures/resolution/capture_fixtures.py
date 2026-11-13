"""Re-capture the M12 supervisor test fixtures from real pip runs and real PyPI responses.

Not run by the test suite. Usage (network required, runs no package code):

    python3 tests/fixtures/resolution/capture_fixtures.py

What it does:
- Runs the host's pip with the resolver policy arguments plus target-emulation options
  (``--python-version 3.11.18 --implementation cp --abi cp311 --platform manylinux..._x86_64``) and
  ``--report <file>`` with logs captured, exactly as the M12 resolver contract expects
  (report on stdout, pip output on stderr). ``--dry-run --only-binary=:all:`` installs and builds nothing.
- Fetches the PEP 691 JSON index for every involved project, keeps only the files of the versions a
  test needs (documented per project below), and fetches the PEP 658 ``.metadata`` file of every
  selected wheel, checking it against the index-advertised hash.
- Host pip evaluates environment markers against the capturing host, and records the host in the
  report's ``environment`` block. The tests replace that block with the trusted target environment;
  the ``install`` list is never modified.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename

HERE = Path(__file__).resolve().parent
ARCH = "x86_64"
TARGET_OPTS = [
    "--python-version", "3.11.18", "--implementation", "cp", "--abi", "cp311",
    *[x for p in (f"manylinux_2_41_{ARCH}", f"manylinux_2_28_{ARCH}", f"manylinux_2_17_{ARCH}",
                  f"manylinux2014_{ARCH}", f"manylinux_2_5_{ARCH}", f"manylinux1_{ARCH}", f"linux_{ARCH}")
      for x in ("--platform", p)],
]
POLICY = ["install", "--dry-run", "--ignore-installed", "--only-binary=:all:", "--isolated", "--no-input",
          "--disable-pip-version-check", "--index-url", "https://pypi.org/simple"]

# name -> requirement lines
RUNS = {
    "flask_original": ["Flask==2.0.3"],
    "flask_proposed": ["Flask==3.0.3"],
    "requests_socks": ["requests[socks]==2.32.3"],
    "numpy_scipy_pandas": ["numpy==1.24.0", "scipy==1.10.0", "pandas==1.5.3"],
    "conflict_numpy_scipy": ["numpy==2.1.0", "scipy==1.10.0"],
    "sdist_only": ["pycrypto==2.6.1"],
    "requires_python": ["Django==6.0"],
}
# Extra index versions kept for failure analysis (beyond the selected ones).
EXTRA_VERSIONS = {
    "numpy": {"2.1.0"},
    "scipy": {"1.10.0"},
    "pycrypto": None,  # None = keep every file
    "django": {"6.0"},
}
# Metadata of non-selected files needed by forgery tests (a report claiming the real conflict resolved).
EXTRA_METADATA = {"numpy": {"numpy-2.1.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"}}
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "dua-fixture-capture"


def run_pip(name: str, reqs: list[str]) -> dict:
    tmp = HERE / "_tmp"
    tmp.mkdir(exist_ok=True)
    report = tmp / f"{name}.json"
    report.unlink(missing_ok=True)
    cmd = [sys.executable, "-m", "pip", *POLICY, *TARGET_OPTS, "--target", str(tmp / "t"),
           "--report", str(report), *reqs]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    stdout = report.read_bytes() if proc.returncode == 0 and report.exists() else b""
    (HERE / "reports" / f"{name}.stdout.json").write_bytes(stdout)
    # Contract: pip's own output (stdout and stderr) arrives on the resolver's stderr.
    (HERE / "reports" / f"{name}.stderr.txt").write_text(proc.stdout + proc.stderr)
    return {"exit_code": proc.returncode, "requirements": reqs, "command": " ".join(cmd[1:]).replace(str(tmp), "<tmp>")}


def fetch(url: str, accept: str | None = None) -> bytes:
    r = SESSION.get(url, headers={"Accept": accept} if accept else {}, timeout=30, allow_redirects=False)
    r.raise_for_status()
    return r.content


def version_of(filename: str) -> str | None:
    """Version of a wheel or sdist; None for legacy formats pip cannot install (.egg, .exe, ...)."""
    try:
        if filename.endswith(".whl"):
            return str(parse_wheel_filename(filename)[1])
        return str(parse_sdist_filename(filename)[1])
    except ValueError:
        return None


def main() -> None:
    (HERE / "reports").mkdir(exist_ok=True)
    (HERE / "pypi").mkdir(exist_ok=True)
    runs = {name: run_pip(name, reqs) for name, reqs in RUNS.items()}
    keep: dict[str, set | None] = {k: (set(v) if v else None) for k, v in EXTRA_VERSIONS.items()}
    selected_files: dict[str, set] = {}
    for name in RUNS:
        raw = (HERE / "reports" / f"{name}.stdout.json").read_bytes()
        if not raw:
            continue
        for item in json.loads(raw)["install"]:
            proj = canonicalize_name(item["metadata"]["name"])
            if keep.get(proj, set()) is not None:
                keep.setdefault(proj, set()).add(item["metadata"]["version"])
            selected_files.setdefault(proj, set()).add(urlsplit(item["download_info"]["url"]).path.rsplit("/", 1)[1])
    for proj, versions in sorted(keep.items()):
        data = json.loads(fetch(f"https://pypi.org/simple/{proj}/", "application/vnd.pypi.simple.v1+json"))
        files = [f for f in data["files"] if versions is None or version_of(f["filename"]) in versions]
        data["files"] = files
        if "versions" in data and versions is not None:
            data["versions"] = [v for v in data["versions"] if v in versions]
        (HERE / "pypi" / f"simple_{proj}.json").write_text(json.dumps(data, indent=1, sort_keys=True))
        for f in files:
            if f["filename"] not in selected_files.get(proj, set()) | EXTRA_METADATA.get(proj, set()):
                continue
            meta = f.get("core-metadata") or f.get("data-dist-info-metadata")
            if isinstance(meta, dict) and "sha256" in meta:
                body = fetch(f["url"] + ".metadata")
                assert hashlib.sha256(body).hexdigest() == meta["sha256"], f["filename"]
                (HERE / "pypi" / f"{f['filename']}.metadata").write_bytes(body)
    # One per-version JSON API response, for the fallback tests.
    vj = json.loads(fetch("https://pypi.org/pypi/blinker/1.9.0/json"))
    vj = {"info": {k: vj["info"].get(k) for k in ("name", "version", "requires_dist", "requires_python")},
          "urls": [{k: u.get(k) for k in ("filename", "digests", "upload_time_iso_8601", "url", "packagetype")}
                   for u in vj["urls"]]}
    (HERE / "pypi" / "json_blinker_1.9.0.json").write_text(json.dumps(vj, indent=1, sort_keys=True))
    pip_version = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True).stdout
    (HERE / "CAPTURE.json").write_text(json.dumps(
        {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pip": pip_version.split(" from ")[0],
         "target_arch": ARCH, "runs": runs}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
