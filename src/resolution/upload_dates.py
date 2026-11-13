"""FR-32c upload dates: the upload time of the exact selected file, or an explicit 'date unavailable'.

Rule (ADR-011): the selected file is identified by filename AND SHA-256.
1. Source A: the PEP 691 JSON index entry for that file (already matched by the verifier); its PEP 700
   ``upload-time`` if present and parseable.
2. Source B: the per-version JSON API; the single ``urls[]`` entry whose ``filename`` and
   ``digests.sha256`` both match; its ``upload_time_iso_8601``.
3. Otherwise unavailable, with a reason. Other files of the same release are never consulted, so the
   earliest or latest file of a release can never stand in. Timestamps without an explicit offset are
   ambiguous and rejected; offsets are converted to UTC.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.resolution.metadata_client import (
    Deadline,
    IndexFile,
    MetadataFetchError,
    ProjectIndex,
    PyPIMetadataClient,
    VersionJson,
)
from src.resolution.models import UploadDate

SOURCE_INDEX = "pep700-simple-json"
SOURCE_JSON_API = "pypi-json-api"
_RFC3339 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|z|[+-]\d{2}:\d{2})$"
)


def normalize_utc(value: object) -> Optional[str]:
    """Return an RFC 3339 UTC timestamp ending in 'Z', or None if the value is not unambiguous."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    m = _RFC3339.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, fraction, offset = m.groups()
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    if offset in ("Z", "z"):
        tz = timezone.utc
    else:
        sign = 1 if offset[0] == "+" else -1
        oh, om = int(offset[1:3]), int(offset[4:6])
        if oh > 23 or om > 59:
            return None
        tz = timezone(sign * timedelta(hours=oh, minutes=om))
    try:
        dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=tz)
    except ValueError:
        return None
    dt = dt.astimezone(timezone.utc)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        text += f".{dt.microsecond:06d}"
    return text + "Z"


def _unavailable(filename: str, reason: str, source: Optional[str] = None, endpoint: Optional[str] = None,
                 retrieved_at: Optional[str] = None) -> UploadDate:
    return UploadDate(filename=filename, available=False, reason=reason, source=source, endpoint=endpoint,
                      retrieved_at=retrieved_at)


def date_from_index(entry: IndexFile, index: ProjectIndex) -> UploadDate:
    if entry.upload_time is None:
        return _unavailable(entry.filename, "field_missing", SOURCE_INDEX, index.endpoint, index.retrieved_at)
    value = normalize_utc(entry.upload_time)
    if value is None:
        return _unavailable(entry.filename, "unparseable", SOURCE_INDEX, index.endpoint, index.retrieved_at)
    return UploadDate(filename=entry.filename, value=value, available=True, source=SOURCE_INDEX,
                      endpoint=index.endpoint, retrieved_at=index.retrieved_at)


def date_from_json_api(vj: VersionJson, filename: str, sha256: str) -> UploadDate:
    def fail(reason: str) -> UploadDate:
        return _unavailable(filename, reason, SOURCE_JSON_API, vj.endpoint, vj.retrieved_at)

    matches = []
    for entry in vj.data.get("urls", []):
        if not isinstance(entry, dict) or entry.get("filename") != filename:
            continue
        digests = entry.get("digests")
        if isinstance(digests, dict) and isinstance(digests.get("sha256"), str) and digests["sha256"].lower() == sha256:
            matches.append(entry)
    if not matches:
        return fail("file_not_matched")
    if len(matches) > 1:
        return fail("duplicate_file_records")
    raw = matches[0].get("upload_time_iso_8601")
    if raw is None:
        return fail("field_missing")
    value = normalize_utc(raw)
    if value is None:
        return fail("unparseable")
    return UploadDate(filename=filename, value=value, available=True, source=SOURCE_JSON_API,
                      endpoint=vj.endpoint, retrieved_at=vj.retrieved_at)


def resolve_upload_date(
    client: PyPIMetadataClient,
    index: ProjectIndex,
    entry: IndexFile,
    name: str,
    version: str,
    sha256: str,
    deadline: Deadline,
) -> UploadDate:
    """Upload date of the selected file. Never raises for missing data; DeadlineExceeded propagates."""
    first = date_from_index(entry, index)
    if first.available:
        return first
    try:
        vj = client.version_json(name, version, deadline)
    except MetadataFetchError:
        return _unavailable(entry.filename, "fetch_failed", SOURCE_JSON_API)
    return date_from_json_api(vj, entry.filename, sha256)
