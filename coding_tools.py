"""Open explicitly selected project tools without interpolating shell commands."""
import os
from pathlib import Path
import shutil
import subprocess


EDITORS = ('default', 'vscode', 'custom')
SHELLS = ('powershell', 'cmd')
KEY = 'coding_tools'


def load_preferences(sessions):
    saved = sessions.get(KEY, {})
    if not isinstance(saved, dict):
        saved = {}
    return {'editor': saved.get('editor') if saved.get('editor') in EDITORS else 'default',
            'shell': saved.get('shell') if saved.get('shell') in SHELLS else 'powershell',
            'editor_path': saved.get('editor_path') if isinstance(saved.get('editor_path'), str) else ''}


def _program(value):
    if not isinstance(value, (str, Path)) or not str(value).strip() or '\x00' in str(value):
        raise ValueError('请选择编辑器的 exe 程序。')
    path = Path(value)
    if not path.is_absolute() or path.suffix.casefold() != '.exe' or not path.is_file():
        raise ValueError('请选择存在的 exe 程序，不能填写命令或参数。')
    return path.resolve(strict=True)


def save_preferences(sessions, editor, shell, editor_path):
    if editor not in EDITORS or shell not in SHELLS or not isinstance(editor_path, str):
        raise ValueError('编程工具设置无效。')
    path = str(_program(editor_path)) if editor == 'custom' else editor_path
    settings = {'editor': editor, 'shell': shell, 'editor_path': path}
    sessions.set(KEY, settings)
    return settings


def _project(workspace):
    from project_agent import _workspace

    if not workspace or not isinstance(workspace, (str, Path)):
        raise ValueError('请先在聊天中选择项目文件夹。')
    return _workspace(workspace)


def _vscode():
    candidates = []
    for base, suffix in (('LOCALAPPDATA', 'Programs/Microsoft VS Code/Code.exe'),
                         ('ProgramFiles', 'Microsoft VS Code/Code.exe'),
                         ('ProgramFiles(x86)', 'Microsoft VS Code/Code.exe')):
        if os.environ.get(base):
            candidates.append(Path(os.environ[base]) / suffix)
    located = shutil.which('Code.exe')
    if located:
        candidates.append(Path(located))
    for candidate in candidates:
        if candidate.is_file():
            return _program(candidate)
    raise ValueError('没有找到 Visual Studio Code。请先安装，或选择“自定义程序”。')


def launch_editor(workspace, preferences):
    project = _project(workspace)
    editor = preferences.get('editor')
    if editor == 'default':
        os.startfile(str(project))
        return {'target': str(project), 'tool': '系统默认文件夹程序'}
    if editor not in ('vscode', 'custom'):
        raise ValueError('请选择有效的编辑器。')
    executable = _vscode() if editor == 'vscode' else _program(preferences.get('editor_path'))
    argv = [str(executable), *(['--new-window', '--'] if editor == 'vscode' else []), str(project)]
    subprocess.Popen(argv, cwd=project, shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return {'target': str(project), 'tool': executable.name}


def launch_terminal(workspace, preferences):
    project = _project(workspace)
    shell = preferences.get('shell')
    windows = Path(os.environ.get('SystemRoot', os.environ.get('WINDIR', r'C:\Windows')))
    if shell == 'powershell':
        executable = windows / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        args = ['-NoLogo', '-NoProfile', '-NoExit']
    elif shell == 'cmd':
        executable, args = windows / 'System32/cmd.exe', ['/D']
    else:
        raise ValueError('请选择 PowerShell 或命令提示符。')
    executable = _program(executable)
    subprocess.Popen([str(executable), *args], cwd=project, shell=False,
                     creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
    return {'target': str(project), 'tool': executable.name}
