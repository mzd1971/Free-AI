"""Deterministic evaluation of held-out training comparisons without model dependencies.

Held-out rows can opt in with `evaluation: {"type": "exact"}` or
`evaluation: {"type": "json"}`. Both use the existing response as the
reference. An absent rubric or `{"type": "manual"}` leaves the row unscored.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import unicodedata


ACCEPTANCE_FILE = 'acceptance.json'
MIN_PAIRED_CASES = 5
MIN_COVERAGE = 0.8
MIN_AFTER_SCORE = 0.8
GROUPS = {'domain': '领域题', 'general': '基础能力题'}
_MISSING = object()


def _normalize(text):
    return unicodedata.normalize('NFC', text.replace('\r\n', '\n').replace('\r', '\n')).strip()


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Duplicate JSON key')
    return result


def _reject_constant(value):
    raise ValueError(f'Non-finite JSON number: {value}')


def _parse_answer(text):
    return json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_reject_constant,
                      parse_int=Decimal, parse_float=Decimal)


def _json_equal(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(_json_equal(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(_json_equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _rubric(row):
    response = row.get('response')
    if not isinstance(response, str) or not response.strip():
        return 'invalid_reference', None
    specification = row.get('evaluation', _MISSING)
    if specification is _MISSING:
        return 'manual', None
    if not isinstance(specification, dict) or specification.keys() != {'type'}:
        return 'unsupported', None
    kind = specification['type']
    if kind == 'manual':
        return kind, None
    if kind == 'exact':
        return kind, _normalize(response)
    if kind == 'json':
        try:
            return kind, _parse_answer(response)
        except (ValueError, RecursionError, InvalidOperation):
            return 'invalid_reference', None
    return 'unsupported', None


def _score(result, kind, expected):
    if result is _MISSING:
        return 'missing'
    if (not isinstance(result, dict) or not isinstance(result.get('text'), str)
            or type(result.get('hit_token_limit')) is not bool or type(result.get('new_tokens')) is not int
            or result['new_tokens'] < 0 or (result['text'].strip() and not result['new_tokens'])
            or (result['hit_token_limit'] and not result['new_tokens'])):
        return 'invalid'
    if result['hit_token_limit']:
        return 'token_limit'
    if kind == 'exact':
        return 'passed' if _normalize(result['text']) == expected else 'failed'
    if kind == 'json':
        try:
            return 'passed' if _json_equal(_parse_answer(result['text']), expected) else 'failed'
        except (ValueError, RecursionError, InvalidOperation):
            return 'failed'
    return 'unscored'


def _case_key(row, key):
    value = row.get(key) if isinstance(row, dict) else None
    return _normalize(value) if isinstance(value, str) else ''


def _group_metrics(cases):
    scored = {'passed', 'failed', 'token_limit'}
    paired = [case for case in cases if case['type'] in {'exact', 'json'}
              and case['before'] in scored and case['after'] in scored]
    before_passed = sum(case['before'] == 'passed' for case in paired)
    after_passed = sum(case['after'] == 'passed' for case in paired)
    return {
        'total': len(cases), 'evaluable': len(paired),
        'coverage': len(paired) / len(cases) if cases else 0.0,
        'before_passed': before_passed, 'after_passed': after_passed,
        'before_score': before_passed / len(paired) if paired else None,
        'after_score': after_passed / len(paired) if paired else None,
        'manual': sum(case['type'] == 'manual' for case in cases),
        'unsupported': sum(case['type'] == 'unsupported' for case in cases),
        'invalid_reference': sum(case['type'] == 'invalid_reference' for case in cases),
        'incomplete': sum(case['before'] in {'missing', 'invalid'} or case['after'] in {'missing', 'invalid'}
                          for case in cases),
        'before_cutoff': sum(case['before'] == 'token_limit' for case in cases),
        'after_cutoff': sum(case['after'] == 'token_limit' for case in cases),
    }


def _finite_loss(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def evaluate_comparison(comparison, *, baseline_val_loss=None, best_val_loss=None,
                        expected_test_count=_MISSING, checkpoint_available=True):
    """Compare paired, explicitly scored rows and enforce the acceptance thresholds."""
    reviews, failures = [], []
    if not isinstance(comparison, list):
        reviews.append(('comparison_unavailable', '没有可读取的留出题前后对照。'))
        comparison = []
    if expected_test_count is not _MISSING and (type(expected_test_count) is not int
                                                or expected_test_count != len(comparison)):
        reviews.append(('test_count_mismatch', '留出题对照数量与训练摘要不一致。'))
    ids = Counter(_case_key(row, 'id') for row in comparison)
    prompts = Counter(_case_key(row, 'prompt') for row in comparison)
    grouped_cases = {group: [] for group in GROUPS}
    cases = []
    invalid_rows, duplicate_rows = 0, 0
    for row in comparison:
        identity = _case_key(row, 'id')
        prompt = _case_key(row, 'prompt')
        group = row.get('group') if isinstance(row, dict) else None
        if not identity or not prompt or not isinstance(group, str) or group not in GROUPS:
            invalid_rows += 1
            continue
        if ids[identity] > 1 or prompts[prompt] > 1:
            duplicate_rows += 1
            continue
        kind, expected = _rubric(row)
        case = {'id': identity, 'group': group, 'type': kind,
                'before': _score(row.get('before', _MISSING), kind, expected),
                'after': _score(row.get('after', _MISSING), kind, expected)}
        grouped_cases[group].append(case)
        cases.append(case)
    if invalid_rows:
        reviews.append(('invalid_rows', f'有 {invalid_rows} 道留出题的题号、问题或分组无效。'))
    if duplicate_rows:
        reviews.append(('duplicate_rows', f'有 {duplicate_rows} 道留出题的题号或问题重复。'))

    baseline_loss = _finite_loss(baseline_val_loss)
    best_loss = _finite_loss(best_val_loss)
    if not checkpoint_available:
        failures.append(('checkpoint_unavailable', '没有选出可供验收的检查点。'))
    if baseline_loss is None or best_loss is None:
        reviews.append(('validation_unavailable', '训练前和选中检查点都需要有限的验证损失。'))
    elif best_loss > baseline_loss:
        failures.append(('validation_regression', '选中检查点的验证损失高于训练前。'))

    metrics = {group: _group_metrics(grouped_cases[group]) for group in GROUPS}
    for group, label in GROUPS.items():
        result = metrics[group]
        if result['incomplete']:
            reviews.append((f'{group}_incomplete', f'{label}有缺失或无效的生成结果。'))
        if result['unsupported'] or result['invalid_reference']:
            reviews.append((f'{group}_invalid_rubric', f'{label}有无法识别的评测规则或无效的参考回答。'))
        if result['evaluable'] < MIN_PAIRED_CASES or result['coverage'] < MIN_COVERAGE:
            reviews.append((f'{group}_insufficient_coverage',
                            f'{label}需至少 {MIN_PAIRED_CASES} 道前后均可评分的题，且覆盖至少 {MIN_COVERAGE:.0%}。'))
            continue
        if result['after_passed'] < result['before_passed']:
            failures.append((f'{group}_regression', f'{label}训练后通过数低于训练前。'))
        if result['after_score'] < MIN_AFTER_SCORE:
            failures.append((f'{group}_below_target', f'{label}训练后通过率低于 {MIN_AFTER_SCORE:.0%}。'))

    reasons = failures + reviews
    return {
        'schema_version': 1,
        'status': 'failed' if failures else 'needs_review' if reviews else 'passed',
        'reason_codes': [code for code, _ in reasons], 'reasons': [message for _, message in reasons],
        'thresholds': {'min_paired_cases': MIN_PAIRED_CASES, 'min_coverage': MIN_COVERAGE,
                       'min_after_score': MIN_AFTER_SCORE},
        'metrics': metrics, 'validation': {'baseline_loss': baseline_loss, 'best_loss': best_loss},
        'invalid_rows': invalid_rows, 'duplicate_rows': duplicate_rows, 'cases': cases,
    }


def _read_json(path):
    try:
        data = path.read_bytes()
        return json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_keys), data
    except (OSError, ValueError, RecursionError):
        return None, None


def _require_review(report, code, reason):
    report['status'] = 'needs_review'
    report['reason_codes'].insert(0, code)
    report['reasons'].insert(0, reason)
    return report


def evaluate_run(directory):
    """Recompute acceptance from saved training evidence, ignoring any cached report."""
    directory = Path(directory)
    summary, _ = _read_json(directory / 'summary.json')
    comparison, comparison_bytes = _read_json(directory / 'comparison.json')
    if not isinstance(summary, dict) or summary.get('completed') is not True:
        return _require_review(evaluate_comparison(comparison), 'run_incomplete', '没有可读取的训练完成摘要。')
    best_epoch = summary.get('best_epoch')
    report = evaluate_comparison(
        comparison, baseline_val_loss=summary.get('baseline_validation_loss'),
        best_val_loss=summary.get('best_validation_loss'), expected_test_count=summary.get('test_count'),
        checkpoint_available=not (type(best_epoch) is int and best_epoch == 0),
    )
    if type(best_epoch) is not int or best_epoch < 0:
        return _require_review(report, 'checkpoint_unverified', '训练完成摘要中缺少有效的检查点轮次。')
    if 'publication_inputs' in summary and comparison_bytes is not None:
        inputs = summary['publication_inputs']
        digest = hashlib.sha256(comparison_bytes).hexdigest()
        if not isinstance(inputs, dict) or inputs.get('comparison.json') != digest:
            return _require_review(report, 'comparison_changed', '留出题前后对照与训练完成时保存的内容不一致。')
    return report
