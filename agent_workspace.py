"""Bounded text search and scoped guidance in a selected project."""

from __future__ import annotations

import os
import stat


SEARCH_ENTRIES = 2500
SEARCH_FILES = 350
SEARCH_BYTES = 8 * 1024 * 1024
SEARCH_OUTPUT = 3400
SEARCH_LINE = 220
INSTRUCTION_FILE_BYTES = 16 * 1024


def _kind(agent, parts):
    from project_agent import _checked_stat

    relative = '/'.join(parts)
    if not relative:
        return agent._path('.', directory=True), True
    parent = agent._path('/'.join(parts[:-1]) or '.', directory=True)
    info = _checked_stat(parent / parts[-1])
    directory = info is not None and stat.S_ISDIR(info.st_mode)
    return agent._path(relative, directory=directory), directory


def _snippet(line, match, case_sensitive):
    if len(line) <= SEARCH_LINE:
        return line
    if not case_sensitive and not line.isascii():
        folded_offset = 0
        for index, char in enumerate(line):
            folded_offset += len(char.casefold())
            if folded_offset > match:
                match = index
                break
    start = max(0, min(match - 28, len(line) - SEARCH_LINE))
    end = start + SEARCH_LINE
    return ('…' if start else '') + line[start:end] + ('…' if end < len(line) else '')


def _directory_entries(agent, path, parts, remaining):
    from project_agent import _pinned

    names = []
    with _pinned(agent.workspace, parts, directory=True), os.scandir(path) as entries:
        for entry in entries:
            if agent.cancel.is_set():
                break
            if len(names) == remaining:
                return names, True
            names.append(entry.name)
    return names, False


def search_text(agent, query, path='.', limit=30, case_sensitive=False):
    """Return one match per line, omitting unsupported files and bounding the scan."""
    from project_agent import (BINARY_EXTENSIONS, FILE_LIMIT, IGNORED, ToolError, _decode,
                               _json, _limit, _path_parts, _pinned, _private, _read_bytes)

    if (not isinstance(query, str) or not 1 <= len(query) <= 160
            or query.splitlines() != [query] or '\x00' in query):
        raise ToolError('搜索字面量必须是 1 到 160 个字符的单行文本。')
    try:
        query.encode('utf-8')
    except UnicodeError as exc:
        raise ToolError('搜索字面量必须是 UTF-8 文本。') from exc
    if type(case_sensitive) is not bool:
        raise ToolError('是否区分大小写必须是布尔值。')
    maximum = _limit(limit, 1, 100, '搜索结果数')
    scope = _path_parts(path, root_ok=True)
    if any(part.casefold() in IGNORED or part.casefold().startswith('.freeai-') for part in scope):
        raise ToolError('搜索范围包含项目工具忽略的路径。')
    needle = query if case_sensitive else query.casefold()

    pending = [scope]
    discovered = scanned = total_bytes = 0
    output_bytes = 100
    matches = []
    truncated = False
    while pending and not agent.cancel.is_set():
        if scanned >= SEARCH_FILES or total_bytes > SEARCH_BYTES - FILE_LIMIT:
            truncated = True
            break
        parts = pending.pop()
        relative = '/'.join(parts) or '.'
        try:
            relative.encode('utf-8')
            if _path_parts(relative, root_ok=True) != parts:
                continue
            with _pinned(agent.workspace, parts):
                target, directory = _kind(agent, parts)
                if directory:
                    names, overflow = _directory_entries(agent, target, parts, SEARCH_ENTRIES - discovered)
                    discovered += len(names)
                    truncated |= overflow
                    for name in sorted(names, key=lambda name: (name.casefold(), name), reverse=True):
                        lowered = name.casefold()
                        if (lowered not in IGNORED and not lowered.startswith('.freeai-')
                                and not _private(name)):
                            pending.append((*parts, name))
                    continue
                if target.suffix.casefold() in BINARY_EXTENSIONS:
                    continue
                scanned += 1
                data = _read_bytes(target)
                if data is None:
                    if parts == scope:
                        raise ToolError('搜索路径不存在。')
                    continue
                total_bytes += len(data)
                text = _decode(data).removeprefix('\ufeff')
                if text != agent._redact(text):
                    raise ToolError('文件含有 API 凭据。')
        except (OSError, ToolError, UnicodeError):
            if parts == scope:
                raise
            continue

        for number, line in enumerate(text.splitlines(), 1):
            if agent.cancel.is_set():
                break
            index = (line if case_sensitive else line.casefold()).find(needle)
            if index < 0:
                continue
            snippet = _snippet(line, index, case_sensitive)
            row = {'path': relative, 'line': number, 'text': snippet}
            row_bytes = len(_json(row).encode('utf-8')) + 1
            if len(matches) >= maximum or output_bytes + row_bytes > SEARCH_OUTPUT:
                truncated = True
                pending.clear()
                break
            matches.append(row)
            output_bytes += row_bytes
            truncated |= snippet != line
    cancelled = agent.cancel.is_set()
    result = {'ok': not cancelled, 'matches': matches, 'scanned': scanned,
              'truncated': truncated or cancelled}
    if cancelled:
        result['status'] = 'cancelled'
    return result


def read_instructions(agent, target='.', budget=2200):
    """Read AGENTS.md along a single workspace path, with the nearest scope last."""
    from project_agent import ToolError, _decode, _limit, _path_parts, _pinned, _read_bytes

    budget = _limit(budget, 0, 8192, '项目约定字节数')
    parts = _path_parts(target, root_ok=True)
    if not budget or agent.cancel.is_set():
        return {'context': '', 'files': [], 'warnings': []}

    with _pinned(agent.workspace, parts):
        _, directory = _kind(agent, parts)
    scopes = parts if directory else parts[:-1]
    documents = []
    unreadable = 0
    for depth in range(len(scopes) + 1):
        if agent.cancel.is_set():
            return {'context': '', 'files': [], 'warnings': []}
        instruction = (*scopes[:depth], 'AGENTS.md')
        relative = '/'.join(instruction)
        try:
            with _pinned(agent.workspace, instruction):
                data = _read_bytes(agent._path(relative), INSTRUCTION_FILE_BYTES)
            if data is not None:
                text = _decode(data).removeprefix('\ufeff').strip()
                if text != agent._redact(text):
                    raise ToolError('文件含有 API 凭据。')
                if text:
                    documents.append((relative, text))
        except (OSError, ToolError):
            unreadable += 1

    blocks, files, warnings = [], [], []
    clipped = False
    for index, (relative, text) in enumerate(reversed(documents)):
        header = f'### {relative}\n'
        body = text.encode('utf-8')
        overhead = len(header.encode('utf-8')) + (2 if blocks else 0)
        # Keep room for broader guidance when a deeply scoped file is long.
        share = max(budget // 2, overhead + 4) if index < len(documents) - 1 else budget
        prefix = body[:max(0, min(share, budget) - overhead)].decode('utf-8', errors='ignore')
        clipped |= prefix != text
        if prefix:
            blocks.append(header + prefix)
            files.append(relative)
            budget -= overhead + len(prefix.encode('utf-8'))
    if unreadable:
        warnings.append(f'{unreadable} 份项目约定无法读取。')
    if clipped:
        warnings.append('项目约定超出容量，已截短。')
    return {'context': '\n\n'.join(reversed(blocks)), 'files': list(reversed(files)), 'warnings': warnings}
