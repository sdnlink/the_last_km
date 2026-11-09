# 启动与运行参数完整说明

本文以当前 `auto_moniter/cli.py`、`console/server.py`、执行器适配器与恢复校验为准。说明区分新任务默认值、Web 默认值与恢复规则；模型名称示例来自本机目录，不保证其他账户可用。

## 先区分两个入口

| 入口 | 作用 | 参数归属 |
| --- | --- | --- |
| `./start.sh` 或 `python -m console` | 启动本地 Web 控制台，随后在页面创建任务 | 只接受 Web 服务参数 |
| `python -m auto_moniter run` 或安装后的 `auto-moniter run` | 直接启动一个监督开发任务 | 接受 worker/reviewer、预算、审核、验收等参数 |

`./start.sh --provider grok` 无效；worker 与 reviewer 应在 Web 新建任务表单中选择，或通过 `auto_moniter run` 指定。下文源码命令使用项目的 `.venv/bin/python`，避免系统 `python3` 版本不匹配。

### Web 服务参数

| 参数 | 默认值 | 使用方法 |
| --- | --- | --- |
| `--port PORT` | `8765` | 监听本机端口，范围 0–65535；0 由系统分配。服务固定绑定 `127.0.0.1`，没有 `--host` 参数 |
| `--cwd DIR` | 当前目录；经 start.sh 启动时是本项目目录 | 新建任务默认项目目录，不表示立即开始开发 |
| `--data-dir DIR` | `<cwd>/.auto-moniter/console` | 保存任务定义及 Web 管理的运行记录；严格 worker 隔离需将此目录放到目标项目之外 |
| `--runs-dir DIR` | 无额外目录 | 额外扫描的历史运行目录，可重复；不是本次任务的 `--run-dir` |
| `-h`, `--help` | — | 查看 Web 帮助 |

```bash
./start.sh --port 8000 --cwd /home/skn/code/auto_moniter
```

启动仅创建服务。服务启动时会枚举 CLI、Paseo 后端和模型目录；模型、执行器及独立后端在新建任务表单中选择。临时角色调整只对支持的原生任务开放，不改全局设置。

### 直接运行 Grok worker 与 Codex reviewer

在项目根目录执行，目标项目必须存在且有本地业务分支；strict 审核需要可用的 bwrap 和实际探测通过：

```bash
.venv/bin/python -m auto_moniter run \
  '按条目检查本项目功能，由 reviewer 完善需求；worker 定向修复，审核及固定检查通过后由 watcher 本地提交。' \
  --cwd /home/skn/code/auto_moniter \
  --provider grok --model grok-4.7 \
  --reviewer codex --reviewer-model gpt-6.1-sol \
  --workflow review-first --review-isolation strict \
  --git-history required --guard \
  --run-dir /tmp/auto-moniter-feature-run \
  --max-runtime 1h --max-agent-calls 24 \
  --turn-timeout 15m --idle-timeout 5m \
  --review-every 5 --keep-rounds 20 \
  --check '.venv/bin/python -m unittest discover -q' \
  --check 'node --test tests/test_agent_model_choices.mjs tests/test_console_requests.mjs tests/test_review_only_boundary.mjs' \
  --check 'git diff --check'
```

示例会实际启动模型和开发，`--run-dir` 必须是新目录。worker 默认使用可写自动批准模式；reviewer 默认使用 Codex read-only。时限是累计监控运行预算，包括模型、审核、检查、同进程暂停与退避；进程完全停止后的离线时间不计入。要严格固定某个现实时间点截止，跨停止恢复时还应扣除离线停留时间，不能只依赖累计运行预算。

## run 参数：需求、目录与流程

