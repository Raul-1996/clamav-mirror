import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FETCHER = REPO / "scripts/fetch_previous_snapshot.py"
DATABASES = ("bytecode.cvd", "daily.cvd", "main.cvd")


class FetchPreviousSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.responses = {}
        responses = self.responses

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, headers, body = responses.get(
                    self.path, (404, {"Content-Type": "text/plain"}, b"not found")
                )
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        address = self.server.server_address
        host = str(address[0])
        port = int(address[1])
        self.api_url = f"http://{host}:{port}"
        self.latest_path = "/repos/owner/repo/releases/latest"
        self.output = self.root / "previous.json"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def run_fetcher(self):
        return subprocess.run(
            [
                "python3",
                str(FETCHER),
                "--repo",
                "owner/repo",
                "--output",
                str(self.output),
                "--api-url",
                self.api_url,
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
        )

    def set_json_response(self, path, data, status=200):
        self.responses[path] = (
            status,
            {"Content-Type": "application/json"},
            json.dumps(data).encode("utf-8"),
        )

    def snapshot(self):
        return {
            "schema_version": 1,
            "databases": {name: {"version": index + 1} for index, name in enumerate(DATABASES)},
        }

    def release(self, assets, tag="db-0123456789abcdef"):
        return {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "assets": assets,
        }

    def test_fails_closed_when_latest_release_metadata_is_forbidden(self):
        self.set_json_response(self.latest_path, {"message": "forbidden"}, status=403)
        result = self.run_fetcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP 403", result.stderr)
        self.assertFalse(self.output.exists())

    def test_allows_absence_only_for_exact_current_legacy_latest(self):
        assets = [
            {"name": name, "url": f"{self.api_url}/assets/{index}"}
            for index, name in enumerate(
                ["bytecode.cvd", "daily.cvd", "LAST_RUN.txt", "MD5SUMS", "SHA256SUMS"],
                start=1,
            )
        ]
        self.set_json_response(self.latest_path, self.release(assets, tag="latest"))
        result = self.run_fetcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("confirmed current legacy latest", result.stdout)
        self.assertFalse(self.output.exists())

    def test_rejects_unapproved_release_without_snapshot(self):
        assets = [{"name": "daily.cvd", "url": f"{self.api_url}/assets/1"}]
        self.set_json_response(self.latest_path, self.release(assets, tag="other"))
        result = self.run_fetcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy migration fingerprint", result.stderr)

    def test_fetches_and_validates_snapshot_asset(self):
        asset_url = f"{self.api_url}/assets/7"
        self.set_json_response(
            self.latest_path,
            self.release([{"name": "SNAPSHOT.json", "url": asset_url}]),
        )
        self.set_json_response("/assets/7", self.snapshot())
        result = self.run_fetcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text()), self.snapshot())

    def test_snapshot_asset_transport_or_server_error_is_fatal(self):
        asset_url = f"{self.api_url}/assets/7"
        self.set_json_response(
            self.latest_path,
            self.release([{"name": "SNAPSHOT.json", "url": asset_url}]),
        )
        self.set_json_response("/assets/7", {"message": "retry later"}, status=503)
        result = self.run_fetcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP 503", result.stderr)
        self.assertFalse(self.output.exists())

    def test_invalid_snapshot_schema_is_fatal(self):
        asset_url = f"{self.api_url}/assets/7"
        self.set_json_response(
            self.latest_path,
            self.release([{"name": "SNAPSHOT.json", "url": asset_url}]),
        )
        self.set_json_response("/assets/7", {"schema_version": 99, "databases": {}})
        result = self.run_fetcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version", result.stderr)


if __name__ == "__main__":
    unittest.main()
