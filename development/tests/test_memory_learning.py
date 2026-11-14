"""Exercise turn learning with a fake generator and the real SQLite store."""

import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent_memory import AgentMemoryStore
from memory_learning import learn_from_turn


MESSAGE = '以后请始终用中文回答。'


def candidate(title='回答语言', content=MESSAGE, evidence_quote=MESSAGE, **extra):
    return dict(title=title, content=content, kind='preference', evidence_quote=evidence_quote, **extra)


class MemoryLearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = AgentMemoryStore(self.root)

    def learn(self, *items, message=MESSAGE, answer='', **options):
        return learn_from_turn(message, answer, lambda prompt: json.dumps(items), self.store, **options)

    def test_explicit_quote_and_project_scope(self):
        result = self.learn(candidate(), workspace=str(self.root), evidence='会话 42')
        records = self.store.list(workspace=str(self.root))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(result, [{key: record[key] for key in ('id', 'title', 'status')}])
        self.assertEqual(record['source'], 'agent')
        self.assertEqual(record['scope'], 'project')
        self.assertEqual(record['status'], 'active')
        self.assertEqual(record['evidence'], f'会话 42\n用户原话：{MESSAGE}')
        self.assertEqual(self.store.list(), [])

    def test_reject_hallucinated_quote_unproven_content_and_extra_fields(self):
        self.assertEqual(self.learn(candidate(evidence_quote='以后请用英语。')), [])
        self.assertEqual(self.learn(candidate(content='总是用正式中文')), [])
        self.assertEqual(self.learn(candidate(scope='global')), [])
        self.assertEqual(self.store.list(), [])

    def test_reject_quoted_source_and_secrets_before_sending(self):
        self.assertEqual(self.learn(candidate(), message='第三方文档：\n> ' + MESSAGE), [])
        self.assertEqual(self.learn(candidate(), message='第三方文档：\n```\n' + MESSAGE + '\n```'), [])
        prompts = []
        secret_message = MESSAGE + ' 我的密码是 hunter2'
        self.assertEqual(learn_from_turn(secret_message, '', prompts.append, self.store), [])
        self.assertEqual(prompts, [])
        self.assertEqual(self.learn(candidate(title='password: hunter2')), [])
        self.assertEqual(self.store.list(), [])

    def test_user_memory_is_preserved_and_repeats_deduplicate(self):
        record = self.store.save_user('回答语言', '使用繁体中文')
        self.assertEqual(self.learn(candidate()), [])
        self.assertEqual(self.store.get(record['id'])['content'], '使用繁体中文')
        result = self.learn(candidate(title='中文偏好'), candidate(title=' 中文偏好 '))
        self.assertEqual(len(result), 1)
        self.assertEqual(self.learn(candidate(title='中文偏好')), result)
        self.assertEqual(len(self.store.list()), 2)

    def test_cancel_and_no_candidates_write_nothing(self):
        cancelled = threading.Event()

        def generator(prompt):
            cancelled.set()
            return json.dumps([candidate()])

        self.assertEqual(learn_from_turn(MESSAGE, '', generator, self.store, cancel_event=cancelled), [])
        self.assertEqual(learn_from_turn(MESSAGE, '', None, self.store), [])
        self.assertEqual(self.learn(), [])
        self.assertEqual(self.store.list(), [])

    def test_input_and_candidate_length_limits(self):
        prompts = []

        def generator(prompt):
            prompts.append(json.loads(prompt.splitlines()[-1]))
            return json.dumps([candidate()])

        long_message = '文' * 1000 + MESSAGE
        self.assertEqual(learn_from_turn(long_message, '答' * 301, generator, self.store), [])
        self.assertEqual(prompts, [{'用户消息': '文' * 1000}])
        self.assertEqual(self.learn(candidate(title='长' * 101)), [])
        self.assertEqual(self.learn(candidate(title='长' * 100))[0]['title'], '长' * 100)

    def test_model_selects_quote_without_copying_answer_into_memory(self):
        original = '我的演示项目代号是青石742。'
        item = {'title': '演示项目代号', 'kind': 'fact', 'evidence_quote': original}
        self.assertEqual(self.learn(dict(item, evidence_quote=original.replace('742', ' 742')),
                                    message=original, answer='青石 742'), [])
        result = self.learn(item, message=original, answer='青石 742')
        self.assertEqual(self.store.get(result[0]['id'])['content'], original)

    def test_invalid_extraction_and_store_failure_can_be_reported(self):
        errors = []
        kwargs = {'on_error': lambda: errors.append(True)}
        self.assertEqual(learn_from_turn(MESSAGE, '', lambda prompt: 'invalid JSON', self.store, **kwargs), [])
        self.assertEqual(errors, [True])
        self.assertEqual(self.learn(**kwargs), [])
        self.assertEqual(self.learn(candidate(evidence_quote='不存在'), **kwargs), [])
        self.assertEqual(errors, [True])
        self.assertEqual(self.learn(candidate(), workspace='invalid\x00workspace', **kwargs), [])
        self.assertEqual(errors, [True, True])

    def test_fenced_json_allowed_but_prose_and_overflow_rejected(self):
        payload = json.dumps([candidate()])
        for raw in ('先说明\n' + payload, json.dumps([candidate()] * 3), '{}'):
            self.assertEqual(learn_from_turn(MESSAGE, '', lambda prompt: raw, self.store), [])
        raw = f'```json\n{payload}\n```'
        self.assertEqual(len(learn_from_turn(MESSAGE, '', lambda prompt: raw, self.store)), 1)


if __name__ == '__main__':
    unittest.main()
