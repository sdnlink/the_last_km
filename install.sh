#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"

package=.
start=false
for argument in "$@"; do
    case "$argument" in
        --languages) package='.[languages]' ;;
        --start) start=true ;;
        -h|--help)
            printf '%s\n' '用法：./install.sh [--languages] [--start]' \
                '在项目的 .venv 中安装 Auto Monitor。' \
                '  --languages   一并安装其他语言的函数解析器' \
                '  --start       安装后启动控制台，首次访问首页进入配置指引'
            exit 0 ;;
        *) printf '未知选项：%s（请运行 ./install.sh --help）\n' "$argument" >&2; exit 2 ;;
    esac
done

supported_python() {
    "$1" -c 'import sys; raise SystemExit(not (sys.platform == "linux" and sys.version_info >= (3, 11)))'
}

venv_python=.venv/bin/python
if [[ ! -x "$venv_python" ]]; then
    bootstrap_python=
    candidates=(python3 python3.14 python3.13 python3.12 python3.11)
    if [[ -n "${AUTO_MONITER_PYTHON:-}" ]]; then
        candidates=("$AUTO_MONITER_PYTHON")
    fi
    for candidate in "${candidates[@]}"; do
        if command -v "$candidate" >/dev/null && supported_python "$candidate"; then
            bootstrap_python="$candidate"
            break
        fi
    done
    if [[ -z "$bootstrap_python" ]]; then
        printf '%s\n' '安装需要 Linux 和 Python 3.11 或更高版本。可用 AUTO_MONITER_PYTHON 指定解释器。' >&2
        exit 1
    fi
    printf '%s\n' '创建项目虚拟环境 .venv…'
    if ! "$bootstrap_python" -m venv .venv; then
        printf '%s\n' '无法创建虚拟环境。请安装与该 Python 版本匹配的 venv（含 ensurepip），然后重新运行本脚本。' >&2
        exit 1
    fi
fi

if ! supported_python "$venv_python" || ! "$venv_python" -c 'import sys; raise SystemExit(sys.prefix == sys.base_prefix)'; then
    printf '%s\n' '现有 .venv 不是受支持的 Python 虚拟环境。请先移开它，再重新运行本脚本。' >&2
    exit 1
fi
if ! "$venv_python" -m pip --version >/dev/null 2>&1 && ! "$venv_python" -m ensurepip; then
    printf '%s\n' '无法在 .venv 中启用 pip。请补齐 Python 的 venv 支持后重试。' >&2
    exit 1
fi

printf '%s\n' '将 Auto Monitor 安装到 .venv…'
if ! "$venv_python" -m pip install -e "$package"; then
    printf '%s\n' '安装未完成。请根据上方 pip 输出检查包索引或构建依赖，再重新运行本脚本。' >&2
    exit 1
fi
"$venv_python" -m auto_moniter --version

if ! command -v git >/dev/null; then
    printf '%s\n' '启动开发任务前，请通过系统包管理器安装 Git。'
fi
if ! command -v bwrap >/dev/null; then
    printf '%s\n' '使用默认的严格审查隔离前，请通过系统包管理器安装 Bubblewrap（bwrap）。'
fi
printf '%s\n' '启动命令：./start.sh' \
    '打开 http://127.0.0.1:8765，按首次配置指引选择、安装并认证 Agent。'
if "$start"; then
    exec ./start.sh
fi
