# Auto Moniter

[English](README.md) | **简体中文**

Auto Moniter 通过 Worker 实现、Reviewer 独立审核和已配置的验收检查执行开发任务，可使用命令行或本地 Web 控制台。

## Worker、Reviewer 与 Watcher

| 角色 | 职责 |
| --- | --- |
| Worker（执行器） | 实现当前派单，运行构建和定向测试，报告已完成项、缺口与证据。 |
| Reviewer（审查器） | 只读检查需求、代码和已有结果，发现问题并复核修复，不修改项目。 |
| Watcher（监督器） | 下发任务、维护问题台账、执行已批准的验收命令，核对条件后确认交付。 |

带 Reviewer 的新任务默认使用 `review-first`：先由审查器完善需求，再交执行器实现；审核发现的问题返回执行器继续修复。需求为 `needs_input` 时，先补齐缺失决定或条件，再下发实现。

完成候选和报告的外部阻塞会触发审核；尚未完成的工作按 `--review-every` 和质量策略进行周期审核。每次任务在原始需求验收完成后结束；需要继续补充下一份需求时，须在原请求中明确提出。Reviewer 默认每轮使用新会话。

## 环境要求

| 组件 | 使用条件 |
| --- | --- |
| Linux、Python 3.11+ | 运行本项目的基础环境。 |
| Git | 本地代码提交和并行任务工作副本需要；启动前配置提交作者。 |
| Agent CLI | 安装并完成所选执行器和审查器的认证，确保两者均可独立运行。 |
| Node.js 22 | JavaScript 和 HTML 内联 JavaScript 的函数范围检查需要；Agent 安装器可能有其他版本要求。 |
| Bubblewrap（`bwrap`） | `strict` 隔离需要，Web 控制台的审查器默认使用该模式。 |
| 目标项目工具链 | 在任务环境中准备目标项目的编译器、依赖和测试工具。 |

基础 Python 包使用标准库，Web 控制台无需构建前端。其他语言的函数解析器通过 `languages` 可选依赖安装。

## 安装

