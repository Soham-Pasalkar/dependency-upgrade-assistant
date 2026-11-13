"""FR-32c upload dates of the exact selected file (ADR-011); M12 criterion 14(c) unit part.

Recorded PyPI responses through the real client. Variants of a recorded response are built in the test
that uses them, so each modification is visible.
"""
from __future__ import annotations

import json

import pytest

from src.resolution.metadata_client import Deadline, DeadlineExceeded, IndexFile, MetadataFetchError, ProjectIndex
from src.resolution.upload_dates import (
    SOURCE_INDEX,
    SOURCE_JSON_API,
    date_from_index,
    normalize_utc,
    resolve_upload_date,
)
from tests.resolution_fixtures import RETRIEVED_AT, FakePyPI, client

BLINKER = "blinker-1.9.0-py3-none-any.whl"
JSON_URL = "https://pypi.org/pypi/blinker/1.9.0/json"


def blinker(fake):
    c = client(fake)
    index = c.project_index("blinker", Deadline())
    entry = index.entries_named(BLINKER)[0]
    return c, index, entry


def resolve(fake, entry=None):
    c, index, real_entry = blinker(fake)
    entry = entry or real_entry
    return resolve_upload_date(c, index, entry, "blinker", "1.9.0", entry.sha256, Deadline())


def json_api(fake):
    return json.loads(fake.body(JSON_URL))


def set_json(fake, data):
    fake.overrides[JSON_URL] = (200, json.dumps(data).encode())


def test_exact_file_from_pep700_index():
    fake = FakePyPI()
    date = resolve(fake)
    raw = next(f["upload-time"] for f in fake.index("blinker")["files"] if f["filename"] == BLINKER)
    assert date.available and date.source == SOURCE_INDEX and date.filename == BLINKER
    assert date.value == normalize_utc(raw) and date.value.endswith("Z")
    assert date.endpoint == "https://pypi.org/simple/blinker/" and date.retrieved_at == RETRIEVED_AT
    assert not any(url == JSON_URL for url, *_ in fake.calls)  # no fallback needed


