#!/usr/bin/env python3
"""Publish a verified ClamAV snapshot as an immutable GitHub Release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DATABASES = ("bytecode.cvd", "daily.cvd", "main.cvd")
ASSET_NAMES = (*DATABASES, "MD5SUMS", "SHA256SUMS", "SNAPSHOT.json")


class PublishError(RuntimeError):
    pass


def digest(path: Path, algorithm: str = "sha256") -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def command(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise PublishError(f"command failed ({result.returncode}): {' '.join(args[:4])}: {detail}")
    return result


def parse_manifest(path: Path, algorithm: str) -> dict[str, str]:
    expected_length = hashlib.new(algorithm).digest_size * 2
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PublishError(f"cannot read {path.name}: {exc}") from exc
    for line in lines:
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise PublishError(f"invalid {path.name} line: {line!r}")
        checksum, name = parts
        if not re.fullmatch(rf"[0-9a-f]{{{expected_length}}}", checksum):
            raise PublishError(f"invalid {algorithm} digest for {name}")
        if name in result:
            raise PublishError(f"duplicate {name} in {path.name}")
        result[name] = checksum
    if set(result) != set(DATABASES):
        raise PublishError(f"invalid database set in {path.name}: {sorted(result)}")
    return result


def load_snapshot(snapshot_dir: Path) -> dict:
    actual_names = {path.name for path in snapshot_dir.iterdir() if path.is_file()}
    if actual_names != set(ASSET_NAMES):
        raise PublishError(
            f"local asset set mismatch: expected={sorted(ASSET_NAMES)} actual={sorted(actual_names)}"
        )
    try:
        snapshot = json.loads((snapshot_dir / "SNAPSHOT.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"invalid SNAPSHOT.json: {exc}") from exc
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise PublishError("invalid SNAPSHOT.json schema_version")

    snapshot_id = snapshot.get("snapshot_id")
    tag = snapshot.get("release_tag")
    if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-f]{16}", snapshot_id):
        raise PublishError("invalid snapshot_id")
    if tag != f"db-{snapshot_id}":
        raise PublishError("invalid snapshot_id/release_tag contract")
    if not isinstance(snapshot.get("generated_at"), str):
        raise PublishError("generated_at is missing")

    sha256_manifest = parse_manifest(snapshot_dir / "SHA256SUMS", "sha256")
    md5_manifest = parse_manifest(snapshot_dir / "MD5SUMS", "md5")
    expected_snapshot_id = hashlib.sha256(
        (snapshot_dir / "SHA256SUMS").read_bytes()
    ).hexdigest()[:16]
    if snapshot_id != expected_snapshot_id:
        raise PublishError(
            f"snapshot_id mismatch: expected={expected_snapshot_id} actual={snapshot_id}"
        )

    databases = snapshot.get("databases")
    if not isinstance(databases, dict) or set(databases) != set(DATABASES):
        raise PublishError("SNAPSHOT.json database set is invalid")
    for name in DATABASES:
        path = snapshot_dir / name
        info = databases.get(name)
        if not isinstance(info, dict):
            raise PublishError(f"SNAPSHOT.json metadata is invalid for {name}")
        if type(info.get("version")) is not int or info.get("verified") is not True:
            raise PublishError(f"SNAPSHOT.json verification metadata is invalid for {name}")
        expected_size = path.stat().st_size
        actual_sha256 = digest(path, "sha256")
        actual_md5 = digest(path, "md5")
        if info.get("size") != expected_size:
            raise PublishError(f"SNAPSHOT.json size mismatch for {name}")
        if sha256_manifest[name] != actual_sha256 or info.get("sha256") != actual_sha256:
            raise PublishError(f"SHA256 mismatch for {name}")
        if md5_manifest[name] != actual_md5 or info.get("md5") != actual_md5:
            raise PublishError(f"MD5 mismatch for {name}")
    return snapshot


def verify_asset_metadata(snapshot_dir: Path, release: dict) -> None:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise PublishError("release assets metadata is missing")
    remote = {item.get("name"): item.get("size") for item in assets if isinstance(item, dict)}
    local = {name: (snapshot_dir / name).stat().st_size for name in ASSET_NAMES}
    if remote != local:
        raise PublishError(f"remote asset set/size mismatch: expected={local} actual={remote}")


def verify_downloaded_assets(snapshot_dir: Path, downloaded_dir: Path) -> None:
    actual_names = {path.name for path in downloaded_dir.iterdir() if path.is_file()}
    if actual_names != set(ASSET_NAMES):
        raise PublishError(
            f"downloaded asset set mismatch: expected={sorted(ASSET_NAMES)} actual={sorted(actual_names)}"
        )
    for name in ASSET_NAMES:
        expected = digest(snapshot_dir / name)
        actual = digest(downloaded_dir / name)
        if actual != expected:
            raise PublishError(
                f"downloaded asset digest mismatch for {name}: expected={expected} actual={actual}"
            )


def parse_release(result: subprocess.CompletedProcess[str]) -> dict:
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PublishError(f"invalid release metadata JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PublishError("release metadata must be an object")
    return data


def wait_for_latest(gh: str, repo: str, tag: str, retries: int, delay: float) -> None:
    if retries < 1 or delay < 0:
        raise PublishError("latest verification retry settings are invalid")
    last = "no response"
    for attempt in range(retries):
        result = command(
            [gh, "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"],
            check=False,
        )
        actual = result.stdout.strip() if result.returncode == 0 else ""
        if result.returncode == 0 and actual == tag:
            return
        last = f"exit={result.returncode} tag={actual!r} stderr={result.stderr.strip()!r}"
        if attempt + 1 < retries:
            time.sleep(delay)
    raise PublishError(f"latest release verification failed: expected={tag}; {last}")


def publish(
    repo: str,
    snapshot_dir: Path,
    target: str,
    gh: str,
    latest_retries: int,
    retry_delay: float,
) -> str:
    snapshot = load_snapshot(snapshot_dir)
    tag = snapshot["release_tag"]
    view_args = [
        gh,
        "release",
        "view",
        tag,
        "--repo",
        repo,
        "--json",
        "databaseId,isDraft,assets",
    ]
    existing_result = command(view_args, check=False)
    if existing_result.returncode == 0:
        existing = parse_release(existing_result)
        if existing.get("isDraft"):
            command([gh, "release", "delete", tag, "--repo", repo, "--yes", "--cleanup-tag"])
        else:
            verify_asset_metadata(snapshot_dir, existing)
            with tempfile.TemporaryDirectory(prefix="clamav-release-verify-") as temp:
                command([gh, "release", "download", tag, "--repo", repo, "--dir", temp])
                verify_downloaded_assets(snapshot_dir, Path(temp))
            wait_for_latest(gh, repo, tag, latest_retries, retry_delay)
            return tag

    asset_paths = [str(snapshot_dir / name) for name in ASSET_NAMES]
    command(
        [
            gh,
            "release",
            "create",
            tag,
            *asset_paths,
            "--repo",
            repo,
            "--target",
            target,
            "--title",
            f"ClamAV DB snapshot {snapshot['snapshot_id']}",
            "--notes",
            "Verified immutable ClamAV database snapshot.",
            "--draft",
        ]
    )

    draft = parse_release(command(view_args))
    if not draft.get("isDraft"):
        raise PublishError("new release is not a draft before verification")
    verify_asset_metadata(snapshot_dir, draft)
    with tempfile.TemporaryDirectory(prefix="clamav-release-verify-") as temp:
        command([gh, "release", "download", tag, "--repo", repo, "--dir", temp])
        verify_downloaded_assets(snapshot_dir, Path(temp))

    release_id = draft.get("databaseId")
    if not isinstance(release_id, int):
        raise PublishError("draft release databaseId is missing")
    command(
        [
            gh,
            "api",
            "--method",
            "PATCH",
            f"repos/{repo}/releases/{release_id}",
            "-F",
            "draft=false",
            "-f",
            "make_latest=true",
        ]
    )
    wait_for_latest(gh, repo, tag, latest_retries, retry_delay)
    return tag


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--snapshot-dir", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--latest-retries", type=int, default=5)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        tag = publish(
            args.repo,
            args.snapshot_dir,
            args.target,
            args.gh,
            args.latest_retries,
            args.retry_delay,
        )
    except (PublishError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
