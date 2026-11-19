"""Exercise project snapshots using real files on the repository's drive."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from project_agent import ToolError, _sha
from project_snapshot import (
    SnapshotCancelled, _manifest_json, describe_snapshot, finish_snapshot, restore_snapshot,
    review_snapshot, start_snapshot,
)


DEVELOPMENT = Path(__file__).resolve().parents[1]


class ProjectSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.snapshots = self.root / 'snapshots'

    def write(self, name, text):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def start(self):
        return Path(start_snapshot(self.snapshots, self.workspace))

    def finish(self, snapshot):
        return finish_snapshot(self.snapshots, snapshot, self.workspace)

    def review(self, snapshot, **options):
        return review_snapshot(self.snapshots, snapshot, self.workspace, **options)

    def describe(self, snapshot):
        return describe_snapshot(self.snapshots, snapshot, self.workspace)

    def restore(self, snapshot, **options):
        return restore_snapshot(self.snapshots, snapshot, self.workspace, **options)

    def rewrite(self, archive, modify):
        with ZipFile(archive) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        modify(entries)
        with ZipFile(archive, 'w') as target:
            for name, data in entries.items():
                target.writestr(name, data)

    def test_capture_and_restore_actual_command_created_updated_and_deleted_files(self):
        self.write('update.txt', 'original\n')
        self.write('removed/old.txt', 'previous file\n')
        snapshot = self.start()
        command = """