| 参数 | 新任务默认 | 使用方法 |
| --- | --- | --- |
| `requirement` 位置参数 | 无 | 直接传入需求文字，例如 `run '实现健康检查接口'` |
| `-f FILE`, `--file FILE` | 无 | 从 UTF-8 文件读取需求，优先于位置参数；未提供需求/文件的新任务可从标准输入读取 |
| `-C DIR`, `--cwd DIR` | 当前目录 | worker 修改和检查执行的项目目录，恢复时须匹配原项目 |
| `--run-dir DIR` | 项目外私有状态目录中的时间戳子目录 | 提示词、日志、检查点与报告目录；同目录已有任务时必须 `--resume` |
| `--workflow review-first` | 新建且有 reviewer 时选用 | 先审核需求，再实现、独立复核、watcher 固定检查与本地提交 |
| `--workflow legacy` | `--no-review` 新任务选用；也可显式指定 | 保留传统逐轮审计流程。恢复默认沿用检查点流程，不能切换 |
| `--modules FILE` | 无固定模块 | JSON 数组；每项必填 `id/title/acceptance`，可填 `depends_on/checks/files`。固定验收不可被 agent 改写 |
| `--quality-policy FILE` | 不启用额外质量策略 | 读取 JSON 质量策略；非空策略会补齐字段默认值；恢复沿用原策略，不能替换 |
| `--resume` | 否 | 恢复 `--run-dir` 中的任务，不创建新任务；须传原 provider、reviewer 和后端配置 |
| `-h`, `--help` | — | 查看实际 run 参数 |

默认运行目录根为 `AUTO_MONITER_STATE_DIR`；未设置时使用 `$XDG_STATE_HOME/auto-moniter`，再未设置则为 `~/.local/state/auto-moniter`。实际任务目录为 `<根>/<项目路径哈希>/runs/<时间戳>`。Web 默认目录另见前表。

```bash
.venv/bin/python -m auto_moniter run --file requirements.md \
  --provider grok --reviewer codex --cwd /path/to/project
```

## run 参数：执行器、模型与权限模式

| 参数 | 新任务默认 | 使用方法 |
| --- | --- | --- |
| `-p NAME`, `--provider NAME` | `paseo` | worker 执行器：`paseo/claude/codex/grok/gemini/cursor/antigravity/command` |
| `--model ID` | 对应 CLI 默认模型 | worker 的模型 ID，不是执行器名称 |
| `--mode MODE` | 适配器的 worker 自动批准模式 | 指定 worker 权限模式，按真实后端转换，见后表 |
| `--paseo-backend ID` | `paseo` | 仅 worker 为 Paseo 时使用的实际后端 ID；默认值不保证你的 daemon 注册了同名后端，通常应显式选择实际枚举出的 `grok/claude/codex` 等 |
| `--paseo-mode MODE` | 无显式覆盖 | `--mode` 的 Paseo 兼容入口；同角色避免同时设置两个不同值 |
| `--reviewer NAME` | 空，须明确指定 | 独立审查执行器；没有 reviewer 时必须明确 `--no-review`。`command` reviewer 不支持 |
| `--reviewer-model ID` | 相同执行器且相同后端时沿用 worker 模型；否则该 reviewer 默认模型 | 指定 reviewer 模型；不同 Paseo 后端不共享模型命名空间 |
| `--reviewer-mode MODE` | Codex `read-only`；其他支持后端 `plan` | reviewer 权限模式，只允许只读/plan；可写模式被拒绝 |
| `--reviewer-paseo-backend ID` | 沿用 `--paseo-backend` | reviewer 通过 Paseo 时实际使用的后端；目前只读审核仅支持 Claude 后端，需显式选择 `claude` |
| `--reviewer-paseo-mode MODE` | 无显式覆盖 | Paseo reviewer 的权限模式兼容入口，只支持 Claude plan |
| `--fresh-review` | 开启 | 每次审核使用新 reviewer 会话 |
| `--no-fresh-review` | 关闭此功能需显式设置 | 尝试续接 reviewer 独立会话；Paseo reviewer 禁止使用此选项 |
| `--no-review` | 关闭 | 省略独立审核；只能配 legacy，不能配 review-first；仍验证 worker 结论、证据和配置的检查，不等同于独立验收 |
| `--extra-arg=ARG` | 无 | 传给 worker CLI，可重复；ARG 以 `--` 开头时用等号，例如 `--extra-arg=--verbose` |
| `--reviewer-extra-arg=ARG` | 无 | 传给 reviewer CLI，可重复；权限、配置、工作目录、provider 和会话等覆盖选项会被拒绝 |

