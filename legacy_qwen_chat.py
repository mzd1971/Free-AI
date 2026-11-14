"""加载已完成的 LoRA，支持原模型对照；首次请求读取本地权重。"""
import argparse
import json
from contextlib import nullcontext
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

from finetune import ROOT, SYSTEM
from paths import RUNS_ROOT, base_model_path

_cached = {}


def completed_runs(agent_model=False):
    return sorted(RUNS_ROOT.glob("qwen-agent-lora-*/summary.json" if agent_model else "qwen-lora-*/summary.json"), reverse=True)


def load_latest(agent_model=False):
    global _cached
    runs = completed_runs(agent_model) or completed_runs()
    if not runs:
        raise ValueError("尚无完成的 Qwen LoRA 训练。")
    run = runs[0].parent
    if run in _cached:
        return _cached[run][0], _cached[run][1], run
    config = json.loads((run / "run_config.json").read_text(encoding="utf-8"))
    tokenizer = AutoTokenizer.from_pretrained(run / "adapter", local_files_only=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    base = AutoModelForCausalLM.from_pretrained(base_model_path(config.get("base_snapshot")), torch_dtype=dtype,
        device_map={"": device}, attn_implementation="sdpa", local_files_only=True)
    model = PeftModel.from_pretrained(base, run / "adapter", is_trainable=False).eval()
    _cached[run] = (model, tokenizer)
    return model, tokenizer, run


@torch.inference_mode()
def _generate(message, history=None, use_base=False, agent_model=False):
    if not message or not message.strip():
        raise ValueError("请输入问题。")
    model, tokenizer, _ = load_latest(agent_model)
    history = history or []
    messages = [{"role": "system", "content": SYSTEM}]
    messages.extend({"role": row["role"], "content": row["content"]}
                    for row in history[-6:] if isinstance(row.get("content"), str)
                    and row["role"] in {"user", "assistant"})
    messages.append({"role": "user", "content": message})
    while True:
        ids = tokenizer.apply_chat_template(messages, tokenize=True, enable_thinking=False,
            add_generation_prompt=True, return_tensors="pt")
        if ids.shape[1] <= 1536:
            break
        if len(messages) <= 2:
            raise ValueError("本机原型限制输入为 1536 tokens，请缩短本次问题。")
        del messages[1:3]
    ids = ids.to(next(model.parameters()).device)
    torch.manual_seed(42)
    settings = GenerationConfig(do_sample=True, temperature=0.7, top_p=0.8, top_k=20,
        max_new_tokens=256, eos_token_id=tokenizer.eos_token_id, pad_token_id=tokenizer.pad_token_id)
    with model.disable_adapter() if use_base else nullcontext():
        output = model.generate(input_ids=ids, attention_mask=torch.ones_like(ids), generation_config=settings)
    result = tokenizer.decode(output[0, ids.shape[1]:], skip_special_tokens=True).strip()
    if len(output[0]) - ids.shape[1] >= 256:
        result += "\n\n[已达到本次输出长度上限]"
    return result


def chat(message, history=None, use_base=False, agent_model=False, use_memory=True,
         memory_store=None, return_details=False):
    if not use_memory:
        answer = _generate(message, history, use_base, agent_model)
        return {'answer': answer, 'episode_id': None, 'trace': [], 'verified': False} if return_details else answer
    from epistemology import respond
    result = respond(message, lambda prompt: _generate(prompt, history, use_base, agent_model), store=memory_store)
    return result if return_details else result['answer']


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--base", action="store_true")
    args = parser.parse_args()
    print(chat(args.prompt, use_base=args.base))
