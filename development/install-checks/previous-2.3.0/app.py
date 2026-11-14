"""本地教学界面；所有训练与生成调用 ailab.py 中的真实模型。"""
import os
import json
from pathlib import Path
from threading import Lock
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
import pandas as pd
import torch
from ailab import ROOT, CharTokenizer, load_checkpoint, sample, train_run
from paths import RUNS_ROOT, DATA_ROOT
GPU_LOCK = Lock()


def _training(steps, rate, seed, device):
    try:
        for event in train_run(int(steps), float(rate), int(seed), device):
            rows = event["rows"]
            frame = pd.DataFrame([
                {"step": row["step"], "loss": row[key], "dataset": name}
                for row in rows for key, name in [("train_loss", "训练集"), ("validation_loss", "验证集")]
            ])
            info = event["metadata"]
            state = "已完成" if event["complete"] else "训练中"
            status = (f"**{state} · 第 {rows[-1]['step']} 步**  \n"
                      f"设备：{info['device']} · 参数：{info['parameters']:,} · "
                      f"验证集未知字符：{info['validation_unknown_ratio']:.1%}  \n"
                      f"实验目录：`{event['run_dir']}`")
            yield status, frame, event["before"], event["after"], gr.update(choices=checkpoints())
    except (ValueError, RuntimeError, OSError) as exc:
        raise gr.Error(str(exc)) from exc


def training(steps, rate, seed, device):
    with GPU_LOCK:
        yield from _training(steps, rate, seed, device)


def checkpoints():
    paths = sorted(RUNS_ROOT.glob("*/trained.pt"), reverse=True)
    return [str(p) for p in paths]


def _generate(checkpoint, prompt, temperature, length):
    if not checkpoint:
        raise gr.Error("请先训练，或点击刷新检查点后选择一个已完成的实验。")
    try:
        path = Path(checkpoint).resolve()
        if not path.is_relative_to(RUNS_ROOT.resolve()):
            raise ValueError("请选择本实验室输出目录中的检查点。")
        model, tokenizer = load_checkpoint(path)
        unknown = sum(char not in tokenizer.lookup for char in prompt)
        text = sample(model, tokenizer, prompt, int(length), float(temperature))
        return text, f"输入中有 {unknown} 个词表外字符。玩具模型的续写不代表可靠回答。"
    except (ValueError, RuntimeError, OSError) as exc:
        raise gr.Error(str(exc)) from exc


def generate(checkpoint, prompt, temperature, length):
    with GPU_LOCK:
        return _generate(checkpoint, prompt, temperature, length)


def tokenize(text):
    tokenizer = CharTokenizer.fit((DATA_ROOT / "train.txt").read_text(encoding="utf-8"))
    return [[char, idx, "未知字符" if idx == 0 else "训练词表内"]
            for char, idx in zip(text, tokenizer.encode(text))]


