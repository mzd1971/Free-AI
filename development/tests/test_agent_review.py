"""Exercise file-tool review independently of the desktop and the model."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_review import review_changes
from project_agent import ToolError, _sha


DEVELOPMENT = Path(__file__).resolve().parents[1]


class AgentReviewTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.runs = self.root / 'runs'
        self.run = self.runs / 'one'
        self.run.mkdir(parents=True)
        self.changes = []

    def change(self, path, before, after, *, legacy=False, applied=True):
        index = len(self.changes)
        row = {'path': path, 'before_sha256': _sha(before), 'after_sha256': _sha(after),
               'backup': None, 'before_mode': 0o644 if before is not None else None}
        if before is not None:
            row['backup'] = f'backup-{index:04d}'
            (self.run / row['backup']).write_bytes(before)
        if not legacy:
            row.update(after_backup=f'after-{index:04d}', applied=applied)
            (self.run / row['after_backup']).write_bytes(after)
        self.changes.append(row)
        return row

    def manifest(self):
        (self.run / 'changes.json').write_text(json.dumps({
            'version': 1, 'workspace': str(self.workspace.resolve()), 'changes': self.changes,
        }), encoding='utf-8')

    def review(self, **kwargs):
        self.manifest()
        return review_changes(self.runs, self.run, self.workspace, **kwargs)

    def test_diff_uses_first_and_last_snapshot_across_edits_and_never_writes(self):
        self.change('main.py', b'x = 1\n', b'x = 2\n')
        self.change('new.py', None, '新文件\n'.encode())
        self.change('main.py', b'x = 2\n', b'x = 3\n')
        self.change('reset.py', b'original\n', b'temporary\n')
        self.change('reset.py', b'temporary\n', b'original\n')
        current = self.workspace / 'main.py'
        current.write_bytes(b'user changed it later\n')

        result = self.review()
        main, new, reset = result['files']
        self.assertEqual((main['path'], main['status'], main['added'], main['removed']),
                         ('main.py', 'modified', 1, 1))
        self.assertIn('-x = 1\n+x = 3\n', main['diff'])
        self.assertNotIn('x = 2', main['diff'])
        self.assertNotIn('user changed', main['diff'])
        self.assertEqual((new['status'], new['added'], new['removed']), ('new', 1, 0))
        self.assertIn('--- /dev/null\n+++ b/new.py\n', new['diff'])
        self.assertEqual((reset['status'], reset['diff']), ('unchanged', ''))
        self.assertEqual(current.read_bytes(), b'user changed it later\n')
        self.assertFalse((self.workspace / 'new.py').exists())
        self.assertFalse(result['truncated'])

    def test_legacy_requires_exact_current_hash_and_modern_missing_snapshot_is_unavailable(self):
        self.change('legacy.py', b'old\n', b'agent\n', legacy=True)
        modern = self.change('modern.py', b'old\n', b'agent\n')
        (self.run / modern['after_backup']).unlink()
        legacy = self.workspace / 'legacy.py'
        legacy.write_bytes(b'agent\n')
        (self.workspace / 'modern.py').write_bytes(b'agent\n')
        self.assertEqual([file['status'] for file in self.review()['files']], ['modified', 'unavailable'])
        legacy.write_bytes(b'user\n')
        result = self.review()
        self.assertEqual([file['status'] for file in result['files']], ['unavailable', 'unavailable'])
        self.assertFalse(any(file['diff'] for file in result['files']))
        self.assertIn('可验证', result['notice'])

    def test_pending_discontinuous_and_altered_snapshots_are_unavailable(self):
        self.change('pending.py', None, b'new\n', applied=False)
        (self.workspace / 'pending.py').write_bytes(b'new\n')
        self.change('discontinuous.py', b'old\n', b'first\n')
        self.change('discontinuous.py', b'other\n', b'last\n')
        corrupted = self.change('corrupted.py', b'old\n', b'new\n')
        (self.run / corrupted['after_backup']).write_bytes(b'other\n')
        files = self.review()['files']
        self.assertEqual([file['status'] for file in files], ['unavailable'] * 3)
        self.assertFalse(any(file['diff'] for file in files))

    def test_rejects_wrong_workspace_run_traversal_and_snapshot_name(self):
        row = self.change('main.py', None, b'content\n')
        self.manifest()
        other_workspace = self.root / 'other'
        other_workspace.mkdir()
        outside_run = self.root / 'one'
        outside_run.mkdir()
        for run in (outside_run, 'nested/one', self.runs / 'one' / '..' / 'one'):
            with self.subTest(run=run), self.assertRaises(ToolError):
                review_changes(self.runs, run, self.workspace)
        with self.assertRaises(ToolError):
            review_changes(self.runs, self.run, other_workspace)
        row['after_backup'] = '../elsewhere'
        with self.assertRaises(ToolError):
            self.review()
        row['after_backup'] = 'after-0000'
        row['path'] = '../elsewhere'
        with self.assertRaises(ToolError):
            self.review()

    def test_hard_linked_snapshot_and_legacy_file_are_unavailable(self):
        row = self.change('snapshot.py', None, b'new\n')
        self.change('legacy.py', None, b'new\n', legacy=True)
        outside = self.root / 'outside.txt'
        outside.write_bytes(b'new\n')
        backup = self.run / row['after_backup']
        backup.unlink()
        os.link(outside, backup)
        os.link(outside, self.workspace / 'legacy.py')
        self.assertEqual([file['status'] for file in self.review()['files']], ['unavailable'] * 2)

    def test_rejects_reparse_run_and_legacy_parent(self):
        self.change('linked/snapshot.py', None, b'new\n', legacy=True)
        (self.run / 'snapshot.py').write_bytes(b'new\n')
        self.manifest()
        for alias in (self.runs / 'alias', self.workspace / 'linked'):
            if os.name == 'nt':
                result = subprocess.run(
                    ['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(self.run)],
                    cwd=self.root, capture_output=True,
                )
                if result.returncode:
                    self.skipTest('Windows junction unavailable')
                self.addCleanup(alias.rmdir)
            else:
                alias.symlink_to(self.run, target_is_directory=True)
                self.addCleanup(alias.unlink)
        with self.assertRaises(ToolError):
            review_changes(self.runs, self.runs / 'alias', self.workspace)
        self.assertEqual(self.review()['files'][0]['status'], 'unavailable')

    def test_symlinked_snapshot_is_unavailable_when_supported(self):
        row = self.change('snapshot.py', None, b'new\n')
        snapshot = self.run / row['after_backup']
        source = self.root / 'outside.txt'
        source.write_bytes(snapshot.read_bytes())
        try:
            snapshot.unlink()
            snapshot.symlink_to(source)
        except OSError as exc:
            self.skipTest(f'Cannot create symbolic links: {exc}')
        self.assertEqual(self.review()['files'][0]['status'], 'unavailable')

    def test_corrupt_files_still_consume_the_shared_read_budget(self):
        row = self.change('corrupt.py', b'old', b'new')
        (self.run / row['after_backup']).write_bytes(b'bad')
        self.change('later.py', None, b'next')
        with patch('agent_review.MAX_INPUT', 8):
            result = self.review()
        self.assertEqual([file['status'] for file in result['files']], ['unavailable', 'truncated'])
        self.assertTrue(result['truncated'])

    def test_diff_byte_and_line_budgets_with_unicode_and_no_final_newline(self):
        self.change('字符.py', b'old\n', ('新字' * 400).encode())
        self.change('lines.py', None, b'line\n' * 3000)
        result = self.review(budget=300)
        self.assertTrue(result['truncated'])
        self.assertIn('上限', result['notice'])
        self.assertEqual([file['status'] for file in result['files']], ['truncated'] * 2)
        self.assertLessEqual(sum(len(file['diff'].encode()) for file in result['files']), 300)
        full = self.review()['files'][0]
        self.assertEqual(full['status'], 'modified')
        self.assertIn('文件末尾没有换行', full['diff'])


if __name__ == '__main__':
    unittest.main()
