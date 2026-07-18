#!/usr/bin/env python3
"""Fetch previous immutable snapshot metadata and fail closed on GitHub errors."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

DATABASES = ("bytecode.cvd", "daily.cvd", "main.cvd")
LEGACY_TAG = "latest"
LEGACY_ASSETS = {
    "bytecode.cvd",
    "daily.cvd",
    "LAST_RUN.txt",
    "MD5SUMS",
    "SHA256SUMS",
}


class FetchError(RuntimeError):
    pass


def request_bytes(url: str, *, token: str | None, accept: str) -> bytes:
    headers = {
        "Accept": accept,
        "User-Agent": "clamav-mirror-snapshot-fetcher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"GitHub request failed with HTTP {exc.code}: {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"GitHub transport failed for {url}: {exc}") from exc


def parse_json_object(raw: bytes, label: str) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FetchError(f"{label} must be a JSON object")
    return value


def validate_snapshot(snapshot: dict) -> None:
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise FetchError(f"invalid previous schema_version: {snapshot.get('schema_version')!r}")
    databases = snapshot.get("databases")
    if not isinstance(databases, dict) or set(databases) != set(DATABASES):
        raise FetchError("previous snapshot database set is invalid")
    for name in DATABASES:
        info = databases.get(name)
        if not isinstance(info, dict) or type(info.get("version")) is not int:
            raise FetchError(f"invalid previous version for {name}")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def fetch_previous_snapshot(repo: str, output: Path, api_url: str, token: str | None) -> bool:
    latest_url = f"{api_url.rstrip('/')}/repos/{repo}/releases/latest"
    release = parse_json_object(
        request_bytes(latest_url, token=token, accept="application/vnd.github+json"),
        "latest release metadata",
    )
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise FetchError("latest release must be public and non-prerelease")

    assets = release.get("assets")
    if not isinstance(assets, list) or not all(isinstance(item, dict) for item in assets):
        raise FetchError("latest release assets metadata is invalid")
    names = [item.get("name") for item in assets]
    if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)):
        raise FetchError("latest release asset names are invalid or duplicated")

    snapshot_assets = [item for item in assets if item.get("name") == "SNAPSHOT.json"]
    if snapshot_assets:
        if len(snapshot_assets) != 1 or not isinstance(snapshot_assets[0].get("url"), str):
            raise FetchError("SNAPSHOT.json asset metadata is invalid")
        raw_snapshot = request_bytes(
            snapshot_assets[0]["url"],
            token=token,
            accept="application/octet-stream",
        )
        validate_snapshot(parse_json_object(raw_snapshot, "previous SNAPSHOT.json"))
        atomic_write(output, raw_snapshot)
        return True

    if release.get("tag_name") == LEGACY_TAG and set(names) == LEGACY_ASSETS:
        output.unlink(missing_ok=True)
        return False

    raise FetchError(
        "SNAPSHOT.json is absent and latest release does not match the approved legacy migration fingerprint"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--api-url", default="https://api.github.com")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        found = fetch_previous_snapshot(
            args.repo, args.output, args.api_url, os.environ.get("GH_TOKEN")
        )
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("previous snapshot fetched" if found else "confirmed current legacy latest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
