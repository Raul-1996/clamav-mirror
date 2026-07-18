import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PUBLISHER = REPO / "scripts/publish_snapshot.py"


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
state_path = pathlib.Path(os.environ["FAKE_GH_STATE"])
log_path = pathlib.Path(os.environ["FAKE_GH_LOG"])
source = pathlib.Path(os.environ["FAKE_ASSET_SOURCE"])
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
state = json.loads(state_path.read_text()) if state_path.exists() else {"created": False, "draft": False, "published": False}

def save():
    state_path.write_text(json.dumps(state))

def assets():
    omit = os.environ.get("FAKE_OMIT_ASSET")
    if state.get("old_incomplete"):
        omit = "main.cvd"
    return [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(source.iterdir())
        if p.is_file() and p.name != omit
    ]

if args[:2] == ["release", "view"]:
    if not state.get("created"):
        raise SystemExit(1)
    print(json.dumps({"databaseId": 123, "isDraft": state.get("draft", False), "assets": assets()}))
    raise SystemExit(0)
if args[:2] == ["release", "create"]:
    if os.environ.get("FAKE_CREATE_FAIL") == "1":
        raise SystemExit(9)
    state.update({"created": True, "draft": True, "published": False, "tag": args[2], "old_incomplete": False})
    save()
    raise SystemExit(0)
if args[:2] == ["release", "delete"]:
    state.update({"created": False, "draft": False, "published": False})
    save()
    raise SystemExit(0)
if args[:2] == ["release", "download"]:
    target = pathlib.Path(args[args.index("--dir") + 1])
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_file():
            shutil.copyfile(item, target / item.name)
    if os.environ.get("FAKE_TAMPER") == "1":
        with (target / "daily.cvd").open("ab") as stream:
            stream.write(b"tampered")
    raise SystemExit(0)
if args and args[0] == "api" and "PATCH" in args:
    if os.environ.get("FAKE_PATCH_FAIL") == "1":
        raise SystemExit(8)
    state.update({"draft": False, "published": True})
    save()
    print(json.dumps({"id": 123, "draft": False}))
    raise SystemExit(0)
if args and args[0] == "api" and args[1].endswith("/releases/latest"):
    if not state.get("published"):
        raise SystemExit(1)
    delay = int(os.environ.get("FAKE_LATEST_DELAY", "0"))
    checks = int(state.get("latest_checks", 0))
    if checks < delay:
        state["latest_checks"] = checks + 1
        save()
        raise SystemExit(1)
    print(state["tag"])
    raise SystemExit(0)
