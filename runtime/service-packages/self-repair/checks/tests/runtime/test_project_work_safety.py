"""Characterization of ole_project_work.safety: model-writable artifact dirs must not escape."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5] / 'hermes-home/ole-project-work/package'))
from ole_project_work import safety  # noqa: E402


class Safety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def code(self, fn, *a):
        with self.assertRaises(safety.Fault) as cm:
            fn(*a)
        return cm.exception.code

    def test_artifact_path_rejections(self):
        work = self.root / 'work'
        (work / 'artifacts').mkdir(parents=True)
        for rel, code in (('../x.md', 'unsafe_artifact'), ('notes.md', 'unsafe_artifact'),
                          ('artifacts/.hidden.md', 'unsafe_artifact'), ('artifacts/a/../../x.md', 'unsafe_artifact'),
                          ('/etc/passwd', 'unsafe_artifact'), ('artifacts/run.sh', 'artifact_type_forbidden'),
                          ('artifacts/x.py', 'artifact_type_forbidden'), (None, 'unsafe_artifact'),
                          ('artifacts/' + 'a' * 250 + '.md', 'unsafe_artifact')):
            self.assertEqual(self.code(safety.artifact_path, work, rel), code, rel)
        self.assertEqual(safety.artifact_path(work, 'artifacts/report.md'), work / 'artifacts/report.md')

    def test_symlinked_artifact_directory_is_refused(self):
        work = self.root / 'work'
        outside = self.root / 'outside'
        outside.mkdir()
        work.mkdir()
        (work / 'artifacts').symlink_to(outside)
        self.assertEqual(self.code(safety.artifact_path, work, 'artifacts/r.md'), 'symlink_forbidden')

    def test_read_file_refuses_symlink_hardlink_and_oversize(self):
        real = self.root / 'real.txt'
        real.write_text('secret')
        link = self.root / 'link.txt'
        link.symlink_to(real)
        self.assertEqual(self.code(safety.read_file, link), 'symlink_forbidden')
        hard = self.root / 'hard.txt'
        os.link(real, hard)
        self.assertEqual(self.code(safety.read_file, real), 'regular_single_link_file_required')
        os.unlink(hard)
        self.assertEqual(safety.read_file(real), b'secret')
        self.assertEqual(self.code(safety.read_file, real, 3), 'file_too_large')

    def test_read_file_refuses_fifo_without_blocking(self):
        fifo = self.root / 'pipe'
        os.mkfifo(fifo)
        self.assertEqual(self.code(safety.read_file, fifo), 'regular_single_link_file_required')

    def test_write_private_is_atomic_0600_and_leaves_no_temp(self):
        target = self.root / 'state.json'
        safety.write_private(target, '{"a":1}')
        safety.write_private(target, '{"a":2}')
        self.assertEqual(target.read_text(), '{"a":2}')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.root.iterdir()], ['state.json'])

    def test_private_directory_rejects_loose_mode_and_relative_paths(self):
        loose = self.root / 'loose'
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        self.assertEqual(self.code(safety.private, loose), 'private_directory_required')
        self.assertEqual(self.code(safety.private, 'relative/dir'), 'unsafe_path')
        self.assertEqual(self.code(safety.private, self.root / 'a' / '..' / 'b'), 'unsafe_path')

    def test_text_validation(self):
        for bad in ('', '  ', 'a\0b', 'x' * 20, 5, None):
            self.assertEqual(self.code(safety.text, bad, 10), 'invalid_text')
        self.assertEqual(safety.text('ok', 10), 'ok')

    def test_digest_is_key_order_independent(self):
        self.assertEqual(safety.digest({'a': 1, 'b': 2}), safety.digest({'b': 2, 'a': 1}))
        self.assertNotEqual(safety.digest({'a': 1}), safety.digest({'a': 2}))


if __name__ == '__main__':
    unittest.main()