| 方式 | worker 省略模式时 | reviewer 模式 |
| --- | --- | --- |
| 原生 Codex | `full-access` | `read-only` |
| 原生 Claude、Grok | `bypassPermissions` | `plan` |
| 原生 Gemini | `yolo` | `plan` |
| 原生 Cursor | `force` | `plan` |
| 原生 Antigravity | `bypass` | `plan` |
| Paseo → Claude | `bypass` | 仅 `plan`，必须新会话且不能 strict 包装 daemon |
| Paseo → Codex | `full-access` | 不支持只读 reviewer，改用原生 Codex |
| Paseo → Grok | daemon 默认，不指定模式 | 不支持本项目要求的只读 reviewer；其 ACP 没有权限切换，不能填写 bypass/plan |
| Paseo → 其他后端 | 后端默认；未知模式 ID 原样转交 | 不支持本项目的 Paseo 只读审核入口 |

`bypass/full-access/yolo` 等常见别名会按已知后端转换；不能通过 reviewer 附加参数绕过审核权限限制。模型认证由执行器负责，密钥从服务或 CLI 进程环境/现有登录配置继承，没有 `--XAI_API_KEY` 参数。start.sh 不会替你 source 交互式 shell 文件，服务启动前须确保所需变量已 export。

Paseo worker + 原生 reviewer：

```bash
.venv/bin/python -m auto_moniter run '实现接口并增加回归' \
  --cwd /path/to/project \
  --provider paseo --paseo-backend grok --model grok-4.7 \
  --reviewer codex --reviewer-model gpt-6.1-sol \
  --max-runtime 1h
```

两个角色都通过 Paseo，但使用不同后端：

```bash
.venv/bin/python -m auto_moniter run '实现接口并增加回归' \
  --cwd /path/to/project \
  --provider paseo --paseo-backend grok --model grok-4.7 \
  --reviewer paseo --reviewer-paseo-backend claude \
  --reviewer-mode plan --review-isolation provider \
  --max-runtime 1h
```

## run 参数：审核节奏与时间预算

时间参数支持裸秒数及 `s/m/h/d`，例如 `900`、`15m`、`1h`；允许非负小数，不支持 `1h30m` 这样的组合字符串。字节参数只接受整数，不接受 `64MiB`。

| 参数 | 新任务默认 | 使用方法 |
| --- | --- | --- |
| `--max-runtime TIME` | `0`，不限制 | 累计运行预算，例如 `1h`；恢复不会清零已用时间；到期中止当前调用并保留证据 |
| `--max-agent-calls N` | `0`，不限制 | 累计调用上限，含 worker、reviewer、需求完善和审核重试，不是后端内部请求数 |
| `--max-rounds N` | `0`，不设轮数上限 | worker 轮次上限，不是 reviewer 次数；恢复沿用轮号，不从第 1 轮重算 |
| `--turn-timeout TIME` | 新任务 `0`，恢复省略则继承 | 单次角色调用硬上限，worker/reviewer 都适用。例如 `15m`；0 关闭单轮硬限制，总预算仍有效 |
| `--idle-timeout TIME` | 新任务 `15m`，恢复省略则继承 | CLI 无任何输出多久后中断本轮；不是源码未改动时间；0 关闭 |
| `--review-every N` | 新任务 `5`，恢复省略则继承 | 每 N 个 worker 轮次进行进度审核；完成候选和外部阻塞立即审核。0 仅关闭按轮数审核，质量策略仍可提前触发 |
| `--retry-delay TIME` | 新任务 `5s`，恢复省略则继承 | 重试初始退避，后续最长 60 秒，等待仍计总预算；0 取消等待 |
| `--stall-limit N` | `0`，不因这一限制停止 | 相同缺口、缺少有效进展累计达到阈值时停止；质量策略可另设无进展上限 |
| `--failure-limit N` | `0`，不因这一限制停止 | 连续执行失败上限；认证错误等不可自动恢复问题仍会直接停止 |
| `--restart-after-stall N` | `3` | 缺少进展达到间隔时清理并重建 agent 会话；0 关闭重建，不等于关闭审核或总预算 |

review-first 的质量策略 `review_interval_s` 统计 worker 累计时间，在正常返回后触发审核，不缩短单轮硬上限；legacy 保留按该间隔中断 worker 的历史行为。要仅在完成候选/外部阻塞审核，除了 `--review-every 0`，还需在质量策略中关闭 `review_interval_s` 和 `review_changed_files`。实际硬超时与预算错误不被当作完成。

## run 参数：验收和 Git 提交

