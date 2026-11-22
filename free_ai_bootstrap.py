"""Windowed launcher for the independent client and training applications."""
import gc
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def self_test(trainer=False):
    import tkinter as tk
    from unittest.mock import patch
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory(prefix='free-ai-check-', dir=ROOT) as scratch:
        os.environ['AI_TRAINING_DATA'] = scratch
        if trainer:
            from trainer_app import TrainerApp as Application
        else:
            from client_app import ClientApp as Application
        import paths
        window = tk.Tk()
        window.withdraw()
        app = None
        def settle(timeout=8):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                window.update()
                if not app.busy:
                    return
                time.sleep(.02)
            raise AssertionError('Interface task did not settle')
        try:
            with patch('socket.create_connection', side_effect=AssertionError('Unexpected startup network')) as startup_connection:
                app = Application(window)
                settle()
                startup_connection.assert_not_called()
            assert len(app.pages) == 3
            assert paths.ARTIFACTS == Path(scratch)
            assert paths.MODELS_ROOT == Path(scratch) / 'models'
            assert app.manager.root == paths.MODELS_ROOT
            assert not {'torch', 'transformers', 'peft'} & set(sys.modules)
            assert (ROOT / 'assets/free-ai.ico').is_file()
            assert not app.manager.ready('text-default')
            report = {'passed': True, 'mode': 'trainer' if trainer else 'client',
                      'title': window.title(), 'python': sys.executable, 'app': str(ROOT),
                      'pages': 3, 'daily_database_modified': False, 'no_training_dependency_imported': True}
            report['model_storage_isolated'] = True
            if trainer:
                assert not hasattr(app, 'transcript')
                assert app.trainer_model.get() == ''
                with patch.object(app, 'model_ready', return_value=True), patch('training_desktop.training_command') as train, patch.object(app.manager, 'install') as download:
                    app.finetune()
                    assert not app.busy
                    train.assert_not_called()
                    download.assert_not_called()
                    app.use_training_model()
                    assert app.trainer_model.get() == 'legacy-training-model'
                    train.assert_not_called()
                    download.assert_not_called()
                from training_extension import prepare_starter_data
                prepare_starter_data()
                app.check_data()
                settle()
                assert '64' in app.data_notice.get(), app.data_notice.get()
                report['starter_data_inspected'] = True
                report.update(starts_without_selected_model=True, explicit_model_choice_required=True,
                              choosing_model_does_not_start_training_or_download=True)
            else:
                assert not hasattr(app, 'learning_tab_view')
                assert app.selected_model is None
                assert app.backend == 'local'
                assert not app.api_settings.public()['key_available']
                assert not app.api_settings.path.exists()
                report.update(api_default_unselected=True, api_startup_offline=True)
                app.input.insert('1.0', '缺少模型时保留草稿')
                app.send_chat()
                assert app.current_page == 1
                assert app.input.get('1.0', 'end').strip() == '缺少模型时保留草稿'
                assert app.project.assistant_enabled.get()
                # Keep the streaming/cancel smoke on the plain generator. The
                # unified tool loop is covered by the native integration suite.
                app.project.assistant_enabled.set(False)
                app.input.delete('1.0', 'end')
                app.input.insert('1.0', '保存对话测试')
                def fake_chat(*args, **kwargs):
                    kwargs['on_token']('测试回答')
                    return {'answer': '测试回答', 'episode_id': None}
                with patch.object(app.manager, 'ready', return_value=True), patch('qwen_chat.chat', side_effect=fake_chat), patch('qwen_chat.runtime', return_value=SimpleNamespace(last_context_notice='')):
                    # Downloaded files alone do not select a model.
                    app.send_chat()
                    assert not app.busy and app.session_id is None
                    app.enable_model()
                    app.send_chat()
                    settle()
                first = app.session_id
                assert app.sessions.messages(first)[-1]['content'] == '测试回答'
                app.new_conversation()
                assert not app.history and app.session_id is None
                app.load_conversation(first)
                assert len(app.history) == 2
                app.input.insert('1.0', '测试停止')
                def interrupted_chat(*args, **kwargs):
                    kwargs['on_token']('已经生成的文字')
                    kwargs['cancel_event'].set()
                    raise RuntimeError('cancelled by test')
                with patch.object(app.manager, 'ready', return_value=True), patch('qwen_chat.chat', side_effect=interrupted_chat), patch('logging.exception'):
                    app.send_chat()
                    settle()
                answer = app.sessions.messages(first)[-1]['content']
                assert '已经生成的文字' in answer and '已停止' in answer
                report.update(fresh_install_without_models=True, conversation_roundtrip=True,
                              cancelled_answer_persisted=True, three_client_pages_only=True,
                              starts_without_selected_model=True, explicit_model_choice_required=True)
                app.disable_model()
                app.new_conversation()
                app.select_skill('calculator')
                app.input.insert('1.0', '12*3-7')
                with patch('qwen_chat.chat') as inference, patch('qwen_chat.runtime') as runtime:
                    app.send_chat()
                    settle()
                    inference.assert_not_called()
                    runtime.assert_not_called()
                assert '29' in app.last_answer
                assert app.last_tool_result['tool_execution_verified']
                from model_catalog import chat_models
                from app_skills import skill_catalog
                assert len(chat_models(app.manager.catalog)) == 5
                assert len(skill_catalog()) == 10
                for section in ('models', 'skills', 'extensions'):
                    app.show_capabilities(section)
                    window.update()
                report.update(model_options=5, skill_options=10, local_skill_without_model=True)
            for index in range(3):
                app.select_page(index)
                window.update()
            filename = 'trainer-self-test.json' if trainer else 'launcher-self-test.json'
            (ROOT / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            if app and hasattr(app, 'poll_id'):
                window.after_cancel(app.poll_id)
            window.destroy()
            gc.collect()


if __name__ == '__main__':
    trainer_mode = '--trainer' in sys.argv
    try:
        if '--self-test' in sys.argv:
            self_test(trainer_mode)
        else:
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FreeAI.Trainer.2' if trainer_mode else 'FreeAI.Client.2')
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
            if trainer_mode:
                from trainer_app import main
            else:
                from client_app import main
            main()
    except Exception:
        (ROOT / ('trainer-startup-error.log' if trainer_mode else 'startup-error.log')).write_text(traceback.format_exc(), encoding='utf-8')
        raise
