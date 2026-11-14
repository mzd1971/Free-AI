"""Desktop training bridge: validates data, streams a real isolated training process."""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = ('sft_train.jsonl', 'sft_validation.jsonl', 'sft_test.jsonl', 'sft_general_test.jsonl')


def inspect_data(directory):
    directory = Path(directory)
    seen, identities, counts = set(), set(), {}
    for name in FILES:
        rows = [json.loads(line) for line in (directory / name).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        if not rows:
            raise ValueError(f'{name} 不能为空。')
        for row in rows:
            if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key].strip() for key in ('id', 'prompt', 'response')):
                raise ValueError(f'{name} 中每道题都需要编号、问题和参考回答（文件字段：id / prompt / response）。')
            if row['id'] in identities or row['prompt'].strip() in seen:
                raise ValueError('发现重复题号或问题。练习题和检查用的新题不能重复，请修改题目后重试。')
            identities.add(row['id'])
            seen.add(row['prompt'].strip())
        counts[name] = len(rows)
    return counts


def training_command(directory, epochs, rate, initial, agent, stop_file):
    epochs, rate = int(epochs), float(rate)
    if not 1 <= epochs <= 10 or not math.isfinite(rate) or not 0 < rate <= 0.01:
        raise ValueError('学习遍数必须为 1–10，每次调整幅度必须大于 0 且不超过 0.01。')
    inspect_data(directory)
    python = Path(sys.executable).with_name('python.exe') if os.name == 'nt' else Path(sys.executable)
    args = [str(python), '-E', '-s', '-X', 'utf8', '-u', str(ROOT / 'finetune.py'), '--data-dir', str(directory),
            '--epochs', str(epochs), '--learning-rate', str(rate), '--stop-file', str(stop_file)]
    if initial:
        if not (Path(initial) / 'adapter_config.json').is_file():
            raise ValueError('请选择以前训练的学习成果文件夹，里面应有 adapter_config.json 文件。')
        args += ['--initial-adapter', str(initial)]
    if agent:
        args += ['--agent']
    return args


def train_process(command, emit):
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
    summary, cancelled, tail = None, False, []
    with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
        for line in process.stdout:
            tail = (tail + [line.rstrip()])[-20:]
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    event = {'event': 'log', 'text': line.rstrip()}
            except json.JSONDecodeError:
                event = {'event': 'log', 'text': line.rstrip()}
            emit(event)
            if event.get('event') == 'cancelled':
                cancelled = True
            if event.get('completed'):
                summary = event
        code = process.wait()
    if cancelled and code == 2:
        return {'cancelled': True}
    if code != 0 or not summary:
        raise RuntimeError(f'训练进程退出码 {code}，未生成完成摘要。\n' + '\n'.join(tail))
    return summary