```sh
git clone https://github.com/sdnlink/the_last_km.git
cd the_last_km
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

需要检查 TypeScript/TSX、Go、Rust、Java、C/C++、C#、Kotlin、Swift、Ruby、PHP、Lua 或 Bash 的函数范围时，在同一 Python 环境安装解析器：

```sh
python3 -m pip install -e '.[languages]'
```

新终端中重新激活 `.venv`，然后检查安装和 CLI 可用性：

```sh
auto-moniter --version
auto-moniter providers
auto-moniter doctor --cwd /path/to/project
auto-moniter selftest
```

`providers` 检查是否能找到 CLI，`doctor` 检查本地运行条件，两者均不验证账号认证或模型访问权限。`selftest` 使用本地夹具，不调用模型。

目标仓库尚未配置提交作者时，先设置：

```sh
git -C /path/to/project config user.name "Your Name"
git -C /path/to/project config user.email "you@example.com"
```

## 支持的 Worker 与 Reviewer

下表列出本项目当前适配器的支持范围。第一列标识用于 `--provider`（Worker）或 `--reviewer`（Reviewer）。

| Agent 标识 | CLI 命令 | Worker 默认模式 | Reviewer 支持 |
| --- | --- | --- | --- |
| `codex` — Codex | `codex` | `full-access` | 支持，使用 `read-only`。 |
| `claude` — Claude Code | `claude` | `bypassPermissions` | 支持，使用 `plan`。 |
| `grok` — Grok | `grok` | `bypassPermissions` | 支持，使用 `plan`。 |
| `antigravity` — Antigravity CLI | `agy` | `bypass` | 支持，使用 `plan`。 |
| `gemini` — Gemini CLI | `gemini` | `yolo` | 支持，使用 `plan`。 |
| `cursor` — Cursor Agent | `cursor-agent` | `force` | 支持，使用 `plan`。 |
| `codebuddy` — CodeBuddy | `codebuddy` | `bypassPermissions` | 支持，使用 `plan`。 |
| `copilot` — GitHub Copilot CLI | `copilot` | 自动执行 | 仅支持 Worker。 |
| `hermes` — Hermes | `hermes` | `yolo` | 仅支持 Worker。 |
| `kimi` — Kimi Code CLI | `kimi` | `auto` | 仅支持 Worker。 |
| `glm` — GLM | `claude`，使用独立 GLM 配置 | `bypassPermissions` | 支持，使用 `plan`。 |
| `deepseek` — DeepSeek | `claude`，使用独立 DeepSeek 配置 | `bypassPermissions` | 支持，使用 `plan`。 |
| `paseo` — Paseo daemon | `paseo` | 随所选后端确定 | 仅支持 Claude 后端的 `plan` 模式。 |
| `command` — 自定义命令 | `--exec` 指定的命令模板 | 由模板确定 | 仅支持 Worker，通过 CLI 配置。 |

Copilot、Hermes、Kimi 和 CodeBuddy 每轮启动新的 CLI 会话，Watcher 持续传入当前派单和上轮反馈；其他适配器按各自能力续接 Worker 会话。原生 Reviewer 使用适配器支持的审核模式；`strict` 隔离通过 Bubblewrap 增加操作系统只读边界。

运行 `auto-moniter providers` 查看本机发现结果。实际选择的两个角色均需完成安装和认证；找到可执行文件不代表已能访问模型。

## 配置 Agent

在 Web 控制台打开“Agent 管理”，点击名称配置模型、服务地址、认证和可执行文件路径。可复用已有 CLI，也可从页面安装；安装进度显示在 Agent 列表上方。

需要 CLI 登录的执行器，应使用运行 Auto Moniter 的同一系统用户在终端完成登录。审查器须支持只读或规划模式。各执行器的角色支持和配置见 [Agent 安装与配置](console/AGENT_SETUP.md)。

控制台配置保存于 `<data-dir>/agents.json`。命令行任务需要复用时，指定其绝对路径：

```sh
export AUTO_MONITER_AGENT_SETTINGS=/path/to/console-data/agents.json
```

未设置该变量时，命令行任务沿用 Agent 自身的本机配置。API Key 通过 Agent 或控制台配置填写。

在配置窗口启用 Agent，并填写默认模型。可执行文件路径留空时自动发现，也可填写绝对路径；已有密钥留空保存会保留旧值，通过清除选项删除。GLM、DeepSeek 须分别配置自己的 API Key；Kimi 的 API Key 模式还需填写模型 ID，并选择对应接口类型。

控制台启动任务时自动加载这些配置。以服务方式运行时，先为服务用户完成认证并导出需要的环境变量，再启动服务。安装和版本检测不验证账户额度或模型访问能力。

## 使用 Web 控制台

在本项目根目录启动：

```sh
./start.sh
```

打开 `http://127.0.0.1:8765`。也可运行 `python3 -m console`，安装后可使用 `auto-moniter-console`。指定端口、默认项目和外部数据目录：

```sh
./start.sh --port 9000 --cwd /path/to/project \
  --data-dir ~/.local/state/auto-moniter-console
```

1. 在“Agent 管理”中配置执行器和审查器。
2. 创建任务，填写项目路径、开发需求和完成标准。
3. 扫描已有检查，选择对应项目并检查运行前提；需要时补充验收命令。
4. 设置模型、运行时长、调用预算、审核频率和隔离方式，然后启动。
5. 在“实时协作”中跟进执行，在任务详情中查看需求、结论、Git 记录和日志。

默认先审核需求，再执行实现。执行验收模式会运行检查；仅语义审核模式不执行功能测试；制定验收方案模式单独产出方案。逐函数派单需要已批准的可执行检查。

通过“补充要求 / 调整方向”更新任务。对可恢复的停止任务，发送要求会同时尝试恢复执行；已有补充要求无需重复填写，输入框留空即可继续发送。预算耗尽时，在恢复窗口明确追加预算。

控制台监听本机回环地址，远程访问可使用 SSH 隧道：

```sh
ssh -L 8765:127.0.0.1:8765 user@server
```

## 使用命令行执行任务

将项目路径、执行器、审查器和检查命令替换为实际配置：

```sh
auto-moniter run \
  --cwd /path/to/project \
  --provider claude \
  --reviewer codex \
  --workflow review-first \
  --guard \
  --max-runtime 2h \
  --max-agent-calls 100 \
  --check 'python3 -m unittest discover -s tests -q' \
  '增加健康检查接口，并验证成功和失败情况。'
```

