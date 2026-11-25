"""Operator-configured source aliases for fixed acceptance commands."""

import re
from pathlib import Path


def source_aliases(values, checks):
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError('检查源码别名须为列表。')
    bindings = {}
    for item in values:
        match = re.fullmatch(r'check:([1-9][0-9]{0,3})=([A-Za-z0-9][A-Za-z0-9_-]{0,63})', item) if isinstance(item, str) else None
        if not match:
            raise ValueError('检查源码别名格式为 check:编号=目录名。')
        index, label = match.groups()
        index = int(index)
        if index > len(checks) or index in bindings:
            raise ValueError(f'检查源码别名引用不存在或重复的固定检查 check:{index}。')
        bindings[index] = label
    by_command = {}
    for index, label in bindings.items():
        command = checks[index - 1]
        if checks.count(command) > 1:
            raise ValueError(f'固定检查 check:{index} 的命令重复，无法单独绑定源码别名。')
        by_command[command] = (index, label)
    return by_command


def source_root(run: Path, cwd: Path, index: int, label: str) -> Path:
    directory = run / f'check-source-{index}'
    directory.mkdir(exist_ok=True)
    if directory.is_symlink():
        raise ValueError('检查源码别名目录不能是符号链接。')
    alias = directory / label
    try:
        alias.symlink_to(cwd, target_is_directory=True)
    except FileExistsError:
        pass
    if not alias.is_symlink() or alias.resolve() != cwd:
        raise ValueError(f'固定检查 check:{index} 的源码别名未指向当前工作区。')
    return alias