def test_multi_file_release_uses_only_the_selected_file():
    fake = FakePyPI()
    files = [f for f in fake.index("charset-normalizer")["files"] if "3.5.2" in f["filename"]]
    assert len(files) > 1
    times = sorted(normalize_utc(f["upload-time"]) for f in files)
    selected = files[len(files) // 2]
    c = client(fake)
    index = c.project_index("charset-normalizer", Deadline())
    entry = index.entries_named(selected["filename"])[0]
    date = resolve_upload_date(c, index, entry, "charset-normalizer", "3.5.2", entry.sha256, Deadline())
    assert date.value == normalize_utc(selected["upload-time"]) and date.filename == selected["filename"]


def test_no_earliest_or_latest_substitution():
    """Selected file in the middle of a release whose files were uploaded at different times."""
    fake = FakePyPI()
    data = fake.index("blinker")
    base = next(f for f in data["files"] if f["filename"] == BLINKER)
    early = {**base, "filename": "blinker-1.9.0.tar.gz", "url": base["url"].replace(BLINKER, "blinker-1.9.0.tar.gz"),
             "hashes": {"sha256": "1" * 64}, "upload-time": "2020-01-01T00:00:00Z"}
    late = {**base, "filename": "blinker-1.9.0-py2.py3-none-any.whl",
            "url": base["url"].replace(BLINKER, "blinker-1.9.0-py2.py3-none-any.whl"),
            "hashes": {"sha256": "2" * 64}, "upload-time": "2030-01-01T00:00:00Z"}
    data["files"] = [early, {**base, "upload-time": "2025-06-01T12:00:00Z"}, late]
    fake.set_index("blinker", data)
    assert resolve(fake).value == "2025-06-01T12:00:00Z"


def test_missing_index_field_falls_back_to_json_api():
    fake = FakePyPI()
    fake.edit_index_file("blinker", BLINKER, lambda e: e.pop("upload-time"))
    date = resolve(fake)
    expected = next(u["upload_time_iso_8601"] for u in json_api(fake)["urls"] if u["filename"] == BLINKER)
    assert date.available and date.source == SOURCE_JSON_API and date.endpoint == JSON_URL
    assert date.value == normalize_utc(expected) and date.retrieved_at == RETRIEVED_AT


def test_unparseable_index_field_falls_back_to_json_api():
    fake = FakePyPI()
    fake.edit_index_file("blinker", BLINKER, lambda e: e.__setitem__("upload-time", "yesterday"))
    assert resolve(fake).source == SOURCE_JSON_API


def _no_index_time(fake):
    fake.edit_index_file("blinker", BLINKER, lambda e: e.pop("upload-time"))


def test_missing_in_both_sources():
    fake = FakePyPI()
    _no_index_time(fake)
    data = json_api(fake)
    for u in data["urls"]:
        u.pop("upload_time_iso_8601")
    set_json(fake, data)
    date = resolve(fake)
    assert (date.available, date.reason, date.value, date.display) == (False, "field_missing", None, "date unavailable")


def test_json_api_never_uses_another_file():
    fake = FakePyPI()
    _no_index_time(fake)
    data = json_api(fake)
    data["urls"] = [u for u in data["urls"] if u["filename"] != BLINKER]
    set_json(fake, data)
    date = resolve(fake)
    assert (date.available, date.reason) == (False, "file_not_matched")


def test_json_api_filename_match_with_different_hash_is_not_used():
    fake = FakePyPI()
    _no_index_time(fake)
    data = json_api(fake)
    for u in data["urls"]:
        u["digests"]["sha256"] = "0" * 64
    set_json(fake, data)
    assert resolve(fake).reason == "file_not_matched"


def test_duplicate_file_records_are_ambiguous():
    fake = FakePyPI()
    _no_index_time(fake)
    data = json_api(fake)
    match = next(u for u in data["urls"] if u["filename"] == BLINKER)
    data["urls"].append({**match, "upload_time_iso_8601": "2001-01-01T00:00:00Z"})
    set_json(fake, data)
    assert resolve(fake).reason == "duplicate_file_records"


@pytest.mark.parametrize("bad", ["2024-13-01T00:00:00Z", "2024-12-04T17:35:28", "2024-12-04", "", 1733333333])
def test_unparseable_or_ambiguous_timestamps(bad):
    fake = FakePyPI()
    _no_index_time(fake)
    data = json_api(fake)
    for u in data["urls"]:
        u["upload_time_iso_8601"] = bad
    set_json(fake, data)
    assert resolve(fake).reason == "unparseable"


@pytest.mark.parametrize("status_or_error", [503, 404, MetadataFetchError("timeout", "slow"),
                                             MetadataFetchError("too_large", "big")])
def test_unavailable_registry_gives_date_unavailable(status_or_error):
    fake = FakePyPI()
    _no_index_time(fake)
    if isinstance(status_or_error, int):
        fake.overrides[JSON_URL] = (status_or_error, b"")
    else:
        fake.errors[JSON_URL] = status_or_error
    date = resolve(fake)
    assert (date.available, date.reason, date.filename) == (False, "fetch_failed", BLINKER)


def test_malformed_json_api_response():
    fake = FakePyPI()
    _no_index_time(fake)
    fake.overrides[JSON_URL] = (200, b"{not json")
    assert resolve(fake).reason == "fetch_failed"


def test_oversized_json_api_response():
    fake = FakePyPI()
    _no_index_time(fake)
    fake.overrides[JSON_URL] = (200, b" " * (16 * 1024 * 1024 + 1))
    assert resolve(fake).reason == "fetch_failed"


def test_deadline_propagates_instead_of_a_date():
    fake = FakePyPI()
    _no_index_time(fake)
    c, index, entry = blinker(fake)
    with pytest.raises(DeadlineExceeded):
        resolve_upload_date(c, index, entry, "blinker", "1.9.0", entry.sha256, Deadline(at=0.0, clock=lambda: 1.0))


@pytest.mark.parametrize("raw,expected", [
    ("2024-12-04T17:35:28.174320Z", "2024-12-04T17:35:28.174320Z"),
    ("2024-12-04T19:35:28.174320+02:00", "2024-12-04T17:35:28.174320Z"),
    ("2024-12-04T12:35:28-05:00", "2024-12-04T17:35:28Z"),
    ("2024-12-04t17:35:28.1z", "2024-12-04T17:35:28.100000Z"),
    ("2024-12-04T17:35:28.123456789Z", "2024-12-04T17:35:28.123456Z"),
    ("2024-12-31T23:30:00-01:00", "2025-01-01T00:30:00Z"),
    ("2024-12-04T17:35:28", None),
    ("2024-12-04T17:35:28+25:00", None),
    ("not a date", None),
    (None, None),
])
def test_normalize_utc(raw, expected):
    assert normalize_utc(raw) == expected


def test_index_entry_without_time_reports_field_missing():
    index = ProjectIndex(name="x", files=(), endpoint="https://pypi.org/simple/x/", retrieved_at=RETRIEVED_AT)
    entry = IndexFile("x-1-py3-none-any.whl", "https://files.pythonhosted.org/x", "0" * 64, None, False, None, None,
                      None)
    date = date_from_index(entry, index)
    assert (date.available, date.reason, date.source) == (False, "field_missing", SOURCE_INDEX)
