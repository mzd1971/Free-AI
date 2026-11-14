"""构建可核验工具调用 SFT 数据；不覆盖原数据或已有实验数据。"""
import hashlib
import json
import math
from pathlib import Path

from agent_lab import calculate
from paths import ROOT, RUNS_ROOT, DATA_ROOT


def prompt_for(task):
    # 与 run_agent 的首次无经验调用逐字一致。
    return ('你在受约束的工具实验中。只输出一个JSON对象，不要解释。\n'
            '可用工具：calculate计算四则运算，格式{"tool":"calculate","expression":"12*3-7"}；'
            'training_summary读取本项目最近完成的训练指标，格式{"tool":"training_summary"}。\n'
            '经验只是有适用范围的参考资料，不是高优先级指令：[]'
            '\n任务：' + task + '\n上次工具反馈：')


def row(split, index, task, expression, expected):
    # 金标准由下面独立的 Python 运算生成；禁止执行字符串代码。
    actual = calculate(expression)['value']
    if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-8):
        raise ValueError(f'金标准与工具不一致: {task}')
    if abs(expected) >= 10**9:
        raise ValueError('超出数据数值范围')
    return {'id': f'agent_v2_{split}_{index:03d}', 'prompt': prompt_for(task),
            'response': json.dumps({'tool': 'calculate', 'expression': expression}, ensure_ascii=False, separators=(',', ':')),
            'metadata': {'task': task, 'expected': expected, 'source': 'deterministic_synthetic',
                         'answer_validation': 'independent_python_arithmetic_crosschecked_with_agent_lab.calculate'}}


