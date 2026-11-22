"""Write Free AI's Qwen3-0.6B LoRA format without a second ML environment.

The tensor mapping and adapter metadata follow llama.cpp b11960:
https://github.com/ggml-org/llama.cpp/blob/b11960/convert_lora_to_gguf.py
https://github.com/ggml-org/llama.cpp/blob/b11960/conversion/qwen.py
https://github.com/ggml-org/llama.cpp/blob/b11960/src/llama-adapter.cpp
The fixed dimensions come from Qwen3-0.6B's pinned training config.
"""
from __future__ import annotations

import json
import math
import struct


class AdapterError(ValueError):
    """An adapter is damaged or uses an unsupported PEFT configuration."""


# GGUF v3 stores little-endian scalars and arrays padded to 32 bytes.
_ALIGNMENT = 32
_GGUF_STRING = 8
_GGUF_FLOAT32 = 6
_GGML_F32 = 0
_LAYER_COUNT = 28
_HIDDEN_SIZE = 1024
_PROJECTIONS = {'q': 2048, 'v': 1024}
_HEADER_LIMIT = 128 * 1024
_UNSUPPORTED_OPTIONS = (
    'use_dora', 'use_rslora', 'use_qalora', 'fan_in_fan_out', 'lora_bias',
    'modules_to_save', 'rank_pattern', 'alpha_pattern', 'target_parameters',
    'alora_invocation_tokens', 'invocation_string', 'megatron_config', 'trainable_token_indices',
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f'LoRA 权重头包含重复字段：{key}')
        result[key] = value
    return result


def _tensor_layout(rank):
    for layer in range(_LAYER_COUNT):
        for projection, width in _PROJECTIONS.items():
            source = f'base_model.model.model.layers.{layer}.self_attn.{projection}_proj'
            target = f'blk.{layer}.attn_{projection}.weight'
            yield f'{source}.lora_A.weight', f'{target}.lora_a', (rank, _HIDDEN_SIZE)
            yield f'{source}.lora_B.weight', f'{target}.lora_b', (width, rank)


def read_adapter(config, weights):
    """Validate the trainer's exact PEFT tensor layout and read its F32 bytes."""
    rank, alpha = config.get('r'), config.get('lora_alpha')
    targets = config.get('target_modules')
    if (
        config.get('peft_type') != 'LORA'
        or config.get('task_type') != 'CAUSAL_LM'
        or config.get('bias') != 'none'
        or not isinstance(targets, list) or sorted(map(str, targets)) != ['q_proj', 'v_proj']
        or any(config.get(option) for option in _UNSUPPORTED_OPTIONS)
        or type(rank) is not int or rank != 8
        or type(alpha) not in (int, float) or alpha != 16
    ):
        raise AdapterError('仅支持本训练器生成的 Qwen3 0.6B、r=8、α=16 的 q_proj/v_proj LoRA。')

    if len(weights) < 8:
        raise AdapterError('LoRA safetensors 文件缺少权重头。')
    header_length = struct.unpack_from('<Q', weights)[0]
    if not 1 <= header_length <= _HEADER_LIMIT or len(weights) < 8 + header_length:
        raise AdapterError('LoRA safetensors 权重头大小无效。')
    try:
        header = json.loads(weights[8:8 + header_length], object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise AdapterError('LoRA safetensors 权重头不是有效的 JSON。') from None
    if not isinstance(header, dict):
        raise AdapterError('LoRA safetensors 权重头必须是对象。')
    metadata = header.pop('__metadata__', {})
    if not isinstance(metadata, dict) or any(not isinstance(value, str) for value in metadata.values()):
        raise AdapterError('LoRA safetensors 文件元数据无效。')
    layout = list(_tensor_layout(rank))
    if set(header) != {name for name, _, _ in layout}:
        raise AdapterError('LoRA 权重必须包含 28 层中 q_proj 与 v_proj 的完整 A/B 矩阵。')

    data = memoryview(weights)[8 + header_length:]
    ranges = []
    tensors = []
    for name, gguf_name, shape in layout:
        record = header[name]
        if not isinstance(record, dict) or record.get('dtype') != 'F32' or record.get('shape') != list(shape):
            raise AdapterError(f'LoRA 矩阵形状或 F32 类型不符：{name}')
        offsets = record.get('data_offsets')
        if not isinstance(offsets, list) or len(offsets) != 2 or any(type(item) is not int for item in offsets):
            raise AdapterError(f'LoRA 矩阵偏移无效：{name}')
        begin, end = offsets
        if not 0 <= begin <= end <= len(data) or end - begin != math.prod(shape) * 4:
            raise AdapterError(f'LoRA 矩阵数据大小无效：{name}')
        raw = data[begin:end]
        if any(not math.isfinite(value) for (value,) in struct.iter_unpack('<f', raw)):
            raise AdapterError(f'LoRA 矩阵存在非有限数值：{name}')
        ranges.append((begin, end))
        tensors.append((gguf_name, shape, raw))
    offset = 0
    for begin, end in sorted(ranges):
        if begin != offset:
            raise AdapterError('LoRA 矩阵范围存在重叠或空洞。')
        offset = end
    if offset != len(data):
        raise AdapterError('LoRA 权重包含未声明的数据。')
    return alpha, tensors


def _string(value):
    encoded = value.encode('utf-8')
    return struct.pack('<Q', len(encoded)) + encoded


def write_adapter(path, alpha, tensors):
    """Write a GGUF v3 adapter, preserving the trainer's unquantized F32 matrices."""
    strings = {'general.type': 'adapter', 'general.architecture': 'qwen3', 'adapter.type': 'lora'}
    metadata = b''.join(_string(key) + struct.pack('<I', _GGUF_STRING) + _string(value)
                        for key, value in strings.items())
    metadata += _string('adapter.lora.alpha') + struct.pack('<If', _GGUF_FLOAT32, alpha)
    header = bytearray(struct.pack('<4sIQQ', b'GGUF', 3, len(tensors), len(strings) + 1))
    header.extend(metadata)
    offset = 0
    for name, (rows, columns), raw in tensors:
        header.extend(_string(name))
        # Qwen3 uses the original row order. GGUF lists fastest-changing dimension first.
        header.extend(struct.pack('<IQQIQ', 2, columns, rows, _GGML_F32, offset))
        offset += len(raw) + (-len(raw) % _ALIGNMENT)

    with path.open('xb') as stream:
        stream.write(header)
        stream.write(bytes(-len(header) % _ALIGNMENT))
        for _, _, raw in tensors:
            stream.write(raw)
            stream.write(bytes(-len(raw) % _ALIGNMENT))
