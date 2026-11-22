import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from training_evaluation import ACCEPTANCE_FILE, evaluate_comparison, evaluate_run


def generated(text, *, cutoff=False):
    return {'text': text, 'new_tokens': 20, 'hit_token_limit': cutoff}


def comparison_rows(count=5):
    return [
        {'id': f'{group}_{index}', 'group': group, 'prompt': f'{group} question {index}',
         'response': 'café\n42', 'evaluation': {'type': 'exact'},
         'before': generated('wrong'), 'after': generated('  cafe\u0301\r\n42  ')}
        for group in ('domain', 'general') for index in range(count)
    ]


class TrainingEvaluationTests(unittest.TestCase):
    def score(self, rows, **kwargs):
        return evaluate_comparison(rows, **{'baseline_val_loss': 2.0, 'best_val_loss': 1.0, **kwargs})

    def test_paired_normalized_exact_answers_pass(self):
        rows = comparison_rows()
        result = self.score(rows)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['reasons'], [])
        self.assertEqual(result['metrics']['domain']['before_score'], 0.0)
        self.assertEqual(result['metrics']['domain']['after_score'], 1.0)
        self.assertEqual(result['metrics']['general']['evaluable'], 5)

        rows[0]['after'] = generated('CAFÉ\n42')
        rows[1]['after'] = generated('café\n 42')
        result = self.score(rows)
        self.assertEqual(result['metrics']['domain']['after_passed'], 3)
        self.assertIn('domain_below_target', result['reason_codes'])

    def test_freeform_is_manual_and_short_holdouts_do_not_pass(self):
        rows = comparison_rows()
        for row in rows:
            del row['evaluation']
            row['before'] = row['after'] = generated(row['response'])
        result = self.score(rows)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['metrics']['domain']['manual'], 5)
        self.assertIsNone(result['metrics']['domain']['after_score'])
        self.assertIn('general_insufficient_coverage', result['reason_codes'])
        self.assertEqual(self.score(comparison_rows(4))['status'], 'needs_review')

    def test_manual_coverage_and_explicit_human_review(self):
        rows = comparison_rows(6)
        rows[0]['evaluation'] = {'type': 'manual'}
        result = self.score(rows)
        self.assertAlmostEqual(result['metrics']['domain']['coverage'], 5 / 6)
        self.assertEqual(result['status'], 'passed')
        rows.append(dict(rows[0], id='extra', prompt='another manual domain question'))
        self.assertIn('domain_insufficient_coverage', self.score(rows)['reason_codes'])

    def test_json_scores_structure_and_rejects_ambiguous_output(self):
        rows = comparison_rows()
        cases = (
            ('{"order": [1, 2], "ok": true}', ' {"ok": true, "order": [1.0, 2]} ', 'passed'),
            ('{"ok": true}', '{"ok": 1}', 'failed'),
            ('{"ok": true}', '{"ok": false, "ok": true}', 'failed'),
            ('{"ok": true}', '```json\n{"ok": true}\n```', 'failed'),
            ('{"n": 1}', '{"n": NaN}', 'failed'),
        )
        for row, (reference, output, _) in zip(rows, cases):
            row.update(response=reference, evaluation={'type': 'json'}, after=generated(output))
        result = self.score(rows)
        self.assertEqual([case['after'] for case in result['cases'][:5]], [case[2] for case in cases])

    def test_poisoned_or_malformed_rubrics_require_review(self):
        rows = comparison_rows(6)
        rows[0]['evaluation'] = {'type': 'exact', 'script': 'raise RuntimeError("executed")'}
        result = self.score(rows)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['metrics']['domain']['unsupported'], 1)
        self.assertIn('domain_invalid_rubric', result['reason_codes'])

        rows[0].update(evaluation={'type': 'json'}, response='{"value": 0, "value": 1}')
        self.assertEqual(self.score(rows)['metrics']['domain']['invalid_reference'], 1)

    def test_each_cutoff_is_scored_as_failure_and_regression_is_blocked(self):
        rows = comparison_rows()
        rows[0]['before'] = generated(rows[0]['response'])
        rows[0]['after'] = generated(rows[0]['response'], cutoff=True)
        for row in rows[1:5]:
            row['before'] = generated(row['response'])
        result = self.score(rows)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['cases'][0]['after'], 'token_limit')
        self.assertEqual(result['metrics']['domain']['after_score'], 0.8)
        self.assertEqual(result['metrics']['domain']['after_cutoff'], 1)
        self.assertIn('domain_regression', result['reason_codes'])

    def test_missing_or_malformed_results_block_acceptance_even_with_coverage(self):
        rows = comparison_rows(6)
        del rows[0]['after']
        rows[6]['after'] = dict(rows[6]['after'], hit_token_limit='false')
        result = self.score(rows)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['metrics']['domain']['evaluable'], 5)
        self.assertEqual(result['cases'][0]['after'], 'missing')
        self.assertEqual(result['cases'][6]['after'], 'invalid')
        self.assertIn('domain_incomplete', result['reason_codes'])
        self.assertIn('general_incomplete', result['reason_codes'])
        self.assertEqual(self.score([])['status'], 'needs_review')

    def test_duplicate_ids_and_prompts_cannot_inflate_counts(self):
        rows = comparison_rows(7)
        rows[0]['id'] = f' {rows[7]["id"]} '
        rows[1]['prompt'] = rows[8]['prompt']
        rows.append({'id': 'bad-group', 'prompt': 'other', 'group': ['general']})
        result = self.score(rows)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['duplicate_rows'], 4)
        self.assertEqual(result['invalid_rows'], 1)
        self.assertEqual(result['metrics']['domain']['evaluable'], 5)
        self.assertEqual(result['metrics']['general']['evaluable'], 5)

    def test_validation_outcomes_cannot_replace_held_out_results(self):
        rows = comparison_rows()
        for loss in (None, float('nan'), float('inf'), -1, True, '1.0'):
            with self.subTest(loss=loss):
                result = self.score(rows, baseline_val_loss=loss)
                self.assertEqual(result['status'], 'needs_review')
                self.assertIsNone(result['validation']['baseline_loss'])
        self.assertIn('validation_regression', self.score(rows, best_val_loss=2.1)['reason_codes'])
        self.assertIn('checkpoint_unavailable', self.score(rows, checkpoint_available=False)['reason_codes'])

    def test_run_recomputes_evidence_independently_of_cached_acceptance(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            directory = Path(temporary)
            comparison_path = directory / 'comparison.json'
            summary_path = directory / 'summary.json'
            rows = comparison_rows()
            summary = {'completed': True, 'test_count': len(rows), 'best_epoch': 1,
                       'baseline_validation_loss': 2.0, 'best_validation_loss': 1.0}
            (directory / ACCEPTANCE_FILE).write_text('{"status":"passed"}', encoding='utf-8')
            self.assertIn('run_incomplete', evaluate_run(directory)['reason_codes'])
            summary_path.write_text(json.dumps(summary), encoding='utf-8')
            self.assertIn('comparison_unavailable', evaluate_run(directory)['reason_codes'])
            comparison_path.write_text(json.dumps(rows), encoding='utf-8')
            self.assertEqual(evaluate_run(directory)['status'], 'passed')

            sealed = {'comparison.json': hashlib.sha256(comparison_path.read_bytes()).hexdigest()}
            summary_path.write_text(json.dumps(dict(summary, publication_inputs=sealed)), encoding='utf-8')
            self.assertEqual(evaluate_run(directory)['status'], 'passed')
            comparison_path.write_text(json.dumps(rows, indent=2), encoding='utf-8')
            self.assertIn('comparison_changed', evaluate_run(directory)['reason_codes'])

            summary_path.write_text(json.dumps(dict(summary, test_count=None)), encoding='utf-8')
            self.assertIn('test_count_mismatch', evaluate_run(directory)['reason_codes'])
            summary_path.write_text(json.dumps(summary), encoding='utf-8')
            del rows[0]['after']
            comparison_path.write_text(json.dumps(rows), encoding='utf-8')
            self.assertEqual(evaluate_run(directory)['status'], 'needs_review')
            comparison_path.write_text('[{"id":"red","id":"blue"}]', encoding='utf-8')
            self.assertIn('comparison_unavailable', evaluate_run(directory)['reason_codes'])


if __name__ == '__main__':
    unittest.main()
