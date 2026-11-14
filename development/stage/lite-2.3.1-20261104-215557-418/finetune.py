"""在本机实际微调 Qwen3-0.6B；默认只训练 LoRA，独立记录验证与留出测试。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

import torch
from peft import LoraConfig, PeftModel, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

from paths import ROOT, RUNS_ROOT, DATA_ROOT, base_model_path
from compute_guard import ComputeLease
MODEL = "Qwen/Qwen3-0.6B"
SYSTEM = "你是一个中文助手。请准确、简洁地回答；不确定时说明不确定，不要编造已执行的操作。"


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_rows(name, data_dir=None):
    return [json.loads(line) for line in ((Path(data_dir) if data_dir else DATA_ROOT) / name).read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def encode_row(tokenizer, row, limit=512):
    prefix = tokenizer.apply_chat_template([
        {"role": "system", "content": SYSTEM}, {"role": "user", "content": row["prompt"]}
    ], tokenize=False, add_generation_prompt=True, enable_thinking=False)
    prefix_ids = tokenizer.encode(prefix, add_special_tokens=False)
    # 使用与推理相同的非思考前缀。只对回答及结束符计算损失。
    answer_ids = tokenizer.encode(row["response"] + "<|im_end|>", add_special_tokens=False)
    ids = prefix_ids + answer_ids
    if len(ids) > limit:
        raise ValueError(f"样本 {row['id']} 有 {len(ids)} tokens，超过 {limit}；拒绝静默截断。")
    return {"input_ids": torch.tensor([ids], dtype=torch.long),
            "labels": torch.tensor([[-100] * len(prefix_ids) + answer_ids], dtype=torch.long),
            "attention_mask": torch.ones(1, len(ids), dtype=torch.long)}


@torch.inference_mode()
def validation_loss(model, batches, check_stop=None):
    model.eval()
    numerator = denominator = 0
    for item in batches:
        if check_stop:
            check_stop()
        inputs = {key: value.to("cuda") for key, value in item.items()}
        count = int((inputs["labels"][:, 1:] != -100).sum())
        loss = model(**inputs).loss.item()
        numerator += loss * count
        denominator += count
    return numerator / denominator


@torch.inference_mode()
def answer(model, tokenizer, prompt, max_tokens=192):
    model.eval()
    model.config.use_cache = True
    inputs = tokenizer.apply_chat_template([
        {"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}
    ], tokenize=True, add_generation_prompt=True, enable_thinking=False, return_tensors="pt").to("cuda")
    torch.manual_seed(42)
    torch.cuda.manual_seed_all(42)
    # 官方非思考模式推荐采样；前后比较固定种子、参数和输出预算。
    generation = GenerationConfig(do_sample=True, temperature=0.7, top_p=0.8, top_k=20,
                                  max_new_tokens=max_tokens, pad_token_id=tokenizer.pad_token_id,
                                  eos_token_id=tokenizer.eos_token_id)
    output = model.generate(input_ids=inputs, attention_mask=torch.ones_like(inputs), generation_config=generation)
    new_ids = output[0, inputs.shape[1]:]
    return {"text": tokenizer.decode(new_ids, skip_special_tokens=True).strip(),
            "new_tokens": len(new_ids), "hit_token_limit": len(new_ids) >= max_tokens}


def run(epochs=3, data_dir=None, initial_adapter=None, output_prefix="qwen-lora", learning_rate=0.0001, stop_file=None):
    if not 1 <= epochs <= 10 or not math.isfinite(learning_rate) or not 0 < learning_rate <= 0.01:
        raise ValueError("轮数必须为1–10，学习率必须大于0且不超过0.01。")
    def check_stop():
        if stop_file and Path(stop_file).exists():
            print(json.dumps({"event": "cancelled", "run": str(target)}, ensure_ascii=False), flush=True)
            raise SystemExit(2)
    if not torch.cuda.is_available():
        raise RuntimeError("本次正式微调需要 CUDA GPU。")
    torch.set_num_threads(4)
    torch.manual_seed(42)
    random.seed(42)
    start = time.monotonic()
    target = RUNS_ROOT / (output_prefix + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    target.mkdir(parents=True, exist_ok=False)
    check_stop()
    files = ["sft_train.jsonl", "sft_validation.jsonl", "sft_test.jsonl", "sft_general_test.jsonl"]
    groups = [read_rows(name, data_dir) for name in files]
    all_prompts = [row["prompt"].strip() for group in groups for row in group]
    if len(all_prompts) != len(set(all_prompts)):
        raise ValueError("数据存在重复问题，停止训练。")
    for name, group in zip(files, groups):
        (target / name).write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in group), encoding="utf-8")
    snapshot = base_model_path()
    manifest = Path(snapshot) / 'bundle_source.json'
    revision = json.loads(manifest.read_text(encoding='utf-8'))['revision'] if manifest.exists() else Path(snapshot).name
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    base = AutoModelForCausalLM.from_pretrained(snapshot, torch_dtype=torch.bfloat16,
        device_map={"": "cuda"}, attn_implementation="sdpa", local_files_only=True)
    torch.cuda.reset_peak_memory_stats()
    model = (PeftModel.from_pretrained(base, initial_adapter, is_trainable=True) if initial_adapter else
        get_peft_model(base, LoraConfig(r=8, lora_alpha=16, lora_dropout=0.05,
        target_modules=["q_proj", "v_proj"], bias="none", task_type="CAUSAL_LM")))
    train_batches = [encode_row(tokenizer, row) for row in groups[0]]
    val_batches = [encode_row(tokenizer, row) for row in groups[1]]
    test_rows = [dict(row, group=label) for rows, label in [(groups[2], "domain"), (groups[3], "general")] for row in rows]
    config = {"base_model": MODEL, "revision": revision, "base_snapshot": snapshot,
        "system_prompt": SYSTEM, "epochs": epochs, "learning_rate": learning_rate,
        "initial_adapter": str(initial_adapter) if initial_adapter else None,
        "baseline": "initial_adapter" if initial_adapter else "base_model",
        "gradient_accumulation": 4, "max_sequence_length": 512, "seed": 42,
        "lora_r": 8, "lora_alpha": 16, "lora_targets": ["q_proj", "v_proj"],
        "trainable_parameters": sum(p.numel() for p in model.parameters() if p.requires_grad),
        "total_parameters": sum(p.numel() for p in model.parameters()),
        "data_counts": dict(zip(files, map(len, groups))),
        "data_hashes": {name: hashlib.sha256((target / name).read_bytes()).hexdigest() for name in files},
        "data_provenance": "User-selected local dataset; see saved data snapshots and source manifest.",
        "gpu": torch.cuda.get_device_name(0), "torch": str(torch.__version__),
        "precision": "bfloat16 base, float32 trainable adapter", "generation_max_tokens": 192}
    dump(target / "run_config.json", config)
    print(json.dumps({"event": "loaded", "run": str(target), **config}, ensure_ascii=False), flush=True)
    metrics = []
    comparisons = []
    with (nullcontext() if initial_adapter else model.disable_adapter()):
        baseline_val = validation_loss(model, val_batches, check_stop)
        for row in test_rows:
            check_stop()
            result = dict(row, before=answer(model, tokenizer, row["prompt"]))
            comparisons.append(result)
            dump(target / "comparison.json", comparisons)
            print(json.dumps({"event": "baseline", "id": row["id"]}), flush=True)
    best_loss = float("inf")
    best_epoch = 0
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model.config.use_cache = False
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=learning_rate)
    updates = 0
    training_start = time.monotonic()
    for epoch in range(1, epochs + 1):
        order = list(range(len(train_batches)))
        random.Random(42 + epoch).shuffle(order)
        model.train()
        total_loss = 0.0
        for offset in range(0, len(order), 4):
            check_stop()
            indices = order[offset:offset + 4]
            optimizer.zero_grad(set_to_none=True)
            for index in indices:
                batch = {key: value.to("cuda") for key, value in train_batches[index].items()}
                loss = model(**batch).loss
                if not torch.isfinite(loss):
                    raise RuntimeError("训练损失非有限值，停止保存。")
                total_loss += loss.item()
                (loss / len(indices)).backward()
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
            optimizer.step()
            updates += 1
            if updates % 4 == 0:
                print(json.dumps({"event": "train", "epoch": epoch, "update": updates,
                    "mean_loss": total_loss / (offset + len(indices)),
                    "allocated_gb": torch.cuda.memory_allocated() / 2**30}), flush=True)
        val = validation_loss(model, val_batches, check_stop)
        check_stop()
        metrics.append({"epoch": epoch, "updates": updates, "train_loss": total_loss / len(order), "validation_loss": val})
        dump(target / "metrics.json", {"baseline_validation_loss": baseline_val, "epochs": metrics})
        if val < best_loss:
            best_loss, best_epoch = val, epoch
            model.save_pretrained(target / "adapter")
            tokenizer.save_pretrained(target / "adapter")
        print(json.dumps({"event": "epoch_end", **metrics[-1], "best_epoch": best_epoch}), flush=True)
    training_seconds = time.monotonic() - training_start
    model.gradient_checkpointing_disable()
    model.disable_input_require_grads()
    # 只按验证损失选检查点；留出测试不用于选择或调参。
    model.load_adapter(target / "adapter", adapter_name="selected", is_trainable=False)
    model.set_adapter("selected")
    model.eval()
    for row in comparisons:
        check_stop()
        row["after"] = answer(model, tokenizer, row["prompt"])
        check_stop()
        dump(target / "comparison.json", comparisons)
        print(json.dumps({"event": "evaluation", "id": row["id"]}), flush=True)
    summary = {"run_dir": str(target), "baseline_validation_loss": baseline_val,
        "best_validation_loss": best_loss, "best_epoch": best_epoch, "updates": updates,
        "training_seconds": training_seconds, "total_seconds": time.monotonic() - start,
        "peak_allocated_gb": torch.cuda.max_memory_allocated() / 2**30,
        "peak_reserved_gb": torch.cuda.max_memory_reserved() / 2**30,
        "test_count": len(comparisons), "completed": True}
    check_stop()
    dump(target / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if output_prefix != 'desktop-smoke':
        try:
            from epistemology import record_training
            print(json.dumps({'event': 'training_experience', **record_training(summary)}, ensure_ascii=False), flush=True)
        except Exception as exc:
            # Saved weights remain valid even if the optional experience store is unavailable.
            print(json.dumps({'event': 'memory_warning', 'message': str(exc)}, ensure_ascii=False), flush=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--initial-adapter", type=Path)
    parser.add_argument("--agent", action="store_true", help="输出独立的Agent适配器，保留普通聊天模型")
    parser.add_argument("--learning-rate", type=float, default=0.0001)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--output-prefix", choices=["desktop-smoke"], help="隔离桌面集成测试产物，不替换日常模型")
    args = parser.parse_args()
    if not 1 <= args.epochs <= 10:
        parser.error("epochs 必须为 1–10")
    # The worker, rather than its launcher UI, owns the lease. Keep the file
    # handle alive until this process exits (including error/forced stop), so
    # CUDA teardown cannot race a new client's model load. Tool installation
    # never enters this main block and does not reserve compute resources.
    _training_compute_lease = ComputeLease('training').acquire()
    run(args.epochs, args.data_dir, args.initial_adapter, args.output_prefix or ("qwen-agent-lora" if args.agent else "qwen-lora"),
        args.learning_rate, args.stop_file)
