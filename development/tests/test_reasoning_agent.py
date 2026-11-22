"""Only provider-supplied explanations become separate display metadata."""
import tempfile
from pathlib import Path
import unittest

from project_agent import ProjectAgent
from remote_api import APICancelled


class ReasoningAgentTests(unittest.TestCase):
    def test_callbacks_are_saved_separately_and_old_models_need_no_new_argument(self):
        for streaming in (True, False):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch)
                (root / 'workspace').mkdir()
                events = []

                class Model:
                    supports_reasoning_callback = streaming
                    def complete_tools(self, messages, tools, **kwargs):
                        if streaming:
                            kwargs['on_reasoning']('MODEL_EXPLANATION')
                        else:
                            assert 'on_reasoning' not in kwargs
                        return {'role': 'assistant', 'content': '正式回答', 'reasoning_content': 'MODEL_EXPLANATION'}

                result = ProjectAgent(root / 'workspace', Model(), runs_root=root / 'runs',
                    capability_mode='assistant', approve_command=lambda _: False, on_event=events.append).run('问题')
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['answer'], '正式回答')
                self.assertEqual(result['reasoning'].count('MODEL_EXPLANATION'), 1)
                self.assertEqual([e['type'] for e in events if e['type'].startswith('model_')], ['model_start', 'model_end'])
                self.assertTrue(any(e['type'] == 'reasoning' for e in events))

    def test_cancel_preserves_partial_reasoning_and_bounds_its_size(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'workspace').mkdir()
            class Model:
                supports_reasoning_callback = True
                def complete_tools(self, messages, tools, **kwargs):
                    kwargs['on_reasoning']('中' * 30000)
                    raise APICancelled('停止')
            result = ProjectAgent(root / 'workspace', Model(), runs_root=root / 'runs',
                capability_mode='assistant', approve_command=lambda _: False).run('问题')
            self.assertEqual(result['status'], 'stopped')
            self.assertEqual(len(result['reasoning']), 24000)
            self.assertNotIn('中中中', result['answer'])

    def test_no_reasoning_is_invented_for_a_plain_reply(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'workspace').mkdir()
            class Model:
                def complete_tools(self, messages, tools, **kwargs):
                    return {'role': 'assistant', 'content': '回复'}
            events = []
            result = ProjectAgent(root / 'workspace', Model(), runs_root=root / 'runs',
                capability_mode='assistant', approve_command=lambda _: False, on_event=events.append).run('问题')
            self.assertNotIn('reasoning', result)
            self.assertFalse(any(e['type'] == 'reasoning' for e in events))