print("unexpected fake gh args", args, file=sys.stderr)
raise SystemExit(97)
'''


class PublishSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        for name, data in {
            "bytecode.cvd": b"bytecode",
            "daily.cvd": b"daily",
            "main.cvd": b"main",
        }.items():
            (self.out / name).write_bytes(data)
        sha_lines = []
        md5_lines = []
        for name in ("bytecode.cvd", "daily.cvd", "main.cvd"):
            data = (self.out / name).read_bytes()
            sha_lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
            md5_lines.append(f"{hashlib.md5(data).hexdigest()}  {name}\n")
        sha_text = "".join(sha_lines)
        md5_text = "".join(md5_lines)
        (self.out / "SHA256SUMS").write_text(sha_text, encoding="ascii")
        (self.out / "MD5SUMS").write_text(md5_text, encoding="ascii")
        self.snapshot_id = hashlib.sha256(sha_text.encode("ascii")).hexdigest()[:16]
        databases = {}
        for index, name in enumerate(("bytecode.cvd", "daily.cvd", "main.cvd"), start=1):
            data = (self.out / name).read_bytes()
            databases[name] = {
                "version": index,
                "verified": True,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "md5": hashlib.md5(data).hexdigest(),
            }
        (self.out / "SNAPSHOT.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "snapshot_id": self.snapshot_id,
                    "release_tag": f"db-{self.snapshot_id}",
                    "generated_at": "2026-07-18T06:00:00Z",
                    "databases": databases,
                }
            ),
            encoding="utf-8",
        )
        self.fake_gh = self.root / "gh"
        self.fake_gh.write_text(FAKE_GH, encoding="utf-8")
        self.fake_gh.chmod(0o755)
        self.state = self.root / "state.json"
        self.log = self.root / "gh.log"

    def tearDown(self):
        self.tmp.cleanup()

    def run_publisher(self, **flags):
        env = os.environ.copy()
        env.update(
            {
                "FAKE_GH_STATE": str(self.state),
                "FAKE_GH_LOG": str(self.log),
                "FAKE_ASSET_SOURCE": str(self.out),
            }
        )
        env.update(
            {
                key: ("1" if value else "0") if isinstance(value, bool) else str(value)
                for key, value in flags.items()
            }
        )
        return subprocess.run(
            [
                "python3",
                str(PUBLISHER),
                "--repo",
                "Raul-1996/clamav-mirror",
                "--snapshot-dir",
                str(self.out),
                "--target",
                "deadbeef",
                "--gh",
                str(self.fake_gh),
                "--retry-delay",
                "0",
            ],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
        )

    def commands(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_publishes_only_after_complete_downloaded_assets_match(self):
        result = self.run_publisher()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        download_index = next(i for i, x in enumerate(commands) if x[:2] == ["release", "download"])
        patch_index = next(i for i, x in enumerate(commands) if x and x[0] == "api" and "PATCH" in x)
        self.assertLess(download_index, patch_index)
        state = json.loads(self.state.read_text())
        self.assertTrue(state["published"])
        self.assertFalse(state["draft"])

    def test_incomplete_existing_draft_is_deleted_and_recreated(self):
        self.state.write_text(
            json.dumps(
                {
                    "created": True,
                    "draft": True,
                    "published": False,
                    "tag": f"db-{self.snapshot_id}",
                    "old_incomplete": True,
                }
            ),
            encoding="utf-8",
        )
        result = self.run_publisher()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        delete_index = next(i for i, x in enumerate(commands) if x[:2] == ["release", "delete"])
        create_index = next(i for i, x in enumerate(commands) if x[:2] == ["release", "create"])
        self.assertLess(delete_index, create_index)
        self.assertTrue(json.loads(self.state.read_text())["published"])

    def test_retries_eventually_consistent_latest_readback(self):
        result = self.run_publisher(FAKE_LATEST_DELAY=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["latest_checks"], 2)
        self.assertTrue(state["published"])

    def test_local_cvd_tamper_fails_before_any_github_call(self):
        with (self.out / "daily.cvd").open("ab") as stream:
            stream.write(b"tampered")
        result = self.run_publisher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mismatch", result.stderr.lower())
        self.assertEqual(self.commands(), [])

    def test_invalid_snapshot_metadata_fails_before_any_github_call(self):
        snapshot_path = self.out / "SNAPSHOT.json"
        snapshot = json.loads(snapshot_path.read_text())
        snapshot["databases"]["daily.cvd"]["version"] = "not-an-integer"
        snapshot_path.write_text(json.dumps(snapshot))
        result = self.run_publisher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("metadata", result.stderr.lower())
        self.assertEqual(self.commands(), [])

    def test_upload_failure_never_calls_publish_patch(self):
        result = self.run_publisher(FAKE_CREATE_FAIL=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(x and x[0] == "api" and "PATCH" in x for x in self.commands()))

    def test_tampered_download_never_calls_publish_patch(self):
        result = self.run_publisher(FAKE_TAMPER=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mismatch", result.stderr.lower())
        self.assertFalse(any(x and x[0] == "api" and "PATCH" in x for x in self.commands()))

    def test_missing_remote_asset_never_calls_publish_patch(self):
        result = self.run_publisher(FAKE_OMIT_ASSET="main.cvd")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("asset set", result.stderr.lower())
        self.assertFalse(any(x and x[0] == "api" and "PATCH" in x for x in self.commands()))


if __name__ == "__main__":
    unittest.main()
