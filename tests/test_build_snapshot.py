import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
BUILDER = REPO / "scripts/build_snapshot.py"


FAKE_SIGTOOL = r"""#!/usr/bin/env python3
import pathlib, sys
arg = next((x for x in sys.argv[1:] if x.startswith('--info=')), '')
path = pathlib.Path(arg.split('=', 1)[1])
data = path.read_text(encoding='utf-8', errors='ignore')
if 'INVALID_SIGNATURE' in data:
    print('Verification failed', file=sys.stderr)
    raise SystemExit(2)
version = {'daily.cvd': 28063, 'main.cvd': 62, 'bytecode.cvd': 339}[path.name]
if 'VERSION=' in data:
    version = int(data.split('VERSION=', 1)[1].split()[0])
print(f'File: {path}')
print('Build time: 17 Jul 2026 18:00 +0000')
print(f'Version: {version}')
print('Signatures: 123')
print('Functionality level: 90')
print('Verification OK.')
"""


class BuildSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "db"
        self.db.mkdir()
        self.sigtool = self.root / "sigtool"
        self.sigtool.write_text(FAKE_SIGTOOL, encoding="utf-8")
        self.sigtool.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def write_db(self, name, *, version=None, invalid=False):
        marker = f"VERSION={version} " if version is not None else ""
        if invalid:
            marker += "INVALID_SIGNATURE "
        (self.db / name).write_text(marker + (name + "\n") * 32, encoding="utf-8")

    def populate(self, *, daily=28063, main=62, bytecode=339):
        self.write_db("daily.cvd", version=daily)
        self.write_db("main.cvd", version=main)
        self.write_db("bytecode.cvd", version=bytecode)

    def run_builder(self, out, *extra):
        return subprocess.run(
            [
                "python3",
                str(BUILDER),
                "--db-dir",
                str(self.db),
                "--out-dir",
                str(out),
                "--sigtool",
                str(self.sigtool),
                "--min-size",
                "1",
                *extra,
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
        )

    def test_requires_daily_main_and_bytecode_before_creating_snapshot(self):
        self.write_db("daily.cvd")
        self.write_db("bytecode.cvd")
        out = self.root / "out"
        result = self.run_builder(out)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("main.cvd", result.stderr)
        self.assertFalse((out / "SNAPSHOT.json").exists())

    def test_builds_deterministic_verified_snapshot_and_manifests(self):
        self.populate()
        out = self.root / "out"
        result = self.run_builder(out)
        self.assertEqual(result.returncode, 0, result.stderr)

        expected_names = {
            "daily.cvd",
            "main.cvd",
            "bytecode.cvd",
            "SHA256SUMS",
            "MD5SUMS",
            "SNAPSHOT.json",
        }
        self.assertEqual({p.name for p in out.iterdir()}, expected_names)

        sha_lines = (out / "SHA256SUMS").read_text(encoding="ascii").splitlines()
        self.assertEqual(
            [line.split("  ", 1)[1] for line in sha_lines],
            ["bytecode.cvd", "daily.cvd", "main.cvd"],
        )
        for line in sha_lines:
            digest, name = line.split("  ", 1)
            self.assertEqual(
                hashlib.sha256((out / name).read_bytes()).hexdigest(), digest
            )

        snapshot = json.loads((out / "SNAPSHOT.json").read_text(encoding="utf-8"))
        self.assertRegex(snapshot["snapshot_id"], r"^[0-9a-f]{16}$")
        self.assertEqual(snapshot["release_tag"], f"db-{snapshot['snapshot_id']}")
        self.assertEqual(snapshot["databases"]["daily.cvd"]["version"], 28063)
        self.assertTrue(snapshot["databases"]["daily.cvd"]["verified"])

    def test_snapshot_json_is_identical_when_database_bytes_are_unchanged(self):
        self.populate()
        out_a = self.root / "out-a"
        out_b = self.root / "out-b"
        self.assertEqual(self.run_builder(out_a).returncode, 0)
        self.assertEqual(self.run_builder(out_b).returncode, 0)
        self.assertEqual(
            (out_a / "SNAPSHOT.json").read_bytes(),
            (out_b / "SNAPSHOT.json").read_bytes(),
        )
        snapshot = json.loads((out_a / "SNAPSHOT.json").read_text())
        self.assertEqual(snapshot["generated_at"], "2026-07-17T18:00:00Z")

    def test_rejects_invalid_cvd_signature(self):
        self.populate()
        self.write_db("daily.cvd", version=28063, invalid=True)
        result = self.run_builder(self.root / "out")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signature verification failed", result.stderr.lower())

    def test_rejects_previous_snapshot_with_unknown_schema_version(self):
        self.populate()
        previous = self.root / "previous.json"
        previous.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "databases": {
                        "daily.cvd": {"version": 28063},
                        "main.cvd": {"version": 62},
                        "bytecode.cvd": {"version": 339},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.run_builder(
            self.root / "out", "--previous-snapshot", str(previous)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version", result.stderr)

    def test_rejects_non_integer_previous_database_version(self):
        self.populate()
        previous = self.root / "previous.json"
        previous.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "databases": {
                        "daily.cvd": {"version": 28063},
                        "main.cvd": {"version": "62"},
                        "bytecode.cvd": {"version": 339},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.run_builder(
            self.root / "out", "--previous-snapshot", str(previous)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("main.cvd", result.stderr)
        self.assertIn("version", result.stderr)

    def test_rejects_version_regression_against_previous_snapshot(self):
        self.populate(daily=28062)
        previous = self.root / "previous.json"
        previous.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "databases": {
                        "daily.cvd": {"version": 28063},
                        "main.cvd": {"version": 62},
                        "bytecode.cvd": {"version": 339},
                    }
                }
            ),
            encoding="utf-8",
        )
        result = self.run_builder(
            self.root / "out", "--previous-snapshot", str(previous)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version regression", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
