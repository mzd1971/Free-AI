"""可阅读的字符级 Transformer：数据 → 预测 → 损失 → 更新 → 生成。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import torch
from torch import nn
from torch.nn import functional as F

from paths import ROOT, RUNS_ROOT, DATA_ROOT


@dataclass
class Config:
    context: int = 48
    width: int = 64
    heads: int = 4
    layers: int = 2


class CharTokenizer:
    """0 是未知字符；词表只从训练集创建，验证集不参与词表构造。"""
    def __init__(self, chars: list[str]):
        self.chars = chars
        self.lookup = {char: i + 1 for i, char in enumerate(chars)}

    @classmethod
    def fit(cls, text: str):
        return cls(sorted(set(text)))

    def encode(self, text: str) -> list[int]:
        return [self.lookup.get(char, 0) for char in text]

    def decode(self, ids: list[int]) -> str:
        return "".join(self.chars[i - 1] if i else "�" for i in ids)

    def __len__(self):
        return len(self.chars) + 1


class Block(nn.Module):
    def __init__(self, config: Config):
        super().__init__()
        self.norm1 = nn.LayerNorm(config.width)
        self.attention = nn.MultiheadAttention(config.width, config.heads, batch_first=True)
        self.norm2 = nn.LayerNorm(config.width)
        self.ffn = nn.Sequential(nn.Linear(config.width, 4 * config.width), nn.GELU(),
                                 nn.Linear(4 * config.width, config.width))

    def forward(self, x, mask):
        h = self.norm1(x)
        # True 屏蔽未来位置，防止训练时偷看答案。
        attention, _ = self.attention(h, h, h, attn_mask=mask, need_weights=False)
        x = x + attention
        return x + self.ffn(self.norm2(x))


class TinyLM(nn.Module):
    def __init__(self, vocab: int, config: Config):
        super().__init__()
        self.config = config
        self.token_embedding = nn.Embedding(vocab, config.width)
        self.position_embedding = nn.Embedding(config.context, config.width)
        self.blocks = nn.ModuleList([Block(config) for _ in range(config.layers)])
        self.norm = nn.LayerNorm(config.width)
        self.output = nn.Linear(config.width, vocab)

    def forward(self, ids):
        length = ids.shape[1]
        positions = torch.arange(length, device=ids.device)
        x = self.token_embedding(ids) + self.position_embedding(positions)
        mask = torch.ones(length, length, dtype=torch.bool, device=ids.device).triu(1)
        for block in self.blocks:
            x = block(x, mask)
        return self.output(self.norm(x))


def device_name(choice="auto"):
    if choice not in {"auto", "cpu", "cuda"}:
        raise ValueError("设备只能选择 auto、cpu 或 cuda。")
    if choice == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA 不可用，请选择 CPU 或检查 PyTorch 和显卡驱动。")
    return "cuda" if torch.cuda.is_available() and choice != "cpu" else "cpu"


def load_data(config):
    train_text = (DATA_ROOT / "train.txt").read_text(encoding="utf-8")
    val_text = (DATA_ROOT / "validation.txt").read_text(encoding="utf-8")
    if min(len(train_text), len(val_text)) <= config.context:
        raise ValueError("训练集和验证集的长度都必须超过上下文长度。")
    tokenizer = CharTokenizer.fit(train_text)
    train_ids = torch.tensor(tokenizer.encode(train_text), dtype=torch.long)
    val_ids = torch.tensor(tokenizer.encode(val_text), dtype=torch.long)
    return tokenizer, train_ids, val_ids, train_text, val_text


def batch(data, context, size, device, generator):
    starts = torch.randint(len(data) - context, (size,), generator=generator)
    x = torch.stack([data[i:i + context] for i in starts])
    y = torch.stack([data[i + 1:i + context + 1] for i in starts])
    return x.to(device), y.to(device)


@torch.inference_mode()
def evaluate(model, data, device):
    """每次用同一组窗口，曲线的差异才主要来自参数变化。"""
    generator = torch.Generator().manual_seed(2026)
    was_training = model.training
    model.eval()
    losses = []
    for _ in range(4):
        x, y = batch(data, model.config.context, 8, device, generator)
        logits = model(x)
        losses.append(F.cross_entropy(logits.reshape(-1, logits.shape[-1]), y.reshape(-1)).item())
    model.train(was_training)
    return sum(losses) / len(losses)


@torch.inference_mode()
def sample(model, tokenizer, prompt, length=120, temperature=0.8, seed=42):
    if not prompt:
        raise ValueError("请输入至少一个字符作为开头。")
    if not 1 <= length <= 512 or not 0.1 <= temperature <= 2:
        raise ValueError("生成长度需为 1–512，温度需为 0.1–2。")
    model.eval()
    device = next(model.parameters()).device
    generator = torch.Generator(device=device).manual_seed(seed)
    ids = torch.tensor([tokenizer.encode(prompt)], device=device)
    generated = []
    for _ in range(length):
        logits = model(ids[:, -model.config.context:])[:, -1, :] / temperature
        logits[:, 0] = -float("inf")  # 不输出不可读的未知字符占位符。
        next_id = torch.multinomial(F.softmax(logits, dim=-1), 1, generator=generator)
        ids = torch.cat([ids, next_id], dim=1)
        generated.append(next_id.item())
    return prompt + tokenizer.decode(generated)


def train_run(steps=200, learning_rate=0.003, seed=42, device="auto", output_root=None):
    """生成器：每次返回真实日志，界面和命令行共用相同训练代码。"""
    steps = int(steps)
    seed = int(seed)
    if not 1 <= steps <= 2000:
        raise ValueError("训练步数需为 1–2000。")
    if not math.isfinite(learning_rate) or not 0 < learning_rate <= 0.1:
        raise ValueError("学习率需大于 0 且不超过 0.1。")
    if not 0 <= seed < 2**32:
        raise ValueError("随机种子需在 0 到 2^32-1 之间。")
    torch.set_num_threads(4)
    random.seed(seed)
    torch.manual_seed(seed)
    device = device_name(device)
    config = Config()
    tokenizer, train_ids, val_ids, train_text, val_text = load_data(config)
    model = TinyLM(len(tokenizer), config).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    generator = torch.Generator().manual_seed(seed)
    root = Path(output_root) if output_root else RUNS_ROOT
    run_dir = root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "config": asdict(config), "steps": steps, "learning_rate": learning_rate,
        "seed": seed, "device": device, "torch": str(torch.__version__),
        "parameters": sum(p.numel() for p in model.parameters()),
        "train_characters": len(train_text), "validation_characters": len(val_text),
        "validation_unknown_ratio": (val_ids == 0).float().mean().item(),
        "train_sha256": hashlib.sha256(train_text.encode()).hexdigest(),
        "validation_sha256": hashlib.sha256(val_text.encode()).hexdigest(),
    }
    (run_dir / "config.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    # 保存当次输入，修改原始数据后仍可检查本次实验到底使用了什么。
    (run_dir / "train.txt").write_text(train_text, encoding="utf-8")
    (run_dir / "validation.txt").write_text(val_text, encoding="utf-8")
    payload = {"config": asdict(config), "chars": tokenizer.chars, "state": model.state_dict()}
    torch.save(payload, run_dir / "initial.pt")
    before = sample(model, tokenizer, "问：", length=100, seed=seed)
    rows = []
    interval = max(1, steps // 10)
    for step in range(steps + 1):
        if step % interval == 0 or step == steps:
            train_loss = evaluate(model, train_ids, device)
            val_loss = evaluate(model, val_ids, device)
            if not math.isfinite(train_loss + val_loss):
                raise ValueError("损失出现非有限值，请降低学习率后重试。")
            row = {"step": step, "train_loss": train_loss, "validation_loss": val_loss}
            rows.append(row)
            with (run_dir / "metrics.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
            event = {"run_dir": str(run_dir), "metadata": metadata, "rows": rows.copy(),
                     "before": before, "after": "训练进行中，完成后显示。", "complete": False}
            if step == steps:
                payload["state"] = model.state_dict()
                torch.save(payload, run_dir / "trained.pt")
                event["after"] = sample(model, tokenizer, "问：", length=100, seed=seed)
                event["complete"] = True
                (run_dir / "comparison.json").write_text(json.dumps({"prompt": "问：", "seed": seed,
                    "temperature": 0.8, "before": before, "after": event["after"]},
                    ensure_ascii=False, indent=2), encoding="utf-8")
            yield event
        if step == steps:
            break
        model.train()
        x, y = batch(train_ids, config.context, 16, device, generator)
        logits = model(x)
        loss = F.cross_entropy(logits.reshape(-1, logits.shape[-1]), y.reshape(-1))
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()


def load_checkpoint(path, device="auto"):
    device = device_name(device)
    saved = torch.load(path, map_location=device, weights_only=True)
    tokenizer = CharTokenizer(saved["chars"])
    model = TinyLM(len(tokenizer), Config(**saved["config"])).to(device)
    model.load_state_dict(saved["state"])
    model.eval()
    return model, tokenizer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    train = commands.add_parser("train")
    train.add_argument("--steps", type=int, default=200)
    train.add_argument("--learning-rate", type=float, default=0.003)
    train.add_argument("--seed", type=int, default=42)
    train.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    generate = commands.add_parser("generate")
    generate.add_argument("checkpoint", type=Path)
    generate.add_argument("--prompt", default="问：")
    generate.add_argument("--length", type=int, default=120)
    generate.add_argument("--temperature", type=float, default=0.8)
    generate.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args()
    if args.command == "train":
        for event in train_run(args.steps, args.learning_rate, args.seed, args.device):
            print(json.dumps({"run": event["run_dir"], **event["rows"][-1]}, ensure_ascii=False), flush=True)
    else:
        model, tokenizer = load_checkpoint(args.checkpoint, args.device)
        print(sample(model, tokenizer, args.prompt, args.length, args.temperature))


if __name__ == "__main__":
    main()
