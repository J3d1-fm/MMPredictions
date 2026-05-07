"""Small Cloud Storage helper for pROAS snapshots and artifacts.

Uses only the stdlib so the dashboard image stays dependency-light.
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
GCS_UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
GCS_MEDIA_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}?alt=media"
GCS_OBJECT_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}"


class GCSUnavailable(RuntimeError):
    """Raised when Cloud Storage is configured but unavailable."""


class GCSPreconditionFailed(RuntimeError):
    """Raised when an object generation precondition rejects an upload."""


class GCSSnapshotRegression(RuntimeError):
    """Raised when a new snapshot would discard existing cohort history."""


def bucket_name(config: dict[str, Any]) -> str | None:
    storage = config.get("storage", {})
    return os.environ.get("MMPRED_GCS_BUCKET") or storage.get("gcs_bucket")


def prefix(config: dict[str, Any]) -> str:
    if config.get("_gcs_prefix_override"):
        return str(config["_gcs_prefix_override"]).strip("/")
    storage = config.get("storage", {})
    raw = os.environ.get("MMPRED_GCS_PREFIX") or storage.get("gcs_prefix") or "mmpredictions"
    return str(raw).strip("/")


def enabled(config: dict[str, Any]) -> bool:
    return bool(bucket_name(config))


def object_name(config: dict[str, Any], relative: str) -> str:
    base = prefix(config)
    return f"{base}/{relative.lstrip('/')}" if base else relative.lstrip("/")


def access_token() -> str:
    request = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload["access_token"])


def request_headers(content_type: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token()}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def object_metadata(config: dict[str, Any], relative: str) -> dict[str, Any] | None:
    bucket = bucket_name(config)
    if not bucket:
        return None
    name = urllib.parse.quote(object_name(config, relative), safe="")
    url = GCS_OBJECT_URL.format(bucket=bucket, name=name)
    try:
        request = urllib.request.Request(url, headers=request_headers())
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        text = exc.read().decode("utf-8", errors="replace")[:500]
        raise GCSUnavailable(f"Cloud Storage metadata failed for {relative}: HTTP {exc.code} {text}") from exc
    except urllib.error.URLError as exc:
        raise GCSUnavailable(f"Cloud Storage metadata unavailable for {relative}: {exc}") from exc


def download_bytes(config: dict[str, Any], relative: str) -> bytes | None:
    bucket = bucket_name(config)
    if not bucket:
        return None
    name = urllib.parse.quote(object_name(config, relative), safe="")
    url = GCS_MEDIA_URL.format(bucket=bucket, name=name)
    try:
        request = urllib.request.Request(url, headers=request_headers())
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        text = exc.read().decode("utf-8", errors="replace")[:500]
        raise GCSUnavailable(f"Cloud Storage download failed for {relative}: HTTP {exc.code} {text}") from exc
    except urllib.error.URLError as exc:
        raise GCSUnavailable(f"Cloud Storage download unavailable for {relative}: {exc}") from exc


def upload_bytes(
    config: dict[str, Any],
    relative: str,
    payload: bytes,
    content_type: str,
    if_generation_match: int | None = None,
) -> None:
    bucket = bucket_name(config)
    if not bucket:
        return
    params_dict: dict[str, str | int] = {"uploadType": "media", "name": object_name(config, relative)}
    if if_generation_match is not None:
        params_dict["ifGenerationMatch"] = if_generation_match
    params = urllib.parse.urlencode(params_dict)
    url = GCS_UPLOAD_URL.format(bucket=bucket) + f"?{params}"
    try:
        request = urllib.request.Request(url, data=payload, headers=request_headers(content_type), method="POST")
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise GCSPreconditionFailed(f"Cloud Storage generation changed for {relative}") from exc
        text = exc.read().decode("utf-8", errors="replace")[:500]
        raise GCSUnavailable(f"Cloud Storage upload failed for {relative}: HTTP {exc.code} {text}") from exc
    except urllib.error.URLError as exc:
        raise GCSUnavailable(f"Cloud Storage upload unavailable for {relative}: {exc}") from exc


def download_gzip_json(config: dict[str, Any], relative: str) -> dict[str, Any] | None:
    payload = download_bytes(config, relative)
    if payload is None:
        return None
    return json.loads(gzip.decompress(payload).decode("utf-8"))


def upload_gzip_json(config: dict[str, Any], relative: str, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    upload_bytes(config, relative, gzip.compress(encoded, compresslevel=6), "application/gzip")


def restore_sqlite_snapshot(config: dict[str, Any], db_path: pathlib.Path) -> bool:
    if not enabled(config) or db_path.exists():
        return False
    payload = download_bytes(config, "snapshots/latest.sqlite3.gz")
    if payload is None:
        return False
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(db_path.parent), delete=False) as handle:
        temp_path = pathlib.Path(handle.name)
        try:
            handle.write(gzip.decompress(payload))
        except Exception as exc:  # noqa: BLE001 - fail closed; an empty DB would corrupt latest.
            temp_path.unlink(missing_ok=True)
            raise GCSUnavailable("Cloud Storage SQLite snapshot is not a valid gzip payload") from exc
    temp_path.replace(db_path)
    return True


def validate_snapshot_regression(config: dict[str, Any], manifest: dict[str, Any]) -> None:
    latest = download_gzip_json(config, "snapshots/latest.manifest.json.gz")
    if not latest:
        return
    for key in ("row_count", "weekly_count"):
        latest_count = int(latest.get(key) or 0)
        new_count = int(manifest.get(key) or 0)
        if latest_count > 0 and new_count < latest_count:
            raise GCSSnapshotRegression(
                f"refusing to overwrite snapshot: {key} would drop from {latest_count} to {new_count}"
            )


def upload_sqlite_snapshot(config: dict[str, Any], snapshot_path: pathlib.Path, manifest: dict[str, Any]) -> None:
    if not enabled(config):
        return
    validate_snapshot_regression(config, manifest)
    metadata = object_metadata(config, "snapshots/latest.sqlite3.gz")
    generation = int(metadata["generation"]) if metadata else 0
    upload_bytes(
        config,
        "snapshots/latest.sqlite3.gz",
        gzip.compress(snapshot_path.read_bytes(), compresslevel=6),
        "application/gzip",
        if_generation_match=generation,
    )
    upload_gzip_json(config, "snapshots/latest.manifest.json.gz", manifest)