| 参数 | 新任务默认 | 使用方法 |
| --- | --- | --- |
| `--accept TEXT` | 无补充标准 | 自然语言验收条款，发给 agent 判断；不会作为 shell 命令执行 |
| `--check COMMAND` | 无固定命令 | watcher 在目标项目通过 `sh -c` 执行，可重复；命令退出 0 才通过，不能替代 reviewer；review-first 的模块发布也运行固定检查 |
| `--check-timeout TIME` | 新任务 `10m`，恢复省略则继承 | 每条验收命令超时，必须大于 0；与单轮模型超时分别计量，总运行预算仍生效 |
| `--git-history auto` | 默认 | Git 项目中启用历史；普通非 Git 目录可不记录，已有异常 Git 状态不会静默忽略 |
| `--git-history required` | — | 必须能记录 Git；review-first 审核通过且检查通过后，watcher 本地提交业务分支，审计结论写隐藏引用 |
| `--git-history off` | — | 不记录 Git、不执行这套本地发布；恢复已记录历史的任务不能关闭 |

`--check` 应直接填写真实命令，例如 `--check='python -m unittest discover -q'`，而不是“运行测试并通过”。命令会实际执行并可能生成文件；改动源码会使审核失效，失效候选必须重新审核。worker/reviewer 不自行提交，watcher 不自动推送；legacy 的 Git 记录策略与新 review-first 不同，不能当作相同的业务发布流程。

模块示例（保存为 modules.json 后使用 `--modules modules.json`）：

```json
[
  {"id":"API","title":"接口","acceptance":["合法请求返回成功，非法请求被拒绝"],"files":["app/api.py","tests/test_api.py"],"checks":["python -m unittest tests.test_api -q"]},
  {"id":"FINAL","title":"集成验收","acceptance":["端到端调用满足原始需求"],"depends_on":["API"]}
]
```

质量策略示例（保存为 quality-policy.json 后使用 `--quality-policy quality-policy.json`）：

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

`protect_tests` 保护原验收资产；`verified_progress` 不把单纯编辑当成验证进展；`requirements` 建立需求/模块/证据映射，必须关联用户固定模块；`validation_paths` 保护指定验收路径。`review_tools=files_only` 仅支持原生 Claude reviewer，要求 strict，禁止 reviewer CLI 覆盖。token/费用限制依赖实际 provider 报告，不是完整的服务端账单；详见 [质量策略](../QUALITY_POLICY.md)。

## run 参数：隔离、资源和日志

| 参数 | 新任务默认 | 使用方法 |
| --- | --- | --- |
| `--review-isolation provider` | CLI 默认 | 使用执行器 read-only/plan 模式及审核写入检测 |
| `--review-isolation strict` | Web 默认 | bwrap 强制只读、独立 PID 命名空间；原生 reviewer 才支持，缺少工具或探测失败直接拒绝，不自动降级 |
| `--worker-isolation provider` | 默认 | 使用 worker 执行器自身权限控制 |
| `--worker-isolation strict` | — | 使用 bwrap 限制原生 worker 可写范围；`--run-dir` 必须在目标项目外，保护监控记录及验收资产 |
| `--protect PATH` | 无额外路径 | 严格 worker 中保护项目内既有文件/目录，可重复；必须配 `--worker-isolation strict` |
| `--memory-bytes N` | `0`，不限制 | 原生子进程继承的单进程虚拟内存上限，不是整棵进程树 RSS；Paseo worker 不支持 |
| `--file-bytes N` | `0`，不限制 | 原生子进程单文件写入大小限制；Paseo worker 不支持 |
| `--max-run-bytes N` | `0`，不限制 | 运行目录容量预算，周期采样后停止，不是文件系统硬配额 |
| `--max-history-bytes N` | `0`，不限制 | 共享 Git 对象及任务历史预算，每约 10 秒采样，包含已有历史和其他分支 |
| `--max-agent-state-bytes N` | `0`，不限制 | 本机共享模型会话目录预算，每约 10 秒采样，包含其他任务和已有会话；不自动删除它们 |
| `--max-output-bytes N` | 新任务 `8388608`（8 MiB），恢复省略则继承 | 每个受控子进程 stdout/stderr 合计捕获上限，必须大于 0，超限终止该进程组 |
| `--keep-rounds N` | CLI 新任务 `100`；恢复省略则继承 | 保留最近 N 轮详细记录和受控逐轮文件；0 保留全部；扩大窗口不能找回已清理文件 |

