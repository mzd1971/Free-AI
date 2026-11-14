"""Refresh the travel copy; SQLite files use consistent database snapshots."""
import shutil
import sqlite3
from paths import ARTIFACTS, ROOT


def main():
    source, target = ARTIFACTS.resolve(), (ROOT / 'portable_data').resolve()
    if source == target:
        print('你已经在使用随应用携带的数据，直接复制整个应用文件夹即可。')
        return
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('来源与目标文件夹不能互相包含。')
    if not (source / 'datasets' / 'teaching' / 'sft_train.jsonl').is_file():
        raise ValueError('没有找到完整的学习数据，未复制。')
    count = 0
    for item in source.rglob('*'):
        if not item.is_file() or item.is_symlink() or item.name.endswith(('-wal', '-shm', '.lock')):
            continue
        if '__pycache__' in item.parts:
            continue
        output = target / item.relative_to(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        if item.suffix in {'.sqlite3', '.sqlite', '.db'}:
            with sqlite3.connect(item.as_uri() + '?mode=ro', uri=True) as incoming:
                with sqlite3.connect(output) as outgoing:
                    incoming.backup(outgoing)
        else:
            shutil.copy2(item, output)
        count += 1
    print(f'已更新 {count} 个文件。随身数据位置：{target}')
    print('现在可以复制整个应用文件夹到另一台 Windows 电脑的 D 盘或其他非系统盘。')
    print('不会删除随身副本中已有的文件；复制前请停止训练并关闭应用。')


if __name__ == '__main__':
    main()