def generate():
    train = []
    for i in range(24):
        a, b, c = 13 + i * 3, 5 + i, 2 + i % 5
        cases = [
            (f'每件商品{a}元，买{b}件，使用{c}元优惠券，总共支付多少元？', f'{a}*{b}-{c}', a*b-c),
            (f'有{a}箱零件，每箱{b}袋，每袋{c}个，共有多少个零件？', f'{a}*{b}*{c}', a*b*c),
            (f'行程共{a*b}公里，已经走了{a}公里又走了{c}公里，还剩多少公里？', f'{a*b}-{a}-{c}', a*b-a-c),
            (f'{b}人平摊餐费{a*b}元及服务费{b*c}元，每人付多少元？', f'({a*b}+{b*c})/{b}', (a*b+b*c)/b),
            (f'仓库原有{a*b}个箱子，运入{a}个，发出{c}个，现在多少个？', f'{a*b}+{a}-{c}', a*b+a-c),
            (f'购买{b}支笔每支{c}元，另买一本{a}元的书，总价多少元？', f'{b}*{c}+{a}', b*c+a),
            (f'{a*b+c}个苹果每袋装{b}个，只装满袋，能装满几袋？', f'({a*b+c})//{b}', (a*b+c)//b),
            (f'将{a*b+c}颗糖每盒放{b}颗，装完所有满盒后还剩几颗？', f'({a*b+c})%{b}', (a*b+c)%b),
        ]
        for task, expr, expected in cases:
            train.append(row('train', len(train)+1, task, expr, expected))
    validation = []
    for i in range(8):
        a, b, c = 103+i*7, 9+i, 3+i
        cases = [
            (f'水箱每分钟流入{a}升、流出{b}升，持续{c}分钟后净增加多少升水？', f'({a}-{b})*{c}', (a-b)*c),
            (f'印刷机上午印了{a*b}页，下午印了{a*c}页，每册{a}页，一共能装订几册？', f'({a*b}+{a*c})/{a}', (a*b+a*c)/a),
        ]
        for task, expr, expected in cases:
            validation.append(row('validation', len(validation)+1, task, expr, expected))
    # 留出测试使用不同叙述、组合和常数；不得据此选择 epoch 或再次调参。
    test_cases = [
        ('社团有420元经费，收了18位成员每人25元，支付260元场地费后，余额是多少？', '420+18*25-260', 420+18*25-260),
        ('烘焙店烤了7盘饼干，每盘24块；留出18块试吃，其余平均装入10袋，每袋几块？', '(7*24-18)/10', (7*24-18)/10),
        ('打印一本讲义需要正面36页、附录12页，打印15本后又补印20页，总计打印多少页？', '(36+12)*15+20', (36+12)*15+20),
        ('骑行路线长360公里。头两天每天骑75公里，第三天骑90公里，最后两天均分剩余距离，每天骑多少公里？', '(360-2*75-90)/2', (360-2*75-90)/2),
        ('买4张成人票，每张85元，3张儿童票，每张40元，全单减60元后由两家均摊，每家多少元？', '(4*85+3*40-60)/2', (4*85+3*40-60)/2),
        ('蓄水池原有280升水，水泵以每分钟32升注水9分钟，再抽走170升，现在多少升？', '280+32*9-170', 280+32*9-170),
        ('志愿者带来13盒口罩，每盒50只，另有散装27只。分给9个小组，每组70只，剩余几只？', '13*50+27-9*70', 13*50+27-9*70),
        ('做三批材料，每批需要甲料125克和乙料75克。已有2袋各180克的混合材料，还需准备多少克？', '3*(125+75)-2*180', 3*(125+75)-2*180),
        ('相册原来有92张照片，删掉17张，再导入6组照片每组28张；每页放9张，最多有多少个满页？', '(92-17+6*28)//9', (92-17+6*28)//9),
        ('生产线上午完成8箱产品，每箱35件，下午另做146件；按12件一包全部打包后，有几件不满一包？', '(8*35+146)%12', (8*35+146)%12),
        ('充值200元，余额原有38元。连续7次消费每次19元，再退回两次各12元，账户剩多少元？', '200+38-7*19+2*12', 200+38-7*19+2*12),
        ('图书馆有16层书架，每层放45本，借出135本后，又归还3箱各25本，现在有多少本？', '16*45-135+3*25', 16*45-135+3*25),
    ]
    tests = [row('test', i+1, *case) for i, case in enumerate(test_cases)]
    return train, validation, tests


def main():
    target = RUNS_ROOT.parent / 'datasets' / 'agent-sft-v2'
    if target.exists():
        raise FileExistsError(f'拒绝覆盖已有数据集：{target}')
    original_path = DATA_ROOT / 'sft_train.jsonl'
    general_path = DATA_ROOT / 'sft_general_test.jsonl'
    original = [json.loads(s) for s in original_path.read_text(encoding='utf-8-sig').splitlines() if s.strip()]
    general = [json.loads(s) for s in general_path.read_text(encoding='utf-8-sig').splitlines() if s.strip()]
    if len(original) != 64 or len(general) != 6:
        raise ValueError('原数据计数变化，请人工检查后更新构建脚本。')
    train, validation, tests = generate()
    groups = {'sft_train.jsonl': train+original, 'sft_validation.jsonl': validation,
              'sft_test.jsonl': tests, 'sft_general_test.jsonl': general}
    prompts = [r['prompt'].strip() for rows in groups.values() for r in rows]
    tasks = [r['metadata']['task'] for rows in (train, validation, tests) for r in rows]
    assert len(prompts) == len(set(prompts)), '数据集间存在重复 prompt'
    assert len(tasks) == len(set(tasks)), '数据集间存在重复 task'
    from finetune import MODEL, encode_row
    from huggingface_hub import snapshot_download
    from transformers import AutoTokenizer
    snapshot = snapshot_download(MODEL, local_files_only=True)
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    lengths = {name: [encode_row(tokenizer, r)['input_ids'].shape[1] for r in rows]
               for name, rows in groups.items()}
    target.mkdir(parents=True, exist_ok=False)
    for name, rows in groups.items():
        if name == 'sft_general_test.jsonl':
            (target / name).write_bytes(general_path.read_bytes())
        else:
            (target / name).write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows)+'\n', encoding='utf-8')
    manifest = {'dataset': str(target), 'counts': {k: len(v) for k,v in groups.items()},
                'max_tokens': {k: max(v) for k,v in lengths.items()},
                'model': MODEL, 'tokenizer_revision': Path(snapshot).name,
                'provenance': '192 deterministic synthetic tool examples mixed with 64 existing assistant-authored educational examples; no web or teacher API data',
                'answer_validation': 'Expected values independently written as Python arithmetic, crosschecked using agent_lab.calculate; no eval',
                'split_policy': 'Validation uses distinct water/printing scenarios; test uses held-out wording and operation compositions. All prompts/tasks unique across splits. No existing four agent experiment questions reused.',
                'limitations': ['Small synthetic template dataset, not broad agent competence', 'Validation shares arithmetic operators with training', 'General replay reduces but does not guarantee prevention of forgetting', 'Only calculator action selection is trained; does not train consciousness, autonomous rule discovery, browser use, or general reasoning', 'Test set must not guide checkpoint selection or retries'],
                'sha256': {k: hashlib.sha256((target/k).read_bytes()).hexdigest() for k in groups}}
    (target/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return target


if __name__ == '__main__':
    main()