worker strict + 保护测试目录的示例配置片段：

```bash
--worker-isolation strict --protect tests --protect scripts/check_api.py \
--run-dir /tmp/auto-moniter-protected-run \
--max-run-bytes 67108864 --max-history-bytes 536870912 \
--max-agent-state-bytes 4294967296 --max-output-bytes 8388608
```

## run 参数：guardian 与自定义 worker

| 参数 | 新任务默认 | 使用方法 |
| --- | --- | --- |
| `--guard` | 关闭；Web 启动任务时启用 | 由父 guardian 看守 monitor，异常退出后尝试恢复；不是第二个 reviewer |
| `--guard-restarts N` | `3` | guardian 自动重启上限，已用重启数跨恢复累计；0 不自动重启 |
| `--guard-delay TIME` | `5s` | guardian 重启初始等待，退避最长 60 秒 |
| `--guard-stop-timeout TIME` | `30s` | 请求停止后的优雅退出期限，超过后强制结束，必须大于 0 |
| `--exec TEMPLATE` | 无 | 仅 `--provider command` 的 worker 启动模板，使用 `sh -c` 执行 |
| `--resume-exec TEMPLATE` | 无 | command worker 的续接模板；不提供时不能续接其 CLI 会话，可重新启动本轮 |
| `--reviewer-exec TEMPLATE` | 保留参数，不可用于自定义审核 | command reviewer 无法保证只读，当前实现拒绝启动 |
| `--reviewer-resume-exec TEMPLATE` | 保留参数，不可用于自定义审核 | 同上，不应作为可用 reviewer 功能配置 |

模板占位符：`{prompt}`、`{prompt_file}`、`{cwd}`、`{schema}`、`{session_id}`、`{model}`；替换值按 shell 参数转义，模板本身仍是你指定的 shell 代码。示例：`--provider command --exec='my-agent --cwd {cwd} --prompt-file {prompt_file}' --reviewer codex`。

## 恢复规则

```bash
.venv/bin/python -m auto_moniter run --resume \
  --cwd /home/skn/code/auto_moniter \
  --run-dir /tmp/auto-moniter-feature-run \
  --provider grok --model grok-4.7 \
  --reviewer codex --reviewer-model gpt-6.1-sol \
  --review-isolation strict --git-history required --guard
```

恢复不是重新启动一份需求。支持的检查点状态包括 running、stopped、blocked、budget_exceeded、recovery_failed、action_required；done、failed、stalled、max_rounds 等结束状态不能这样重跑。相同项目存在活动任务或业务 HEAD 外部变化会阻止不安全恢复。

- 原始需求、固定验收标准、命令、固定模块、质量策略和隔离配置须保持；省略验收/模块/质量文件时使用已保存内容。不能用新需求覆盖旧需求。
- provider、reviewer、Paseo 后端、权限模式、CLI 路径、附加参数和自定义模板必须匹配原配置。CLI 不自动从目录推断这些启动选项；Web 按保存的 launch 配置恢复。原生角色的显式临时切换另走 Web 授权入口。
- 模型建议显式填写原值，避免回到 CLI 默认模型；Paseo 续接已有 agent 使用 send，沿用 daemon 中已有模型和模式，不因本地重新填写参数就自动修改该会话。需要改变时先调整 daemon 会话，或创建新运行。
- `--turn-timeout`、`--idle-timeout`、`--retry-delay`、`--check-timeout`、`--max-output-bytes`、`--review-every`、`--keep-rounds` 省略时继承，显式提供时可调整。
- `--max-runtime`、`--max-agent-calls` 和容量/内存/文件预算传 0 或省略时，已有非零预算会继承，不能借 0 清除原预算。已用累计时间、调用数和 guardian 重启数保留。
- `--max-rounds`、`--stall-limit`、`--failure-limit`、`--restart-after-stall` 等应按需要重新明确提供，不要误以为所有参数都自动继承。轮号与已累计的失败/进展信息不会因此清零。
- 有限预算耗尽后，即使执行 `--resume` 也没有额外额度；若已授权追加，应传更大的累计总上限，不是“再增加多少”。例如已用 3600 秒，`--max-runtime 2h` 代表总计 7200 秒。原需求禁止延长时不得追加。

## CLI 与 Web 默认值区别

