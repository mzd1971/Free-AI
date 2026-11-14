"""重新加载独立适配器，确认权重非空、非初始状态并复现已保存的生成结果。"""
import json
import argparse
import torch
from safetensors.torch import load_file
from qwen_chat import load_latest
from finetune import answer, dump
from paths import relocated_path


def main(agent=False):
    model, tokenizer, run = load_latest(agent_model=agent)
    tensors = load_file(str(run / "adapter" / "adapter_model.safetensors"))
    assert all(torch.isfinite(value).all() for value in tensors.values())
    b_weights = [value for key, value in tensors.items() if "lora_B" in key]
    assert b_weights and all(torch.count_nonzero(value).item() > 0 for value in b_weights)
    comparisons = json.loads((run / "comparison.json").read_text(encoding="utf-8"))
    expected = next(row for row in comparisons if row["id"] == "general_003")
    actual = answer(model, tokenizer, expected["prompt"])
    assert actual["text"] == expected["after"]["text"], (actual, expected["after"])
    result = {"finite_adapter_tensors": len(tensors), "nonzero_lora_B_matrices": len(b_weights),
              "reloaded_generation_matches": True, "example_id": expected["id"],
              "adapter_bytes": (run / "adapter" / "adapter_model.safetensors").stat().st_size}
    config = json.loads((run / "run_config.json").read_text(encoding="utf-8"))
    if config.get("initial_adapter"):
        from pathlib import Path
        initial = load_file(str(relocated_path(config["initial_adapter"]) / "adapter_model.safetensors"))
        changed = sum(not torch.equal(value, initial[key]) for key, value in tensors.items())
        assert changed > 0
        result["changed_tensors_vs_initial_adapter"] = changed
    dump(run / "verification.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", action="store_true")
    main(parser.parse_args().agent)
