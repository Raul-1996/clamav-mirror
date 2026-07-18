import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "update-clamav-db.yml"


class WorkflowConfigTests(unittest.TestCase):
    def test_setup_python_cache_uses_hash_locked_manifest(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        marker = re.search(r"uses:\s*actions/setup-python@[0-9a-f]{40}", text)
        if marker is None:
            self.fail("setup-python step not found")
        next_step = text.find("\n      - name:", marker.end())
        block = text[marker.start() : next_step if next_step >= 0 else len(text)]
        self.assertIn("cache: pip", block)
        self.assertIn("cache-dependency-path: requirements.lock", block)


if __name__ == "__main__":
    unittest.main()