def build_app():
    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    with gr.Blocks(title="AI 实验室 · 第一课", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# AI 实验室\n从一次真实的参数更新，理解 AI 如何学习。")
        gr.Markdown(f"当前设备：**{gpu}**。本地文本微调与教学实验；听说看画尚未接入。")
        with gr.Tab("Agent 认知实验"):
            from agent_lab import ExperienceStore, run_agent, practice, AGENT_ROOT
            gr.Markdown("## 实践 → 经验 → 规律 → 再实践\n"
                        "模型选择工具，程序执行并记录结果；每次调用前自动检索相关规则。"
                        "这是外置经验记忆，不会修改模型权重，也不代表产生意识。")
            task = gr.Textbox(value="Python函数默认列表参数为什么会共享数据？", label="通用任务：编程、写作、资料整理、计算或训练问题")
            remember = gr.Checkbox(value=True, label="调用前自动检索经验规则")
            trained_agent = gr.Checkbox(value=True, label="使用扩大训练后的 Agent 专用适配器（尚未完成时用原模型）")
            act = gr.Button("执行 Agent 任务", variant="primary")
            result = gr.JSON(label="真实动作、观察、失败重试与检查范围")
            def agent_call(text, memory, trained):
                from qwen_chat import chat, load_latest
                with GPU_LOCK:
                    _, _, model_path = load_latest(agent_model=trained)
                    output = run_agent(text, memory, planner=lambda prompt: chat(prompt, agent_model=trained, use_memory=False))
                    output["model_path"] = str(model_path)
                    return output
            act.click(agent_call, [task, remember, trained_agent], result, api_name="agent_run")
            gr.Markdown("跨领域回答使用通用认识论检索；可执行工具仍为算术与训练摘要。"
                        "普通回答待核实，工具执行成功也不等于任务正确；完整反馈与审核入口在桌面版‘认识论 · 经验’。")
            with gr.Row():
                demonstration = gr.Button("运行一次有明确答案的示范练习")
                refresh_memory = gr.Button("刷新经验与规则")
            memories = gr.JSON(label="最近经验与有效规则（重启后保留）")
            def show_memory():
                from epistemology import KnowledgeStore
                store = KnowledgeStore()
                return {"rules": store.rules(), "experiences": store.recent()}
            def demonstrate():
                return {"demonstration": practice(), **show_memory()}
            demonstration.click(demonstrate, outputs=memories, api_name="agent_practice")
            refresh_memory.click(show_memory, outputs=memories, api_name="agent_memories")
            rule_id = gr.Textbox(label="要停用的通用规则ID（从上面的规则记录复制）")
            revoke = gr.Button("停用规则，保留原始证据")
            def disable_rule(identity):
                from epistemology import KnowledgeStore
                KnowledgeStore().disable(identity.strip())
                return show_memory()
            revoke.click(disable_rule, rule_id, memories, api_name="agent_disable_rule")
            compare = gr.Button("运行原模型三组对照实验（约一分钟）")
            scores = gr.JSON(label="四道留出算术题：直接回答 / 工具 / 工具+经验")
            def comparison():
                from experiment_agent import main
                with GPU_LOCK:
                    path = main()
                return {"path": str(path), **json.loads((path / "summary.json").read_text(encoding="utf-8"))}
            compare.click(comparison, outputs=scores, api_name="agent_experiment")
            gr.Markdown("对照实验使用独立经验库和脚本示范，不会将测试答案写入日常经验。"
                        "工具两组使用相同调用预算；直接回答只有一次调用，属于参考基线。")
            with gr.Accordion("原理、验证边界与研究依据", open=False):
                gr.Markdown((ROOT / "docs/05-agent-principles.md").read_text(encoding="utf-8"))
            with gr.Accordion("扩大训练实测：工具任务从0/12到5/12", open=False):
                gr.Markdown((ROOT / "docs/07-agent-training-v2.md").read_text(encoding="utf-8"))
        with gr.Tab("训练后的中文助手"):
            from qwen_chat import chat, completed_runs
            runs = completed_runs()
            gr.Markdown("**Qwen3-0.6B + LoRA** · 本地推理，不调用付费 API。"
                        "实验版本：评测中仍有错误建议和逻辑退化。只保留最近三轮历史，超长时进一步缩短。")
            gr.Markdown(f"已完成实验：`{runs[0].parent.name}`" if runs else "正式训练尚未完成；完成后刷新服务即可聊天。")
            use_base = gr.Checkbox(label="对照：关闭 LoRA，使用原始模型", value=False)
            def respond(message, history, base):
                try:
                    with GPU_LOCK:
                        return chat(message, history, base)
                except (ValueError, RuntimeError, OSError) as exc:
                    raise gr.Error(str(exc)) from exc
            gr.ChatInterface(respond, type="messages", additional_inputs=[use_base],
                             concurrency_limit=1)
        with gr.Tab("① 原理与工具"):
            gr.Markdown((ROOT / "docs/01-principles.md").read_text(encoding="utf-8"))
        with gr.Tab("② 文本变成数字"):
            token_input = gr.Textbox(value="模型怎样学习？", label="输入文本")
            token_button = gr.Button("查看字符编号")
            tokens = gr.Dataframe(headers=["字符", "编号", "状态"], interactive=False)
            token_button.click(tokenize, token_input, tokens)
            gr.Markdown("这里每个字符是一个 Token；正式大模型通常使用子词等更复杂的分词方式。")
            with gr.Accordion("查看训练文本与独立验证文本", open=False):
                gr.Textbox(value=(DATA_ROOT / "train.txt").read_text(encoding="utf-8"), label="训练集", lines=8, interactive=False)
                gr.Textbox(value=(DATA_ROOT / "validation.txt").read_text(encoding="utf-8"), label="验证集", lines=6, interactive=False)
        with gr.Tab("③ 从零训练"):
            gr.Markdown("每次从随机参数开始。先用默认配置，再只改变一个参数比较结果。"
                        "验证文本是同主题的人工教学样本，仅用于演示验证流程，不能证明通用能力。")
            with gr.Row():
                steps = gr.Slider(20, 1000, value=200, step=20, label="训练步数")
                rate = gr.Number(value=0.003, label="学习率（建议先用默认值）")
                seed = gr.Number(value=42, precision=0, label="随机种子")
                device = gr.Dropdown(["auto", "cpu", "cuda"], value="auto", label="训练设备")
            train = gr.Button("开始真实训练", variant="primary")
            status = gr.Markdown("尚未开始。每次实验会保存配置、数据快照、损失和训练前后权重。")
            plot = gr.LinePlot(x="step", y="loss", color="dataset", title="训练与验证损失", x_title="更新步数", y_title="交叉熵损失")
            with gr.Row():
                before = gr.Textbox(label="随机权重的续写", lines=5, interactive=False)
                after = gr.Textbox(label="训练后的续写（相同提示词、种子和温度）", lines=5, interactive=False)
        with gr.Tab("④ 加载与生成"):
            checkpoint = gr.Dropdown(checkpoints(), label="已完成的训练检查点")
            refresh = gr.Button("刷新检查点")
            prompt = gr.Textbox(value="问：模型", label="续写开头")
            with gr.Row():
                temperature = gr.Slider(0.1, 2, value=0.8, step=0.1, label="采样温度")
                length = gr.Slider(10, 300, value=120, step=10, label="新增字符数")
            go = gr.Button("用训练后的权重生成")
            generated = gr.Textbox(label="真实模型输出", lines=7)
            note = gr.Markdown()
            refresh.click(lambda: gr.update(choices=checkpoints()), outputs=checkpoint)
            go.click(generate, [checkpoint, prompt, temperature, length], [generated, note], concurrency_id="gpu", concurrency_limit=1)
        with gr.Tab("⑤ 多模态路线"):
            gr.Markdown((ROOT / "docs/02-roadmap.md").read_text(encoding="utf-8"))
        with gr.Tab("本轮训练复盘"):
            gr.Markdown((ROOT / "docs/03-first-run-review.md").read_text(encoding="utf-8"))
        train.click(training, [steps, rate, seed, device], [status, plot, before, after, checkpoint],
                    concurrency_id="gpu", concurrency_limit=1)
    return demo


if __name__ == "__main__":
    build_app().queue(default_concurrency_limit=1).launch(server_name="127.0.0.1", server_port=7860, share=False)