新建的 review-first 任务默认由 Reviewer 逐函数派单，使用 `--no-function-dispatch` 可改为较大范围的任务。多条验收命令分别使用 `--check` 配置。通过审核与验收的代码提交到任务对应的本地业务分支。

| 参数 | 用途 |
| --- | --- |
| `--model`、`--reviewer-model` | 分别指定执行器和审查器模型。 |
| `--review-every 5` | 每累计五轮 Worker 触发进度审核；等待当前轮正常结束并返回有效结论后开始审核。 |
| `--review-every 0` | 关闭按轮次的周期审核；完成候选、阻塞和质量策略检查点仍触发审核。 |
| `--max-rounds N` | 限制 Worker 轮数，`0` 表示不限制；与审核间隔分别设置。 |
| `--max-runtime 2h`、`--max-agent-calls 100` | 设置累计运行时长和模型调用次数预算。 |
| `--turn-timeout 30m`、`--idle-timeout 15m` | 限制单轮时长和 CLI 无输出时长。 |
| `--review-isolation strict` | 使用 Bubblewrap 强制只读审核，需支持的本地原生审查器；CLI 默认值为 `provider`。 |
| `--worker-isolation strict --protect tests` | 使用 Bubblewrap 保护验收文件，运行记录目录须位于目标项目外。 |
| `--guard` | 监督进程意外退出后，在配置的次数上限内自动重启。 |

全部参数见 `auto-moniter run --help`。未安装时，可在本项目根目录用 `python3 -m auto_moniter` 替换 `auto-moniter`。

### 从文件读取需求与指定模型

通过 UTF-8 文件提供需求：

```sh
auto-moniter run --cwd /path/to/project \
  --provider claude --reviewer codex \
  --file requirements.md \
  --check 'python3 -m unittest discover -s tests -q'
```

未指定需求位置参数及 `--file` 时，也可通过管道从标准输入传入需求。使用 `--model worker-model-id`、`--reviewer-model reviewer-model-id` 分别选择当前账号可用的模型；Agent 标识用于选择适配器，模型 ID 用于选择该执行器下的模型。

### 原生 CLI 权限模式

`--mode` 设置 Worker 权限，`--reviewer-mode` 设置 Reviewer 权限。例如使用 Grok 实现、Claude 审查：

```sh
auto-moniter run --cwd /path/to/project \
  --provider grok --mode bypass \
  --reviewer claude --reviewer-mode plan \
  --check 'python3 -m unittest discover -s tests -q' \
  '实现账号锁定，验证失败阈值、解锁和计数复位。'
```

| 适配器 | 可指定的 Worker 模式 |
| --- | --- |
| Codex | `full-access`、`auto`、`read-only`。 |
| Claude / Grok | `default`、`acceptEdits`、`auto`、`dontAsk`、`bypassPermissions`、`plan`。 |
| Gemini | `default`、`auto_edit`、`yolo`、`plan`。 |
| Cursor | `default`、`force`、`plan`。 |
| Antigravity | `default`、`bypass`、`plan`。 |

`bypass`、`full-access` 等常用别名会按实际后端转换。Reviewer 仅允许原生 Codex 的 `read-only` 或受支持适配器的 `plan`。Copilot、Hermes、Kimi 沿用适配器的 Worker 设置，不支持通过 `--mode` 覆盖。CodeBuddy 和基于 Claude 的 GLM、DeepSeek 配置见上方支持表。

附加 CLI 参数使用可重复的 `--extra-arg=VALUE`、`--reviewer-extra-arg=VALUE`；Reviewer 修改权限、配置、工作目录或会话归属的覆盖参数会被拒绝。

### 通过 Paseo 运行

先准备 Paseo daemon 并完成所选后端认证，启动时明确选择后端。Paseo Claude 负责实现、原生 Codex 负责审查：

```sh
auto-moniter run --cwd /path/to/project \
  --provider paseo --paseo-backend claude --mode bypass \
  --reviewer codex \
  --check 'python3 -m unittest discover -s tests -q' \
  '实现接口需求并补充对应回归测试。'
```

两个角色都通过 Paseo 时，Reviewer 选择 Claude：

