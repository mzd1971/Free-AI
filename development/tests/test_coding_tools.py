"""Project locations remain argv data, with no command-string interpolation."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_state import Sessions
import coding_tools


ROOT = Path(__file__).resolve().parents[2]


class CodingToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / 'development')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.project = self.folder / '中文 & project ; $(data)'
        self.project.mkdir()
        self.editor = self.folder / 'editor.exe'
        self.editor.write_bytes(b'fixture, never launched')
        self.sessions = Sessions(self.folder / 'settings')

    def test_preferences_persist_without_opening_tools(self):
        with patch('coding_tools.subprocess.Popen') as launch, patch('coding_tools.os.startfile') as open_folder:
            self.assertEqual(coding_tools.load_preferences(self.sessions)['editor'], 'default')
            expected = coding_tools.save_preferences(self.sessions, 'custom', 'cmd', str(self.editor))
            self.assertEqual(coding_tools.load_preferences(Sessions(self.folder / 'settings')), expected)
            launch.assert_not_called()
            open_folder.assert_not_called()

    def test_invalid_custom_program_never_overwrites_preferences(self):
        original = coding_tools.save_preferences(self.sessions, 'default', 'powershell', '')
        for value in ('editor.exe', str(self.editor) + ' --flag', str(self.folder), str(self.folder / 'test.cmd')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                coding_tools.save_preferences(self.sessions, 'custom', 'cmd', value)
        self.assertEqual(coding_tools.load_preferences(self.sessions), original)

    def test_custom_editor_receives_literal_directory_argument(self):
        with patch('coding_tools.subprocess.Popen') as launch:
            coding_tools.launch_editor(str(self.project), {'editor': 'custom', 'editor_path': str(self.editor)})
        args, options = launch.call_args
        self.assertEqual(args[0], [str(self.editor), str(self.project)])
        self.assertEqual(options['cwd'], self.project)
        self.assertFalse(options['shell'])

    def test_vscode_resolution_uses_executable_instead_of_batch_script(self):
        base = self.folder / 'local'
        exe = base / 'Programs/Microsoft VS Code/Code.exe'
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b'fixture')
        with patch.dict(os.environ, {'LOCALAPPDATA': str(base)}), \
                patch('coding_tools.subprocess.Popen') as launch:
            coding_tools.launch_editor(self.project, {'editor': 'vscode'})
        self.assertEqual(launch.call_args.args[0], [str(exe), '--new-window', '--', str(self.project)])

    def test_terminal_uses_cwd_without_embedding_it_in_shell_source(self):
        for shell in ('powershell', 'cmd'):
            with self.subTest(shell=shell), patch('coding_tools.subprocess.Popen') as launch:
                coding_tools.launch_terminal(self.project, {'shell': shell})
                argv = launch.call_args.args[0]
                self.assertTrue(Path(argv[0]).is_file())
                self.assertEqual(launch.call_args.kwargs['cwd'], self.project)
                self.assertNotIn(str(self.project), ' '.join(argv))
                self.assertFalse(launch.call_args.kwargs['shell'])

    def test_default_opens_existing_directory_and_missing_project_does_not_launch(self):
        with patch('coding_tools.os.startfile') as open_folder, patch('coding_tools.subprocess.Popen') as launch:
            coding_tools.launch_editor(self.project, {'editor': 'default'})
            open_folder.assert_called_once_with(str(self.project))
            with self.assertRaises(ValueError):
                coding_tools.launch_terminal(self.folder / 'missing', {'shell': 'cmd'})
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
