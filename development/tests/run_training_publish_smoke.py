"""Manual CPU smoke of published adapter inference using installed packages.

From the checkout root:
  runtime/python/python.exe -E -s -B -X utf8 -m development.tests.run_training_publish_smoke

The synthetic tensors and comparison rows exercise publication and inference
wiring. They are fixture data and provide no training-quality evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from capability_manager import PackageManager
from development.tests.test_training_publish import fixture_run
from local_runtime import LocalRuntime
from training_publish import BASE_MODEL_ID, publish_run


class ScratchCPUManager(PackageManager):
    """Keep publications and runtime logs in scratch, borrowing verified assets."""

    def __init__(self, root, installed):
        super().__init__(root)
        self.installed = installed

    def ready(self, identity, *, full=False):
        if identity == 'engine-cuda':
            return False
        if identity in ('engine-cpu', BASE_MODEL_ID):
            return self.installed.ready(identity, full=full)
        return super().ready(identity, full=full)

    def asset_path(self, identity):
        if identity in ('engine-cpu', BASE_MODEL_ID):
            return self.installed.asset_path(identity)
        return super().asset_path(identity)


def main():
    installed = PackageManager()
    installed.asset_path('engine-cpu')
    installed.asset_path(BASE_MODEL_ID)
    scratch_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='freeai-native-lora-', dir=scratch_root) as directory:
        scratch = Path(directory)
        model_root = scratch / 'models'
        publisher = ScratchCPUManager(model_root, installed)
        identity = publish_run(publisher, fixture_run(scratch / 'synthetic-run'))

        # Reopen as the client would in a separate process.
        manager = ScratchCPUManager(model_root, installed)
        assert manager.ready(identity, full=True)
        runtime = LocalRuntime(manager, startup_timeout=20, request_timeout=20)
        try:
            answer = runtime.generate('Reply with only the word OK.', model_id=identity, context=2048)
            adapters = runtime._json('/lora-adapters')
            assert answer and len(adapters) == 1 and adapters[0]['scale'] == 1, adapters
            assert Path(adapters[0]['path']).samefile(manager.asset_path(identity)), adapters
            assert manager._active == {'engine-cpu', BASE_MODEL_ID, identity}, manager._active
            print(json.dumps({
                'fixture': 'synthetic adapter and evaluation',
                'backend': runtime.last_engine_notice,
                'trained_model_id': identity,
                'base_package_id': BASE_MODEL_ID,
                'loaded_adapters': len(adapters),
                'adapter_scale': adapters[0]['scale'],
                'completion_tokens': runtime.last_usage.get('completion_tokens'),
            }, ensure_ascii=False))
        finally:
            runtime.shutdown()
        assert not manager._active, manager._active


if __name__ == '__main__':
    main()