```sh
auto-moniter run --cwd /path/to/project \
  --provider paseo --paseo-backend codex --mode full-access \
  --reviewer paseo --reviewer-paseo-backend claude \
  --reviewer-mode plan --review-isolation provider \
  --check 'python3 -m unittest discover -s tests -q' \
  '实现接口需求并补充对应回归测试。'
```

Paseo Claude Worker 默认 `bypass`，Paseo Codex Worker 默认 `full-access`。Paseo Grok Worker 使用后端默认权限，应省略 `--mode` 和 `--paseo-mode`。Paseo Codex、Grok 可作 Worker；审核选择受支持的原生 Reviewer 或 Paseo Claude。Paseo 审核必须使用新会话和 `provider` 隔离；strict Worker 隔离同样要求原生 CLI。

兼容参数 `--paseo-mode`、`--reviewer-paseo-mode` 仍可使用，每个角色选择一种模式参数即可。

### 自定义 Worker 命令

可以自行编写包装脚本，将任务和结论 schema 转换给其他 Agent：

```sh
auto-moniter run --cwd /path/to/project \
  --provider command \
  --exec 'python3 /path/to/agent_wrapper.py --prompt-file {prompt_file} --schema {schema}' \
  --reviewer codex \
  --check 'python3 -m unittest discover -s tests -q' \
  '实现需求并报告验证结果。'
```

包装脚本须返回符合所提供 schema 的结构化结论。命令模板支持 `{prompt}`、`{prompt_file}`、`{cwd}`、`{schema}`、`{session_id}`、`{model}`，替换值已做 shell 转义。包装脚本支持续接会话时，可设置 `--resume-exec`。自定义命令仅用作 Worker。

明确需要省略独立审核、依赖 Worker 结论时，使用 `--workflow legacy --no-review`；该模式仍检查证据及配置的验收命令。

## 验收命令、模块与质量策略

`--accept` 补充文字完成标准，`--check` 指定 Watcher 在目标项目中执行的 shell 命令，多条检查分别填写：

```sh
auto-moniter run --cwd /path/to/project \
  --provider claude --reviewer codex \
  --accept '合法请求成功，非法请求被拒绝并返回明确错误。' \
  --check 'python3 -m unittest discover -s tests -q' \
  --check 'git diff --check' \
  --check-timeout 10m \
  '增加接口功能。'
```

根据需求配置真正验证行为的命令。环境说明、服务器地址和文字要求放入需求或配置中，检查字段填写可执行命令；仅格式检查通过不能证明功能可用。

Web 控制台支持扫描已有测试入口并检查前提，扫描本身不运行测试。已有任务需要补充检查时，先停止，在“恢复执行 → 追加验收命令”中添加；原检查命令和编号保留。

### 固定模块清单

将模块保存为 `modules.json`，使用稳定 ID；存在先后关系时填写依赖：

```json
[
  {
    "id": "API",
    "title": "接口行为",
    "acceptance": ["合法请求成功，非法请求被拒绝"],
    "files": ["app/api.py", "tests/test_api.py"],
    "checks": ["python3 -m unittest tests.test_api -q"]
  },
  {
    "id": "FINAL",
    "title": "集成验收",
    "acceptance": ["端到端请求满足原始需求"],
    "depends_on": ["API"]
  }
]
```

通过 `--modules modules.json` 加载。运行中保持原模块定义，最终交付须覆盖全部适用模块和依赖。

### 逐函数派单与质量配置

启用逐函数派单时，Reviewer 指定目标函数、允许文件、验收条目 ID、批准检查 ID 及新增的定向测试文件。Watcher 核对范围并运行所选回归后再推进。需要较大范围的派单时，使用 `--no-function-dispatch`。语言解析器依赖见安装部分。

可将可选配置保存为 `quality-policy.json`：

```json
{
  "protect_tests": true,
  "verified_progress": true,
  "no_progress_limit": 12,
  "review_interval_s": 600,
  "review_changed_files": 20,
  "blocked_review_limit": 3,
  "requirements": [],
  "validation_paths": [],
  "review_tools": "provider",
  "max_tokens": 0,
  "max_cost_usd": 0
}
```

通过 `--quality-policy quality-policy.json` 加载；Web 中可填写到高级质量配置。示例保护已有验收资产、要求可验证进展并增加审核检查点。`requirements` 关联固定模块与证据，`validation_paths` 保护额外文件；多个文件的函数必须联动时，预先配置批准的 `integration_scopes`。字段说明与示例见 [质量策略](QUALITY_POLICY.md)。