from pathlib import Path
Path('update.txt').write_bytes(b'changed\\n')
Path('created').mkdir()
Path('created/new.txt').write_bytes(b'new file\\n')
Path('removed/old.txt').unlink()
Path('removed').rmdir()
"""
        subprocess.run([sys.executable, '-B', '-c', command], cwd=self.workspace, shell=False,
                       check=True, timeout=10, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

        result = self.finish(snapshot)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['files'], ['created/new.txt', 'removed/old.txt', 'update.txt'])
        self.assertEqual(result['count'], 3)
        diffs = {row['path']: row for row in self.review(snapshot)['files']}
        self.assertEqual((diffs['created/new.txt']['status'], diffs['created/new.txt']['added']),
                         ('new', 1))
        self.assertEqual((diffs['removed/old.txt']['status'], diffs['removed/old.txt']['removed']),
                         ('deleted', 1))
        self.assertIn('+++ /dev/null\n', diffs['removed/old.txt']['diff'])
        self.assertIn('-original\n+changed\n', diffs['update.txt']['diff'])

        restored = self.restore(snapshot)
        self.assertCountEqual(restored['restored'], result['files'])
        self.assertEqual(restored['conflicts'], [])
        self.assertEqual((self.workspace / 'update.txt').read_bytes(), b'original\n')
        self.assertEqual((self.workspace / 'removed/old.txt').read_bytes(), b'previous file\n')
        self.assertFalse((self.workspace / 'created/new.txt').exists())
        again = self.restore(snapshot)
        self.assertEqual((again['restored'], again['conflicts']), ([], []))

    def test_current_user_changes_and_recreated_deletions_are_preserved(self):
        self.write('user.txt', 'before')
        deleted = self.write('removed.txt', 'old content')
        self.write('clean.txt', 'before')
        snapshot = self.start()
        self.write('user.txt', 'agent content')
        deleted.unlink()
        self.write('clean.txt', 'agent content')
        self.write('added.txt', 'agent content')
        self.finish(snapshot)

        self.write('user.txt', 'user edit')
        self.write('removed.txt', 'user recreation')
        self.write('added.txt', 'user edit to addition')
        restored = self.restore(snapshot)
        self.assertEqual(restored['restored'], ['clean.txt'])
        self.assertCountEqual(restored['conflicts'], ['user.txt', 'removed.txt', 'added.txt'])
        self.assertEqual((self.workspace / 'user.txt').read_text(), 'user edit')
        self.assertEqual((self.workspace / 'removed.txt').read_text(), 'user recreation')
        self.assertEqual((self.workspace / 'added.txt').read_text(), 'user edit to addition')

    def test_selective_restore_preserves_user_edits_captured_in_the_same_turn(self):
        self.write('task.txt', 'before task')
        self.write('user.txt', 'before user')
        snapshot = self.start()
        self.write('task.txt', 'agent change')
        self.write('user.txt', 'user change during task')
        self.assertEqual(self.finish(snapshot)['files'], ['task.txt', 'user.txt'])

        empty = self.restore(snapshot, paths=[])
        self.assertEqual((empty['restored'], empty['conflicts']), ([], []))
        self.assertEqual((self.workspace / 'task.txt').read_text(), 'agent change')
        result = self.restore(snapshot, paths=['task.txt'])
        self.assertEqual((result['restored'], result['conflicts']), (['task.txt'], []))
        self.assertEqual((self.workspace / 'task.txt').read_text(), 'before task')
        self.assertEqual((self.workspace / 'user.txt').read_text(), 'user change during task')

    def test_invalid_restore_selection_is_rejected_before_any_mutation(self):
        self.write('track.txt', 'before')
        snapshot = self.start()
        self.write('track.txt', 'after')
        self.finish(snapshot)
        for paths in ('track.txt', {'track.txt'}, ['track.txt', 1], ['track.txt', 'track.txt'],
                      ['track.txt', '../escape'], ['track.txt', 'unknown.txt']):
            with self.subTest(paths=paths), self.assertRaises(ToolError):
                self.restore(snapshot, paths=paths)
            self.assertEqual((self.workspace / 'track.txt').read_text(), 'after')

    def test_summary_can_be_rebuilt_from_archives_without_saved_task_metadata(self):
        self.write('tracked.txt', 'before')
        snapshot = self.start()
        self.write('tracked.txt', 'after')
        with patch('project_snapshot._capture', side_effect=AssertionError('Read must not capture.')):
            pending = self.describe(snapshot)
        self.assertEqual((pending['status'], pending['files'], pending['count']), ('pending', [], 0))
        self.assertFalse((snapshot / 'after.zip').exists())

        self.finish(snapshot)
        saved = (snapshot / 'after.zip').read_bytes()
        self.write('tracked.txt', 'later user change')
        self.write('later.txt', 'new file not present when the task ended')
        with patch('project_snapshot._capture', side_effect=AssertionError('Read must not capture.')):
            recovered = self.describe(snapshot)
        self.assertEqual((recovered['status'], recovered['files'], recovered['count']),
                         ('ready', ['tracked.txt'], 1))
        self.assertEqual((snapshot / 'after.zip').read_bytes(), saved)

    def test_finish_is_idempotent_and_diffs_do_not_follow_the_live_file(self):
        self.write('tracked.txt', 'baseline\n')
        snapshot = self.start()
        self.write('tracked.txt', 'task change\n')
        summary = self.finish(snapshot)
        preview = self.review(snapshot)
        after = (snapshot / 'after.zip').read_bytes()
        self.write('tracked.txt', 'user change\n')
        self.assertEqual(self.finish(snapshot), summary)
        self.assertEqual(self.review(snapshot), preview)
        self.assertEqual((snapshot / 'after.zip').read_bytes(), after)
        self.assertEqual(self.restore(snapshot)['conflicts'], ['tracked.txt'])

    def test_failed_finish_preserves_baseline_and_can_be_retried(self):
        self.write('keep.txt', 'before')
        snapshot = self.start()
        self.write('keep.txt', 'after')
        baseline = (snapshot / 'before.zip').read_bytes()
        with patch('project_snapshot._atomic_bytes', side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):
                self.finish(snapshot)
        self.assertFalse((snapshot / 'after.zip').exists())
        self.assertEqual((snapshot / 'before.zip').read_bytes(), baseline)
        self.assertEqual(self.finish(snapshot)['files'], ['keep.txt'])

    def test_before_only_snapshot_is_kept_and_never_reverts_live_files(self):
        self.write('keep.txt', 'before')
        snapshot = self.start()
        self.write('keep.txt', 'after an interruption')
        self.write('created.txt', 'new')
        self.assertTrue((snapshot / 'before.zip').is_file())
        self.assertFalse((snapshot / 'after.zip').exists())
        self.assertEqual(self.review(snapshot)['files'], [])
        result = self.restore(snapshot)
        self.assertEqual((result['restored'], result['conflicts']), ([], []))
        self.assertIn('缺少任务结束快照', result['notice'])
        self.assertEqual((self.workspace / 'keep.txt').read_text(), 'after an interruption')
        self.assertTrue((self.workspace / 'created.txt').is_file())

    def test_start_can_cancel_without_leaving_a_committed_baseline(self):
        cancel = Event()
        cancel.set()
        with self.assertRaises(SnapshotCancelled):
            start_snapshot(self.snapshots, self.workspace, cancel_event=cancel)
        self.assertFalse(self.snapshots.exists())

    def test_incomplete_baseline_never_confuses_an_existing_path_with_an_addition(self):
        early = self.write('a.txt', 'before')
        self.write('z.txt', 'pre-existing file')
        with patch('project_snapshot.FILE_COUNT_LIMIT', 1):
            snapshot = self.start()
            early.unlink()
            self.write('z.txt', 'updated pre-existing file')
            summary = self.finish(snapshot)
            self.assertEqual(summary['status'], 'partial')
            self.assertEqual(summary['files'], ['a.txt'])
            self.assertEqual(self.restore(snapshot)['restored'], ['a.txt'])
        self.assertEqual((self.workspace / 'z.txt').read_text(), 'updated pre-existing file')

    def test_incomplete_after_scan_does_not_treat_oversize_current_file_as_deleted(self):
        self.write('a.txt', 'before')
        self.write('z.txt', 'original')
        with patch('project_snapshot.FILE_LIMIT', 20):
            snapshot = self.start()
            self.write('a.txt', 'x' * 21)
            self.write('z.txt', 'changed')
            summary = self.finish(snapshot)
            self.assertEqual(summary['status'], 'partial')
            self.assertEqual(summary['files'], ['z.txt'])
            self.assertEqual(self.restore(snapshot)['restored'], ['z.txt'])
        self.assertEqual((self.workspace / 'a.txt').read_text(), 'x' * 21)

    def test_combined_limit_and_equal_content_deduplication(self):
        self.write('a.txt', '12345678')
        self.write('b.txt', '12345678')
        with patch('project_snapshot.TOTAL_LIMIT', 12):
            snapshot = self.start()
            self.write('a.txt', 'abcdefgh')
            self.write('b.txt', 'abc')
            summary = self.finish(snapshot)
            self.assertEqual(summary['status'], 'partial')
            self.assertEqual(summary['files'], ['b.txt'])
        with ZipFile(snapshot / 'before.zip') as before, ZipFile(snapshot / 'after.zip') as after:
            sizes = [info.file_size for archive in (before, after) for info in archive.infolist()
                     if info.filename.startswith('files/')]
            self.assertCountEqual(sizes, [8, 3])

    def test_private_binary_model_and_link_paths_are_not_stored(self):
        self.write('.env', 'different-hidden-marker')
        self.write('models/manifest.txt', 'model-marker')
        self.write('small.gguf', 'fake-binary-marker')
        self.write('config.txt', 'api_key="snapshot-fixture-private-value"')
        self.write('safe.txt', 'safe text')
        outside = self.root / 'outside.txt'
        outside.write_text('outside linked content', encoding='utf-8')
        os.link(outside, self.workspace / 'hardlink.txt')
        external_directory = self.root / 'outside-directory'
        external_directory.mkdir()
        (external_directory / 'outside.txt').write_text('outside directory content', encoding='utf-8')
        linked = self.workspace / 'link-dir'
        if os.name == 'nt':
            from _winapi import CreateJunction
            CreateJunction(str(external_directory), str(linked))
        else:
            linked.symlink_to(external_directory, target_is_directory=True)
        snapshot = self.start()
        with ZipFile(snapshot / 'before.zip') as archive:
            manifest = json.loads(archive.read('manifest.json'))['data']
            self.assertEqual(set(manifest['files']), {'safe.txt'})
            entries = [archive.read(name) for name in archive.namelist() if name.startswith('files/')]
        self.assertEqual(entries, [b'safe text'])

    def test_path_tampering_and_workspace_rebinding_stop_restore(self):
        self.write('change.txt', 'before')
        snapshot = self.start()
        self.write('change.txt', 'after')
        self.finish(snapshot)
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(ToolError):
            restore_snapshot(self.snapshots, snapshot, other)
        with self.assertRaises(ToolError):
            self.review(self.snapshots / 'other' / '..' / snapshot.name)

        def move_entry(entries):
            manifest = json.loads(entries['manifest.json'])
            files = manifest['data']['files']
            files['../outside.txt'] = files.pop('change.txt')
            manifest['sha256'] = _sha(_manifest_json(manifest['data']))
            entries['manifest.json'] = _manifest_json(manifest)

        self.rewrite(snapshot / 'after.zip', move_entry)
        with self.assertRaises(ToolError):
            self.restore(snapshot)
        self.assertEqual((self.workspace / 'change.txt').read_text(), 'after')
        self.assertFalse((self.root / 'outside.txt').exists())

    def test_corrupt_content_blocks_all_changes_before_any_restore(self):
        self.write('a.txt', 'old A')
        self.write('b.txt', 'old B')
        snapshot = self.start()
        self.write('a.txt', 'new A')
        self.write('b.txt', 'new B')
        self.finish(snapshot)

        def corrupt(entries):
            entries['files/' + _sha(b'new B')] = b'wrong'

        self.rewrite(snapshot / 'after.zip', corrupt)
        with self.assertRaises(ToolError):
            self.restore(snapshot)
        self.assertEqual((self.workspace / 'a.txt').read_text(), 'new A')
        self.assertEqual((self.workspace / 'b.txt').read_text(), 'new B')


if __name__ == '__main__':
    unittest.main()
