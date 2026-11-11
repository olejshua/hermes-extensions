import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from source_release import Release, ReleaseError, encode, git

CORE_PATCH = b"""diff --git a/agent/demo.py b/agent/demo.py
--- a/agent/demo.py
+++ b/agent/demo.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hermes-source-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.core = self.root / "core"
        self.core.mkdir()
        git(self.core, "init", "-q")
        (self.core / "agent").mkdir()
        self.core_file = self.core / "agent/demo.py"
        self.core_file.write_text("VALUE = 1\n")
        self.upstream = self.commit(self.core)
        self.core_file.write_text("VALUE = 2\n")
        self.live = self.root / "live"
        self.live.mkdir()
        self.target = self.live / "logic.py"
        self.before = "MAILBOX = 'person@private.example'\n\n\n\n\n\ndef result():\n    return 'one'\n"
        self.target.write_text(self.before)
        self.state = self.root / "private"
        self.state.mkdir()
        self.release = Release(self.state, {
            "repository": "https://github.com/example/hermes.git",
            "upstream": self.upstream,
            "roots": {"core": str(self.core), "runtime/demo": str(self.live)},
        })
        git(self.state, "init", "-q", self.release.repo.name)
        (self.release.repo / "patches").mkdir()
        (self.release.repo / "patches/hermes-agent.patch").write_bytes(CORE_PATCH)
        (self.release.repo / "runtime/demo").mkdir(parents=True)
        self.file = self.release.repo / "runtime/demo/logic.py"
        self.file.write_text(self.before.replace("person@private.example", "person@example.invalid"))
        self.initial = self.commit(self.release.repo)
        # This fixture exercises disk/patch operations. HTTPS is checked on the actual server.
        self.stub = patch.object(self.release, "fetch")
        self.stub.start()
        self.addCleanup(self.stub.stop)
        report = self.release.enroll(self.initial, 1)
        self.assertEqual(report["managed_files"], 2)
        self.assertEqual(self.target.read_text(), self.before)

    @staticmethod
    def commit(repo):
        git(repo, "add", "--all")
        git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "Synthetic source")
        return git(repo, "rev-parse", "HEAD").stdout.decode().strip()

    def test_repo_release_and_native_publisher_binding(self):
        self.file.write_text(self.file.read_text().replace("return 'one'", "return 'two'"))
        candidate = self.commit(self.release.repo)
        staged = self.release.prepare(candidate)
        self.assertEqual(staged["changes"], 1)
        base = Path(staged["plan"]).parent
        source = base / "payload/runtime/demo/logic.py"
        self.assertEqual(source.read_text(), self.before.replace("return 'one'", "return 'two'"))
        rows = self.root / "publisher.json"
        rows.write_bytes(encode([{"target": str(self.target), "source": str(self.file)}]))
        with self.assertRaisesRegex(ReleaseError, "publisher source changed"):
            self.release.verify(candidate, rows)
        rows.write_bytes(encode([{"target": str(self.target), "source": str(source)}]))
        self.assertTrue(self.release.verify(candidate, rows)["native_inputs_verified"])
        with self.assertRaisesRegex(ReleaseError, "installed source does not match"):
            self.release.verify(candidate, accept=True)
        shutil.copyfile(source, self.target)  # Represents the checked publisher on this private fixture.
        self.release.verify(candidate, accept=True)
        self.assertEqual(self.release.status()["commit"], candidate)
        self.assertEqual(self.release.status()["drift"], [])

    def test_profile_conflict_and_foreign_live_edit_stop_before_publication(self):
        self.file.write_text(self.file.read_text().replace("person@example.invalid", "new@example.invalid"))
        conflict = self.commit(self.release.repo)
        with self.assertRaisesRegex(ReleaseError, "git apply failed"):
            self.release.prepare(conflict)
        self.assertEqual(self.target.read_text(), self.before)
        self.target.write_text(self.before + "\n# Foreign edit\n")
        with self.assertRaisesRegex(ReleaseError, "source or profile drift"):
            self.release.prepare(conflict)
        self.assertEqual(self.release.status()["drift"], ["runtime/demo/logic.py"])

    def test_mutated_stage_cannot_be_approved(self):
        self.file.write_text(self.file.read_text().replace("return 'one'", "return 'two'"))
        candidate = self.commit(self.release.repo)
        staged = self.release.prepare(candidate)
        p = Path(staged["plan"]).parent / "payload/runtime/demo/logic.py"
        p.write_text("raise RuntimeError('tampered')\n")
        with self.assertRaisesRegex(ReleaseError, "prepared source differs"):
            self.release.verify(candidate)
        self.assertEqual(self.target.read_text(), self.before)


if __name__ == "__main__":
    unittest.main()