Token 和费用预算依赖所选后端提供的用量数据。时间参数填写秒数或单个 `s`、`m`、`h`、`d` 后缀，例如 `30m`；字节上限填写整数。

## 同工程并行开发

在控制台创建任务时启用“同工程并行开发”，为每个任务填写负责的文件或目录，包含新增测试范围，目录以 `/` 结尾。范围重叠时先拆分，或填写前置任务的完整 ID。

每个任务从已提交代码创建独立 Git 工作副本和 `auto-moniter-task/<任务ID>` 分支。为副本准备所需依赖，验收命令使用项目相对路径。

Watcher 通常每三秒观察共享输入及同伴变更。发现变化后使过期结论失效，交 Reviewer 复核并更新 Worker 派单；声明的前置任务须由 Watcher 最终验收后才合入。合入冲突保留现有工作并重新审查。任务在自己的分支交付，合入主分支另行操作。

## 查看、暂停与恢复

每次启动会打印运行记录目录，用实际路径查看或控制任务：

```sh
auto-moniter status /path/to/run
auto-moniter watch /path/to/run --once
auto-moniter pause /path/to/run
auto-moniter continue /path/to/run
auto-moniter stop /path/to/run
```

暂停在当前轮结束后生效。停止或中断后恢复时，使用原项目路径和角色配置，保留原有模型、后端、模式及隔离参数：

```sh
auto-moniter run --resume \
  --run-dir /path/to/run \
  --cwd /path/to/project \
  --provider claude --reviewer codex
```

恢复保留需求、检查、已有工作和累计用量。先解决报告中的阻塞或追加对应预算，再恢复执行。并行任务的 `--cwd` 使用其独立工作副本路径。

Web 中先停止任务，再通过“临时调整角色”更换受支持的原生 Worker、Reviewer 或模型，然后恢复。原始需求和累计用量保留；Paseo、自定义命令及附加参数不兼容的配置需建立后续任务。

认证错误先修复所选 CLI 的登录或配置；缺少构建或测试依赖时，在任务环境补齐。`needs_input` 需求须提供所报告的决定或证据后才能实现。具体原因查看 `report.json`、角色日志和验收检查结果。

## 数据目录与维护

| 配置 | 默认值或用途 |
| --- | --- |
| CLI 状态目录 | `$XDG_STATE_HOME/auto-moniter/`；未设置时使用 `~/.local/state/auto-moniter/`。 |
| `AUTO_MONITER_STATE_DIR` | 指定 CLI 状态及受管工作副本的根目录。 |
| `--run-dir` | 指定单个 CLI 任务的运行记录目录。 |
| 控制台 `--data-dir` | 默认 `<cwd>/.auto-moniter/console`，保存 Agent 配置、任务定义及控制台启动任务的记录。 |
| 控制台 `--runs-dir` | 额外扫描已有任务的目录，可重复指定。 |

每次运行将原始需求保存为 `requirement.md`，检查点保存为 `state.json`，结束状态保存为 `report.json`，角色输出保存为 `NNN-worker.log`、`NNN-reviewer.log`；已执行的验收检查另有日志和结果记录。诊断和恢复时保留这些文件。

查看占用并预览清理：

```sh
auto-moniter runs --cwd /path/to/project
auto-moniter cleanup --cwd /path/to/project --older-than 7d --keep-runs 5
auto-moniter history list --cwd /path/to/project
```

清理范围核对后再增加 `--apply` 执行。`--keep-rounds` 控制保留的轮次详情；`--max-run-bytes`、`--max-history-bytes`、`--max-agent-state-bytes` 设置存储上限，支持关闭的预算项设为 `0` 可禁用对应上限。

Review-first 任务将审核通过的代码提交到当前检出的本地业务分支，审计记录另存，Watcher 不自动推送远程；并行任务提交到各自任务分支。同一工作树的锁阻止多个任务同时写入同一副本。

部署与恢复见 [运维说明](OPERATIONS.md)，全部参数见 [CLI 参考](docs/cli-reference.md)，检查选择见 [验收指南](ACCEPTANCE_GUIDE.md)，页面操作见 [控制台使用说明](console/README.md)。
