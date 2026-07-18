#!/usr/bin/env python3
"""Build a verified, deterministic ClamAV database snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATABASES = ("bytecode.cvd", "daily.cvd", "main.cvd")
VERSION_RE = re.compile(r"^Version:\s*(\d+)\s*$", re.MULTILINE)
BUILD_TIME_RE = re.compile(r"^Build time:\s*(.+?)\s*$", re.MULTILINE)
FUNCTIONALITY_RE = re.compile(r"^Functionality level:\s*(\d+)\s*$", re.MULTILINE)
BUILD_TIME_VALUE_RE = re.compile(
    r"^(\d{1,2}) ([A-Za-z]{3}) (\d{4}) (\d{2})[:-](\d{2}) ([+-])(\d{2})(\d{2})$"
)
MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        start=1,
    )
}


class SnapshotError(RuntimeError):
    pass


def file_hash(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_database(path: Path, sigtool: str) -> dict[str, object]:
    result = subprocess.run(
        [sigtool, f"--info={path}"], text=True, capture_output=True, check=False
    )
    output = result.stdout + result.stderr
    if result.returncode != 0 or "Verification OK" not in output:
        raise SnapshotError(
            f"signature verification failed for {path.name}: exit={result.returncode}"
        )

    version_match = VERSION_RE.search(output)
    if not version_match:
        raise SnapshotError(f"sigtool did not report a version for {path.name}")

    build_time_match = BUILD_TIME_RE.search(output)
    functionality_match = FUNCTIONALITY_RE.search(output)
    return {
        "version": int(version_match.group(1)),
        "build_time": build_time_match.group(1) if build_time_match else "unknown",
        "functionality_level": (
            int(functionality_match.group(1)) if functionality_match else None
        ),
        "verified": True,
    }


def generated_at(metadata: dict[str, dict[str, object]]) -> str:
    build_times: list[datetime] = []
    for name in DATABASES:
        raw = metadata[name].get("build_time")
        match = BUILD_TIME_VALUE_RE.fullmatch(raw) if isinstance(raw, str) else None
        if not match or match.group(2).title() not in MONTHS:
            raise SnapshotError(f"invalid build time for {name}: {raw!r}")
        day, month_name, year, hour, minute, sign, offset_hour, offset_minute = (
            match.groups()
        )
        offset = timedelta(hours=int(offset_hour), minutes=int(offset_minute))
        if sign == "-":
            offset = -offset
        try:
            value = datetime(
                int(year),
                MONTHS[month_name.title()],
                int(day),
                int(hour),
                int(minute),
                tzinfo=timezone(offset),
            )
        except ValueError as exc:
            raise SnapshotError(f"invalid build time for {name}: {raw!r}") from exc
        build_times.append(value.astimezone(timezone.utc))
    return max(build_times).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_previous(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"cannot read previous snapshot: {exc}") from exc
    if not isinstance(data, dict):
        raise SnapshotError("previous snapshot must be a JSON object")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise SnapshotError(
            f"invalid previous schema_version: {data.get('schema_version')!r}"
        )
    databases = data.get("databases")
    if not isinstance(databases, dict):
        raise SnapshotError("previous databases metadata must be an object")
    for name in DATABASES:
        info = databases.get(name)
        if not isinstance(info, dict) or type(info.get("version")) is not int:
            value = info.get("version") if isinstance(info, dict) else None
            raise SnapshotError(f"invalid previous version for {name}: {value!r}")
    return data


def build_snapshot(
    db_dir: Path,
    out_dir: Path,
    sigtool: str,
    min_size: int,
    previous_path: Path | None,
) -> dict:
    if min_size < 1:
        raise SnapshotError("min-size must be at least 1 byte")
    if out_dir.exists():
        raise SnapshotError(f"output directory already exists: {out_dir}")

    previous = load_previous(previous_path)
    previous_databases = previous.get("databases", {})
    metadata: dict[str, dict[str, object]] = {}

    for name in DATABASES:
        path = db_dir / name
        if not path.is_file():
            raise SnapshotError(f"required database is missing: {name}")
        size = path.stat().st_size
        if size < min_size:
            raise SnapshotError(
                f"database {name} is too small: {size} bytes (minimum {min_size})"
            )
        info = inspect_database(path, sigtool)
        incoming_version = info.get("version")
        if not isinstance(incoming_version, int):
            raise SnapshotError(f"invalid incoming version for {name}: {incoming_version!r}")
        previous_info = previous_databases.get(name, {})
        previous_version = previous_info.get("version")
        if previous_version is not None:
            try:
                previous_version = int(previous_version)
            except (TypeError, ValueError) as exc:
                raise SnapshotError(
                    f"invalid previous version for {name}: {previous_version!r}"
                ) from exc
            if incoming_version < previous_version:
                raise SnapshotError(
                    f"version regression for {name}: "
                    f"incoming={incoming_version} previous={previous_version}"
                )
        info.update(
            {
                "size": size,
                "sha256": file_hash(path, "sha256"),
                "md5": file_hash(path, "md5"),
            }
        )
        metadata[name] = info

    sha_lines = [f"{metadata[name]['sha256']}  {name}\n" for name in DATABASES]
    md5_lines = [f"{metadata[name]['md5']}  {name}\n" for name in DATABASES]
    snapshot_id = hashlib.sha256("".join(sha_lines).encode("ascii")).hexdigest()[:16]
    snapshot = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "release_tag": f"db-{snapshot_id}",
        "generated_at": generated_at(metadata),
        "databases": metadata,
    }

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{out_dir.name}.tmp-", dir=str(out_dir.parent))
    )
    try:
        for name in DATABASES:
            shutil.copyfile(db_dir / name, staging / name)
        (staging / "SHA256SUMS").write_text("".join(sha_lines), encoding="ascii")
        (staging / "MD5SUMS").write_text("".join(md5_lines), encoding="ascii")
        (staging / "SNAPSHOT.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, out_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--sigtool", default="sigtool")
    parser.add_argument("--min-size", type=int, default=100_000)
    parser.add_argument("--previous-snapshot", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        snapshot = build_snapshot(
            args.db_dir,
            args.out_dir,
            args.sigtool,
            args.min_size,
            args.previous_snapshot,
        )
    except SnapshotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"snapshot_id": snapshot["snapshot_id"], "release_tag": snapshot["release_tag"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