| 配置 | CLI 新任务 | Web 新建任务 |
| --- | --- | --- |
| worker | `paseo` | 表单优先选择可用 Codex，否则首个可用执行器；API 省略时 Codex |
| reviewer | 必填或明确 no-review | 表单优先另一可用执行器；API 省略时 Claude |
| Paseo worker 后端 | `paseo` | `paseo`，未枚举到时标明未确认可用 |
| 审核隔离 | provider | strict |
| 总时长 | 不限制 | 120 分钟 |
| 累计模型调用 | 不限制 | 100 |
| 单轮时限 | 不限制 | 1800 秒 |
| 输出空闲时限 | 900 秒 | 900 秒 |
| 每条验收时限 | 600 秒 | 600 秒 |
| 按轮次审核间隔 | 5 | 5 |
| 日志保留 | 100 轮 | 50 轮 |
| 运行/Git 历史/模型会话目录预算 | 不限制 | 64 MiB / 512 MiB / 4 GiB |
| 质量策略 | 不传则不启用额外策略 | 表单预填默认质量策略；API 未传则不启用 |
| guardian | 显式 `--guard` | Web 启动时启用 |

## 查看、控制与维护命令

以下命令同样可用 `.venv/bin/python -m auto_moniter` 替换 `auto-moniter`。`RUN_DIR` 是任务的实际运行目录，`PROJECT` 是目标项目。

| 命令 | 参数与方法 |
| --- | --- |
| `auto-moniter --version` | 显示版本；顶层 `-h/--help` 显示全部子命令 |
| `auto-moniter providers` | 仅检查各 CLI 是否在 PATH，不证明登录和模型调用成功 |
| `auto-moniter doctor -C PROJECT` | 离线检查 Linux、Python、目录及执行器；`--cwd` 是 `-C` 长写法，不能替代真实认证或 bwrap 探测 |
| `auto-moniter selftest` | 使用离线子进程跑需求完善、实现、审核退回、修复、检查和报告，不调用付费模型 |
| `auto-moniter status RUN_DIR --stale-after 60s` | 状态、模块、资源及心跳诊断；stale-after 必须大于 0，心跳过期不代表允许删除或已交付 |
| `auto-moniter watch RUN_DIR --interval 2s` | 持续观察到结束；interval 必须大于 0 |
| `auto-moniter watch RUN_DIR --once` | 只读取一次；成功读取不表示验收通过，应看 delivery_confirmed |
| `auto-moniter pause RUN_DIR` | 当前轮次返回后的边界暂停，不是立即杀掉 worker；暂停时间仍计运行预算 |
| `auto-moniter continue RUN_DIR` | 清除暂停/停止控制请求；不能复活已经退出的 monitor，已退出需 run --resume |
| `auto-moniter stop RUN_DIR` | 停止当前任务并保存检查点，不自动延长预算或重启开发 |
| `auto-moniter runs -C PROJECT --runs-dir ROOT` | 列出运行目录及空间；runs-dir 可省略，指父目录 |
| `auto-moniter cleanup -C PROJECT --runs-dir ROOT --older-than 7d --keep-runs 5` | 预览清理旧结束任务；`--apply` 才执行，`--include-failed` 才纳入 failed/stalled/max_rounds；活动和可恢复现场保留 |

所有子命令支持 `-h/--help`。`status/watch/pause/stop/continue` 的运行目录是位置参数，不写 `--run-dir`。

Git 审计维护：

```bash
auto-moniter history list -C PROJECT
auto-moniter history migrate -C PROJECT                    # 先预览
auto-moniter history migrate -C PROJECT --apply
auto-moniter history cleanup -C PROJECT --older-than 7d --keep-runs 5
auto-moniter history restore RUN_DIR -C PROJECT --apply
```

`history` 省略 operation 时为 list；支持 `--runs-dir ROOT` 重复指定扫描根、`--include-failed`、`--keep-archives 10`、`--archive-older-than 90d`。cleanup 归档后再删除对应引用，恢复只恢复审计引用，不切换业务分支；restore 必须带 RUN_DIR 和 `--apply`。保留年龄和数量共同约束，过期归档可能不可恢复。

任务 run 的退出码：0 表示完成，1 表示未完成、停止、预算/轮次/失败上限或运行异常，2 表示参数/预检/恢复配置无效。watch 默认跟随到最终状态；watch --once、status、doctor 等诊断成功不能当作任务交付通过。
