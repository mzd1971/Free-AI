"""Recall and record local LoRA runs using the assistant's durable memory."""

import hashlib
from pathlib import Path
import re

from agent_memory import AgentMemoryStore
from training_evaluation import ACCEPTANCE_FILE, GROUPS, evaluate_run


MANIFEST_FILE = 'training_memory.json'
_TITLE_PREFIX = 'LoRA 训练记录 '
_EVIDENCE_PREFIX = '由 Free AI 本地训练进程记录\n'
_OUTCOME_LABELS = {
    'cancelled': '训练已取消。训练未完成，不能判断效果。',
    'failed': '训练执行失败。训练未完成，不能判断效果。',
    'passed': '训练已完成，验收通过。',
    'rejected': '训练已完成，验收未通过。',
    'needs_review': '训练已完成，验收需要核实。',
}
_INVALID_EVIDENCE = frozenset({
    'checkpoint_unverified', 'comparison_changed', 'comparison_unavailable', 'test_count_mismatch',
})


def _smoke_run(directory):
    return directory.name.startswith('desktop-smoke-')


def _run_id(directory):
    name = directory.name
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', name):
        return name
    return 'run-' + hashlib.sha256(name.encode('utf-8', errors='surrogatepass')).hexdigest()[:16]


def _is_training_memory(row):
    return (row['source'] == 'agent' and row['origin'] == 'training'
            and row['kind'] == 'note' and row['status'] == 'active'
            and row['title'].startswith(_TITLE_PREFIX) and row['evidence'].startswith(_EVIDENCE_PREFIX))


def recall_training_memory(run_dir, store=None):
    """Select prior run summaries only, for the training manifest and log."""
    directory = Path(run_dir)
    if _smoke_run(directory):
        return []
    store = store if store is not None else AgentMemoryStore()
    current_title = _TITLE_PREFIX + _run_id(directory)
    prior = {row['id']: row for row in store.list(include_disabled=False, include_archived=False)
             if _is_training_memory(row) and row['title'] != current_title}
    if not prior:
        return []
    recalled = store.before_call('LoRA 训练记录 验收 验证损失 留出题', budget=2200, allowed_ids=set(prior))
    return [{key: prior[identity][key] for key in ('id', 'title', 'content', 'evidence')}
            for identity in recalled['memory_ids'][:3]]


def _completed_content(report):
    if _INVALID_EVIDENCE.intersection(report['reason_codes']):
        return 'needs_review', '保存的训练证据无法完整复核。'
    status = 'rejected' if report['status'] == 'failed' else report['status']
    details = []
    baseline = report['validation']['baseline_loss']
    best = report['validation']['best_loss']
    if baseline is not None and best is not None:
        details.append(f'验证损失 {baseline:.6g} → {best:.6g}。')
    for group, label in GROUPS.items():
        stats = report['metrics'][group]
        count = stats['evaluable']
        if count:
            details.append(f"{label}通过数 {stats['before_passed']}/{count} → {stats['after_passed']}/{count}。")
    details.extend(report['reasons'][:2])
    details.append('验收结论仅适用于本次验证集和留出题。')
    return status, ''.join(details)


def record_training_memory(run_dir, outcome='completed', store=None):
    """Persist a bounded outcome with references, leaving training weights untouched."""
    directory = Path(run_dir)
    if _smoke_run(directory) or not directory.is_dir():
        return None
    if outcome not in {'completed', 'failed', 'cancelled'}:
        raise ValueError('未知的训练运行状态。')
    if outcome == 'completed':
        report = evaluate_run(directory)
        if 'run_incomplete' in report['reason_codes']:
            return None
        status, details = _completed_content(report)
        files = ('summary.json', 'comparison.json', ACCEPTANCE_FILE)
    else:
        if (directory / 'summary.json').is_file():
            return None
        status, details = outcome, ''
        files = (MANIFEST_FILE,)
    run_id = _run_id(directory)
    reference = '\n'.join(f'outputs/{run_id}/{name}' for name in files if (directory / name).is_file())
    store = store if store is not None else AgentMemoryStore()
    row = store.remember_training(
        _TITLE_PREFIX + run_id,
        f'运行 {run_id}。{_OUTCOME_LABELS[status]}{details}',
        evidence=_EVIDENCE_PREFIX + (reference or f'训练运行 {run_id}'),
        importance=0.75 if status in {'failed', 'rejected'} else 0.6,
    )
    return row if _is_training_memory(row) else None
