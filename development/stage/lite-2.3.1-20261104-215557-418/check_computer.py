"""Read-only, offline checks for the transferable Windows folder."""
import json
import sys
from pathlib import Path
from paths import ROOT, ARTIFACTS, DATA_ROOT, RUNS_ROOT, base_model_path


def main():
    import torch
    import tkinter
    bundled = ROOT / 'runtime' / 'python'
    model = Path(base_model_path())
    reports = sorted(RUNS_ROOT.glob('qwen*-*/summary.json'))
    checks = {
        '应用自带的运行环境': Path(sys.prefix).resolve() == bundled.resolve(),
        '应用自带的计算组件': Path(torch.__file__).resolve().is_relative_to(bundled.resolve()),
        '应用自带的基础模型': model.resolve().is_relative_to(ROOT.resolve()),
        '练习题齐全': all((DATA_ROOT / name).is_file() for name in
                     ['sft_train.jsonl', 'sft_validation.jsonl', 'sft_test.jsonl', 'sft_general_test.jsonl']),
        '已有完成的学习成果': bool(reports),
    }
    for name, ok in checks.items():
        print(('通过：' if ok else '缺少：') + name)
    print('应用位置：', ROOT)
    print('当前数据位置：', ARTIFACTS)
    print('基础模型位置：', model)
    print('显卡训练：' + (torch.cuda.get_device_name(0) if torch.cuda.is_available()
                       else '暂不可用；可以用电脑处理器聊天。正式训练需要兼容的 NVIDIA 显卡和驱动。'))
    window = tkinter.Tk()
    window.withdraw()
    window.update()
    window.destroy()
    print('桌面窗口：通过')
    print(json.dumps({'checks': checks, 'data_root': str(ARTIFACTS), 'model': str(model),
                      'python': sys.executable, 'gpu_training': torch.cuda.is_available()}, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('检查未通过：', exc)
        raise SystemExit(1)
