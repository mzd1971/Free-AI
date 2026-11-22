"""Exercise ordinary-chat memory through an isolated store and fixture backends."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from agent_memory import AgentMemoryStore
from epistemology import KnowledgeStore
import qwen_chat


PREFERENCE = '以后回复我都请用简体中文。'
ANSWER = '可以，我会使用简体中文。'
TITLE = '首选回复语言'
EXTRACTED = json.dumps([{'title': TITLE, 'content': PREFERENCE, 'kind': 'preference',
                         'evidence_quote': PREFERENCE}], ensure_ascii=False)


class Backend:
    supports_reasoning_callback = True

    def __init__(self, answer=ANSWER, extraction=EXTRACTED):
        self.calls = []
        self.answer = answer
        self.extraction = extraction

    def generate(self, prompt, history=None, **options):
        self.calls.append((prompt, history, options))
        self.last_context_notice = f'请求 {len(self.calls)}'
        if 'evidence_quote' in prompt:
            if callable(self.extraction):
                return self.extraction()
            return self.extraction
        if options.get('on_reasoning'):
            options['on_reasoning']('正文思考')
        if options.get('on_token'):
            options['on_token'](self.answer)
        return self.answer


class ChatMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory_path = self.root / 'memory.sqlite3'
        self.memories = AgentMemoryStore(self.memory_path)
        self.knowledge = KnowledgeStore(self.root / 'knowledge.sqlite3')

    def test_read_injection_does_not_change_recorded_task_or_eligibility(self):
        memory = self.memories.save_user(TITLE, PREFERENCE, kind='preference')
        backend = Backend()
        with patch('qwen_chat.runtime', return_value=backend):
            result = qwen_chat.chat('什么是重构？', memory_store=self.knowledge, return_details=True,
                                   personal_memory=self.memories, read_personal_memory=True)
        prompt = backend.calls[0][0]
        self.assertIn('用户', prompt)
        self.assertIn(PREFERENCE, prompt)
        self.assertIn('不授权', prompt)
        self.assertTrue(prompt.endswith('当前请求：\n什么是重构？'))
        self.assertEqual(result['memory_ids'], [memory['id']])
        episode = self.knowledge.episode(result['episode_id'])
        self.assertEqual(episode['task'], '什么是重构？')
        self.assertFalse(episode['training_eligible'])

    def test_api_defaults_do_not_read_write_or_issue_extra_call(self):
        backend = Backend()
        store = Mock()
        with patch('qwen_chat.runtime') as runtime:
            result = qwen_chat.chat(PREFERENCE, api_client=backend, personal_memory=store,
                                   memory_store=self.knowledge, return_details=True)
        runtime.assert_not_called()
        self.assertEqual(store.mock_calls, [])
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(backend.calls[0][0], PREFERENCE)
        self.assertNotIn('memory_updates', result)
        self.assertNotIn('memory_status', result)
        self.assertNotIn('memory_attempted', result)

    def test_retrieved_memory_not_reported_when_answer_did_not_call_model(self):
        self.memories.save_user(TITLE, PREFERENCE, kind='preference')
        cached = {'answer': '已有结果', 'episode_id': None}
        with patch('epistemology.respond', return_value=cached), patch('qwen_chat.runtime') as runtime:
            result = qwen_chat.chat('已有问题', return_details=True,
                                   personal_memory=self.memories, read_personal_memory=True)
        runtime.assert_not_called()
        self.assertNotIn('memory_ids', result)

    def test_opted_in_api_extracts_once_without_streaming_memory_and_reopens(self):
        backend = Backend()
        stream = Mock()
        reasoning = Mock()
        stage = Mock()
        history = [{'role': 'user', 'content': '上一轮占位内容。'}]
        result = qwen_chat.chat(PREFERENCE, history, api_client=backend, context=8192,
                               memory_store=self.knowledge, personal_memory=self.memories,
                               write_personal_memory=True, memory_evidence='会话 fixture',
                               on_token=stream, on_reasoning=reasoning, on_memory_stage=stage, return_details=True)
        self.assertEqual(result['answer'], ANSWER)
        self.assertEqual(len(result['memory_updates']), 1)
        self.assertTrue(result['memory_attempted'])
        self.assertEqual(result['memory_status'], 'saved')
        self.assertEqual(result['memory_updates'][0]['title'], TITLE)
        stream.assert_called_once_with(ANSWER)
        reasoning.assert_called_once_with('正文思考')
        stage.assert_called_once_with()
        self.assertEqual(backend.calls[0][1], history)
        extraction, sent_history, options = backend.calls[1]
        self.assertEqual(sent_history, [])
        self.assertEqual(options['context'], 8192)
        self.assertNotIn('on_token', options)
        self.assertNotIn('on_reasoning', options)
        self.assertNotIn(history[0]['content'], extraction)
        self.assertEqual(backend.last_context_notice, '请求 1')
        reopened = AgentMemoryStore(self.memory_path)
        self.assertIn(PREFERENCE, reopened.before_call(PREFERENCE)['context'])

    def test_reasoning_details_are_bounded_and_belong_to_the_main_answer(self):
        canonical = '正文思考' * 8000

        class ReasoningBackend(Backend):
            def generate(self, prompt, history=None, **options):
                self.last_reasoning = '记忆提取思考' if 'evidence_quote' in prompt else canonical
                return super().generate(prompt, history=history, **options)

        backend = ReasoningBackend()
        thoughts = []
        result = qwen_chat.chat(PREFERENCE, api_client=backend, memory_store=self.knowledge,
                               personal_memory=self.memories, write_personal_memory=True,
                               on_reasoning=thoughts.append, return_details=True)
        self.assertEqual(thoughts, ['正文思考'])
        self.assertEqual(result['reasoning'], canonical[:24000])
        self.assertEqual(result['answer'], ANSWER)
        self.assertEqual(self.knowledge.episode(result['episode_id'])['answer'], ANSWER)
        self.assertEqual(backend.last_reasoning, canonical)
        self.assertNotIn('正文思考', backend.calls[1][0])
        self.assertNotIn('on_reasoning', backend.calls[1][2])
        self.assertEqual(qwen_chat.chat(PREFERENCE, api_client=backend, memory_store=self.knowledge),
                         ANSWER)

    def test_local_extraction_uses_selected_model_and_keeps_main_reply_on_stop(self):
        stopped = threading.Event()

        def cancel_extraction():
            stopped.set()
            raise RuntimeError('fixture interrupted')

        backend = Backend(extraction=cancel_extraction)
        with patch('qwen_chat.runtime', return_value=backend):
            result = qwen_chat.chat(PREFERENCE, use_memory=False, model_id='chosen',
                                   context=16384, cancel_event=stopped, return_details=True,
                                   personal_memory=self.memories, write_personal_memory=True)
        self.assertTrue(stopped.is_set())
        self.assertEqual(result['answer'], ANSWER)
        self.assertNotIn('memory_updates', result)
        self.assertTrue(result['memory_attempted'])
        self.assertEqual(result['memory_status'], 'cancelled')
        self.assertNotIn('memory_warning', result)
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(backend.calls[1][2]['model_id'], 'chosen')
        self.assertEqual(backend.calls[1][2]['context'], 16384)
        self.assertEqual(self.memories.list(), [])

    def test_memory_store_failure_preserves_answer(self):
        backend = Backend()
        store = Mock(wraps=self.memories)
        store.before_call.side_effect = OSError('fixture memory read failure')
        store.remember.side_effect = OSError('fixture memory write failure')
        with patch('qwen_chat.runtime', return_value=backend):
            result = qwen_chat.chat(PREFERENCE, use_memory=False, return_details=True,
                                   personal_memory=store, read_personal_memory=True,
                                   write_personal_memory=True)
        self.assertEqual(result['answer'], ANSWER)
        self.assertIn('未能读取', result['memory_warning'])
        self.assertIn('整理未完成', result['memory_warning'])
        self.assertTrue(result['memory_attempted'])
        self.assertEqual(result['memory_status'], 'failed')
        self.assertNotIn('memory_updates', result)
        store.remember.assert_called_once()

    def test_memory_completed_without_new_entry_is_quiet(self):
        backend = Backend(extraction='[]')
        with patch('qwen_chat.runtime', return_value=backend):
            result = qwen_chat.chat(PREFERENCE, use_memory=False, return_details=True,
                                   personal_memory=self.memories, write_personal_memory=True)
        self.assertTrue(result['memory_attempted'])
        self.assertEqual(result['memory_status'], 'no_update')
        self.assertNotIn('memory_warning', result)
        self.assertEqual(len(backend.calls), 2)

    def test_blank_or_malformed_extraction_is_not_successful(self):
        for raw in ('', 'not json'):
            with self.subTest(raw=raw), patch('qwen_chat.runtime', return_value=Backend(extraction=raw)):
                result = qwen_chat.chat(PREFERENCE, use_memory=False, return_details=True,
                                       personal_memory=self.memories, write_personal_memory=True)
                self.assertTrue(result['memory_attempted'])
                self.assertEqual(result['memory_status'], 'failed')
                self.assertEqual(result['answer'], ANSWER)
                self.assertNotIn('memory_updates', result)

    def test_missing_main_answer_never_starts_memory(self):
        backend = Backend(answer=' ')
        with patch('qwen_chat.runtime', return_value=backend):
            result = qwen_chat.chat(PREFERENCE, use_memory=False, return_details=True,
                                   personal_memory=self.memories, write_personal_memory=True)
        self.assertFalse(result['memory_attempted'])
        self.assertEqual(result['memory_status'], 'not_attempted')
        self.assertNotIn('memory_warning', result)
        self.assertEqual(len(backend.calls), 1)


if __name__ == '__main__':
    unittest.main()
