"""Verify Hugging Face data conversion and persistence without network access."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from urllib.request import Request

from hf_dataset_catalog import recommend_datasets
import hf_datasets as hf
from training_desktop import FILES, inspect_data


REPO = 'sample/TextDataset'
CONFIG = 'default'
MAPPING = {'prompt': 'instruction', 'input': 'input', 'response': 'output'}
REPO_INFO = {'id': REPO, 'sha': 'a' * 40, 'tags': ['license:mit']}


def _source_row(index, **changes):
    row = {'instruction': f'Prompt {index}', 'input': '', 'output': f'Answer {index}'}
    row.update(changes)
    return {'row_idx': index, 'row': row, 'truncated_cells': []}


def _page(rows, total=None, columns=MAPPING.values()):
    return {
        'features': [{'name': name} for name in columns], 'rows': rows,
        'num_rows_total': total,
    }


class HuggingFaceDatasetsTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix='hf-dataset-test-', dir=Path(__file__).resolve().parents[1])
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.requests = []

    def _mock_import(self, rows, columns=MAPPING.values(), repo=REPO_INFO):
        def endpoint(base, path, params, cancel):
            self.requests.append((base, path, params))
            if path == f'/api/datasets/{REPO}':
                self.assertEqual(base, hf._HUB)
                return repo
            self.assertEqual((base, path), (hf._VIEWER, '/rows'))
            self.assertEqual(params['dataset'], REPO)
            self.assertEqual(params['config'], CONFIG)
            self.assertEqual(params['split'], 'train')
            offset, length = params['offset'], params['length']
            return _page(rows[offset:offset + length], len(rows), columns)
        mock = patch.object(hf, '_get_json', side_effect=endpoint)
        self.addCleanup(mock.stop)
        mock.start()

    def _import(self, count=20, mapping=MAPPING, **options):
        return hf.import_dataset(REPO, CONFIG, 'train', count, mapping, output_root=self.root, **options)

    def test_search_filters_inaccessible_and_invalid_repositories(self):
        items = [
            {**REPO_INFO, 'downloads': 12, 'likes': 4},
            {'id': 'sample/Private', 'private': True},
            {'id': 'sample/Gated', 'gated': 'auto'},
            {'id': 'sample/Disabled', 'disabled': True},
            {'id': REPO.lower()},
            {'id': 'sample/../unsafe'},
        ]
        with patch.object(hf, '_get_json', return_value=items) as get_json:
            results = hf.search_datasets('  TextDataset &  ', limit=10)
        self.assertEqual(results, [{'id': REPO, 'license': 'mit', 'downloads': 12, 'likes': 4}])
        requests = [call.args for call in get_json.call_args_list]
        self.assertEqual(
            [(base, path, params['search'], params['limit']) for base, path, params, _ in requests],
            [(hf._HUB, '/api/datasets', 'TextDataset &', 10), (hf._HUB, '/api/datasets', 'textdataset', 10)],
        )

    def test_search_corrects_typo_ranks_matches_and_queries_beyond_recommendations(self):
        known = 'sahil2801/CodeAlpaca-20k'
        exact = 'community/CodeAlpaca'
        unrelated = 'tatsu-lab/alpaca'
        expanded = [
            {'id': unrelated, 'downloads': 500_000},
            {'id': 'samples/evol-codealpaca-v1', 'downloads': 20_000},
            {'id': exact, 'downloads': 10},
            {'id': known, 'downloads': 1_000, 'tags': ['license:cc-by-4.0']},
        ]
        with patch.object(hf, '_get_json', side_effect=[[], expanded, [
            {'id': known.upper()}, {'id': known},
        ]]) as get_json:
            results = hf.search_datasets('code alpca', limit=5)
        self.assertEqual([row['id'] for row in results], [exact, known, 'samples/evol-codealpaca-v1'])
        self.assertEqual(results[1]['title'], '代码编写练习')
        self.assertEqual(results[1]['license'], 'cc-by-4.0')
        self.assertEqual([call.args[2]['search'] for call in get_json.call_args_list],
                         ['code alpca', 'code alpaca', known])

    def test_search_combines_chinese_topics_and_filters_non_text_candidates(self):
        zh = 'llamafactory/alpaca_gpt4_zh'
        qa = 'sample/ChineseQA'
        tags = ['language:zh', 'task_categories:question-answering']
        remote = [
            {'id': 'sample/ChAudio', 'tags': [*tags, 'modality:audio'], 'downloads': 1_000_000},
            {'id': 'sample/Novel', 'tags': ['language:zh'], 'downloads': 1_000},
            {'id': qa, 'tags': tags},
        ]
        with patch.object(hf, '_get_json', side_effect=[[], remote, [{'id': zh}]]) as get_json:
            results = hf.search_datasets('中文问答')
        self.assertEqual([row['id'] for row in results], [zh, qa])
        self.assertEqual(get_json.call_args_list[1].args[2]['filter'],
                         ['language:zh', 'task_categories:question-answering'])

    def test_search_ranks_curated_math_dataset_ahead_of_keyword_matches(self):
        gsm = 'openai/gsm8k'
        matches = [{'id': f'sample{i}/MATH', 'downloads': 1_000_000 - i} for i in range(20)]
        with patch.object(hf, '_get_json', side_effect=[[], matches, [{'id': gsm}]]) as get_json:
            results = hf.search_datasets('数学')
        self.assertEqual(len(results), 20)
        self.assertEqual([row['id'] for row in results[:2]], [gsm, 'sample0/MATH'])
        self.assertEqual(get_json.call_count, 3)

    def test_search_requires_all_terms_and_bounds_requests_and_results(self):
        popular = [{'id': 'sample/general', 'downloads': 1_000_000}]
        with patch.object(hf, '_get_json', return_value=popular) as get_json:
            self.assertEqual(hf.search_datasets('unseen-phrase_gzvrux rabbit', limit=2), [])
        self.assertEqual(get_json.call_count, 3)
        for call in get_json.call_args_list:
            self.assertEqual(call.args[2]['limit'], 2)

        matches = [{'id': name} for name in ('sample/TinyStories', 'other/TinyStories', 'other/TinyStoriesV2')]
        with patch.object(hf, '_get_json', return_value=matches) as get_json:
            results = hf.search_datasets('Tiny Stories', limit=2)
        self.assertEqual([row['id'] for row in results], ['sample/TinyStories', 'other/TinyStories'])
        self.assertEqual(get_json.call_count, 3)

    def test_search_stops_after_cancellation_or_network_error(self):
        cancel = Event()

        def cancel_during_search(*args):
            cancel.set()
            return []

        with patch.object(hf, '_get_json', side_effect=cancel_during_search) as get_json:
            with self.assertRaises(hf.DatasetCancelled):
                hf.search_datasets('code alpca', cancel=cancel)
        get_json.assert_called_once()

        error = hf.DatasetError('offline')
        with patch.object(hf, '_get_json', side_effect=[[], error]) as get_json:
            with self.assertRaisesRegex(hf.DatasetError, 'offline'):
                hf.search_datasets('中文')
        self.assertEqual(get_json.call_count, 2)

    def test_offline_recommendations_accept_aliases_typo_and_name_fragments(self):
        examples = {
            '中文': 'llamafactory/alpaca_gpt4_zh',
            '问答': 'llamafactory/alpaca_gpt4_zh',
            'creative_writing': 'llamafactory/alpaca_gpt4_zh',
            '代码': 'sahil2801/CodeAlpaca-20k',
            '数学': 'openai/gsm8k',
            '人工对话': 'HuggingFaceH4/no_robots',
            'human-chat': 'HuggingFaceH4/no_robots',
            'code alpca': 'sahil2801/CodeAlpaca-20k',
            ' ALPACA__CLEANED ': 'yahma/alpaca-cleaned',
            'alpaca gpt4 zh': 'llamafactory/alpaca_gpt4_zh',
            'Belle Group': 'BelleGroup/train_0.5M_CN',
            '入门': 'yahma/alpaca-cleaned',
        }
        with patch.object(hf, '_get_json') as get_json:
            self.assertEqual(len(recommend_datasets()), 6)
            self.assertEqual(len(recommend_datasets('中文')), 2)
            self.assertEqual(len(recommend_datasets('英文')), 4)
            self.assertEqual(recommend_datasets('unsuitable_zzzxqx'), [])
            for query, expected in examples.items():
                with self.subTest(query=query):
                    self.assertEqual(recommend_datasets(query, limit=1)[0]['id'], expected)
        get_json.assert_not_called()

    def test_viewer_splits_and_preview_report_truncated_cells(self):
        splits = {'splits': [
            {'config': 'a', 'split': 'test'},
            {'config': 'a', 'split': 'train'},
            {'config': 'a', 'split': 'train'},
            {'config': None, 'split': 'train'},
        ]}
        source = _source_row(0)
        source['truncated_cells'] = ['output']
        with patch.object(hf, '_get_json', side_effect=[splits, _page([source], 51)]) as get_json:
            self.assertEqual(hf.dataset_splits(REPO), [{'config': 'a', 'split': 'test'}, {'config': 'a', 'split': 'train'}])
            preview = hf.preview_rows(REPO, 'a', 'train')
        self.assertEqual(preview['columns'], list(MAPPING.values()))
        self.assertEqual(preview['rows'], [source['row']])
        self.assertEqual(preview['truncated_cells'], [['output']])
        self.assertEqual(preview['total'], 51)
        self.assertEqual(get_json.call_args.args[2]['length'], 5)

    def test_import_preserves_exact_split_data_and_provenance(self):
        first_general = json.loads(hf._GENERAL_FILE.read_text(encoding='utf-8').splitlines()[0])
        rows = [
            _source_row(0, instruction=' Ｆｉｒｓｔ　prompt ', output=' Response '),
            _source_row(1, instruction='First prompt', output='Response'),
            _source_row(2, instruction='First\t prompt', output='Different response'),
            _source_row(3, output={'text': 'non-text value'}),
            _source_row(4, instruction='Title', input='Extra\u2028line'),
            _source_row(5, instruction=first_general['prompt']),
            *(_source_row(i) for i in range(6, 117)),
        ]
        rows[6]['truncated_cells'] = ['irrelevant_column']
        self._mock_import(rows)
        progress = []
        result = self._import(105, progress=lambda seen, count: progress.append((seen, count)))
        self.assertEqual(progress, [(100, 105), (105, 105)])
        self.assertEqual(result['fetched'], 105)
        self.assertEqual(result['counts'], {'train': 81, 'validation': 10, 'test': 10, 'general_test': 5})
        self.assertEqual(result['skipped'], {'invalid': 1, 'truncated': 1, 'duplicate': 1, 'conflicting': 1})
        self.assertEqual(inspect_data(result['path']), dict(zip(FILES, (81, 10, 10, 5))))
        train, validation, test, general = [
            [json.loads(line) for line in (result['path'] / file).read_text(encoding='utf-8').splitlines()]
            for file in FILES
        ]
        self.assertIn('Title\n\nExtra\u2028line', {row['prompt'] for row in train + validation + test})
        self.assertNotIn(first_general['prompt'], {row['prompt'] for row in general})
        report = json.loads(result['report'].read_text(encoding='utf-8'))
        self.assertEqual(report['source']['license'], 'mit')
        self.assertEqual(report['source']['hub_revision'], REPO_INFO['sha'])
        self.assertFalse(report['source']['viewer_revision_pinned'])
        self.assertEqual(report['general_test']['overlaps_excluded'], 1)
        for name, metadata in report['files'].items():
            self.assertEqual(metadata['sha256'], hashlib.sha256((result['path'] / name).read_bytes()).hexdigest())

        repeat = self._import(105)
        self.assertNotEqual(result['path'], repeat['path'])
        for name in FILES:
            self.assertEqual((result['path'] / name).read_bytes(), (repeat['path'] / name).read_bytes())

    def test_chat_mapping_keeps_history_and_skips_bad_roles_or_media(self):
        def chat(index, turns):
            return {'row_idx': index, 'row': {'messages': turns}, 'truncated_cells': []}
        turns = [
            {'role': 'system', 'content': 'Be precise.'},
            {'role': 'user', 'content': 'Question'},
            {'role': 'assistant', 'content': 'Earlier reply'},
            {'role': 'user', 'content': 'Follow-up'},
            {'role': 'assistant', 'content': 'Final answer'},
        ]
        rows = [
            chat(0, turns),
            chat(1, [{'role': 'user', 'content': 'Simple question'}, {'role': 'assistant', 'content': 'Reply'}]),
            chat(2, [{'role': 'user', 'content': 'Other question'}, {'role': 'assistant', 'content': 'Other reply'}]),
            chat(3, [*turns[:-1], {'role': 'assistant', 'content': [{'type': 'text', 'text': 'mixed'}]}]),
            chat(4, [*turns[:-1], {'role': 'tool', 'content': 'Tool result'}]),
        ]
        self._mock_import(rows, columns=['messages'])
        result = self._import(mapping={'messages': 'messages'})
        self.assertEqual(result['fetched'], 5)
        self.assertEqual(result['skipped']['invalid'], 2)
        accepted = [
            json.loads((result['path'] / file).read_text(encoding='utf-8'))
            for file in FILES[:-1]
        ]
        known = next(row for row in accepted if row['id'] == 'hf_000000')
        self.assertEqual(known['prompt'], 'system: Be precise.\n\nuser: Question\n\nassistant: Earlier reply\n\nuser: Follow-up')
        self.assertEqual(known['response'], 'Final answer')

    def test_cancellation_during_final_write_removes_only_pending_import(self):
        self._mock_import([_source_row(i) for i in range(20)])
        cancel = Event()
        prior = self.root / 'prior-import'
        prior.mkdir()
        marker = prior / 'owned-by-user.txt'
        marker.write_text('keep', encoding='utf-8')

        def cancel_before_publication(path):
            inspect_data(path)
            cancel.set()
        with patch.object(hf, 'inspect_data', side_effect=cancel_before_publication):
            with self.assertRaises(hf.DatasetCancelled):
                self._import(cancel=cancel)
        self.assertEqual(list(self.root.iterdir()), [prior])
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')

    def test_cancellation_before_network_and_nontrain_split_never_send_request(self):
        cancel = Event()
        cancel.set()
        with patch.object(hf, 'build_opener') as opener:
            with self.assertRaises(hf.DatasetCancelled):
                hf.preview_rows(REPO, CONFIG, 'train', cancel=cancel)
            with self.assertRaises(hf.DatasetError):
                hf.import_dataset(REPO, CONFIG, 'test', 20, MAPPING, output_root=self.root)
        opener.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_body_size_and_cross_host_redirect_are_rejected(self):
        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *ignored):
                return False

            def read1(self, size):
                return b'x' * size

        with patch.object(hf, '_API_LIMIT', 8), patch.object(hf, 'build_opener') as opener:
            opener.return_value.open.return_value = OversizedResponse()
            with self.assertRaises(hf.DatasetError):
                hf.search_datasets('text')
        request = Request(f'https://huggingface.co/api/datasets/{REPO}')
        with self.assertRaises(hf.DatasetError):
            hf._DatasetRedirect().redirect_request(
                request, None, 302, '', {}, 'https://example.com/api/datasets/other'
            )


if __name__ == '__main__':
    unittest.main()
