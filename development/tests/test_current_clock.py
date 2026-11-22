"""Today's date must come from the device clock, never generated old memories."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_memory import AgentMemoryStore
import qwen_chat
import clock_context


class CurrentClockTests(unittest.TestCase):
    def test_today_ignores_stale_memory_and_does_not_learn_the_answer(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent) as directory:
            store = AgentMemoryStore(Path(directory) / 'memory.sqlite3')
            store.remember('今日日期', '今天是2023年11月20日。', kind='note',
                           agent_id='freeai', evidence='旧对话', pending=False)
            backend = Mock()
            backend.generate.return_value = '根据长期记忆，今天是2023年11月20日。'
            fixed = datetime(2026, 11, 22, 16, 30, tzinfo=timezone(timedelta(hours=8)))
            with patch('clock_context.current_time', return_value=fixed), patch('qwen_chat.runtime', return_value=backend):
                result = qwen_chat.chat('搜索今天的日期', return_details=True, use_memory=False,
                    personal_memory=store, read_personal_memory=True, write_personal_memory=True)
            self.assertIn('2026年11月22日', result['answer'])
            self.assertIn('本机', result['answer'])
            self.assertNotIn('2023', result['answer'])
            self.assertNotIn('memory_ids', result)
            self.assertNotIn('memory_updates', result)
            backend.generate.assert_not_called()

    def test_query_matching_is_narrow_and_does_not_swallow_other_requests(self):
        for message in ('今天几号？', '搜索今天的日期', '请帮我联网搜索一下今天的日期',
                        "What's today's date?", '当前日期', '现在几点了', '今天星期几'):
            with self.subTest(message=message):
                self.assertIsNotNone(clock_context.clock_query_kind(message))
        for message in ('搜索今天的新闻', '今天是什么节日', '今天是什么日子', '翻译：今天几号？',
                        '请用 Python 写一个今天日期函数', '记住我的生日是11月22日',
                        '今天的日期按 YYYY-MM-DD 输出，以后都这样', '北京现在几点', '昨天几号', ''):
            with self.subTest(message=message):
                self.assertIsNone(clock_context.clock_query_kind(message))

    def test_midnight_refresh_and_cancellation(self):
        import threading
        from remote_api import APICancelled
        fixed = datetime(2026, 11, 22, 23, 59, 59, tzinfo=timezone(timedelta(hours=8)))
        with patch('clock_context.current_time', side_effect=[fixed, fixed + timedelta(seconds=2)]), patch('qwen_chat.runtime') as runtime:
            before = qwen_chat.chat('今天几号')
            after = qwen_chat.chat('今天几号')
        self.assertIn('2026年11月22日', before)
        self.assertIn('2026年11月23日', after)
        runtime.assert_not_called()
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(APICancelled):
            qwen_chat.chat('今天几号', cancel_event=cancelled)

    def test_both_backends_receive_fresh_clock_separate_from_history(self):
        from local_runtime import LocalRuntime
        from remote_api import APIClient
        local = LocalRuntime(manager=Mock())
        self.addCleanup(local.shutdown)
        api = APIClient('https://example.com/v1', 'fixture-model', 'fixture-key')
        history = [{'role': 'user', 'content': '旧日期'}, {'role': 'assistant', 'content': '2023年11月20日'}]
        fixed = datetime(2026, 11, 22, 16, 30, tzinfo=timezone(timedelta(hours=8)))
        with patch('clock_context.current_time', return_value=fixed), patch.object(local, '_selection', return_value={}), patch.object(local, '_count_tokens', return_value=200):
            local_rows, _, _ = local._messages('根据今天日期制定三天计划', history, 4096)
            api_rows, _ = api._messages('根据今天日期制定三天计划', history, 4096)
        for rows in (local_rows, api_rows):
            self.assertEqual(rows[0]['role'], 'system')
            self.assertIn('2026年11月22日', rows[0]['content'])
            self.assertIn('UTC+08:00', rows[0]['content'])
            self.assertNotIn('2023', rows[0]['content'])
            self.assertIn('不能替代', rows[0]['content'])
            self.assertEqual(rows[2]['content'], '2023年11月20日')


if __name__ == '__main__':
    unittest.main()
