# auto-moniter

一个人提需求。`auto-moniter` 盯着 coding agent，直到独立审查认为需求已经落地。

工人每轮结束都要给出结论。返回 `continue` 且没有外部阻塞时，监督器把未完成项发回同一个会话，直接继续实现。工人报告完成候选或外部阻塞时，才启动独立审查，检查工作区中的行为、接线、测试和边界。审查发现缺口后，监督器把缺口发回工人的同一个会话，让工人继续做。

审查员默认每轮新开会话，并按后端选择审查模式：直接调用 Codex 使用只读沙箱，Claude、Grok 使用 plan。Paseo 的模式能力见下文。工人自己说完成不算数。审查员说完成时，证据必须指向这次运行期间改过、并且出现在当前 diff 里的工作区文件。引用原来就存在的 `README.md`，或只写 “looks good”，都不会通过。`--no-review` 也走这道检查。

默认持续监控，直到工人明确确认任务完成可交付，并且独立审查通过。询问、等待确认、阻塞、重复缺口、结论格式错误或进程失败，都不会让监控自动结束。监督器把上一轮的问题和输出发回工人的同一会话，要求其在原任务范围内自行作出合理选择并继续。会话失效或进程失败时重新启动，并从已有工作区继续。

缺少真实密钥或外部服务时，监督器要求 agent 完成其他可做的工作、调查替代方案并重新检查依赖，保持监控；它不会伪造密钥、检查结果或完成结论。只有你显式设置的轮次/失败/停滞上限或 SIGINT / SIGTERM 会提前停止运行。

支持的工人和审查员：

| 名称 | 实际命令 | 续会话 | 审查模式 |
| --- | --- | --- | --- |
| `paseo` | `paseo run --no-wait` / `paseo send --no-wait --prompt-file`，随后检查 agent 状态 | 同一个 Paseo agent，id 来自 `agentId` | 按后端选择：Claude 为 `plan`，Codex 为 `auto`，其他后端保留默认模式 |
| `claude` | `claude -p`，提示走 stdin | `--resume <id>` | `--permission-mode plan` |
| `codex` | `codex exec -C <cwd> -`，提示走 stdin | `codex exec resume <id>`，工作目录由进程 cwd 传入 | `--sandbox read-only` |
| `grok` | `grok --prompt-file` | `--resume <id>` | `--permission-mode plan` |
| `antigravity` | `agy --print`，提示走 stdin | `--conversation <id>` | `--mode plan` |
| `cursor` | `cursor-agent -p`，提示走 stdin | `--resume <id>` | `--mode plan` |
| `gemini` | `gemini --prompt` | `--resume <id> --prompt` | `--approval-mode plan` |
| `command` | 自定义 shell 模板 | `{session_id}` | 模板自己保证只读 |

本机没有 `workbuddy` 命令。要用它，写成 `--provider command` 模板，或者先把它注册进 Paseo。

## 环境准备与安装

需要 Python 3.11 或更高版本，项目没有第三方运行时依赖。先安装并完成所选 agent CLI 的登录或 API 配置，确保其命令在 `PATH` 中；工人和审查员都需要能独立运行。使用 Paseo 时，还需要先让 Paseo daemon 和所选后端可用。

在本项目根目录创建虚拟环境并安装（项目在其他位置时替换首行路径）：

```bash
cd /home/skn/code/auto_moniter
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
auto-moniter --help
auto-moniter providers
auto-moniter run --help
```

`providers` 显示各 CLI 是否能在 `PATH` 中找到，`available` 不代表已经登录、后端可用或请求能成功。只需准备实际使用的 CLI。新终端中先重新激活本项目的虚拟环境，再使用 `auto-moniter` 命令。

也可以不安装，直接在本项目根目录执行：

```bash
cd /home/skn/code/auto_moniter
python3 -m auto_moniter providers
python3 -m auto_moniter run \
  --cwd /path/to/project \
  --provider claude \
  --reviewer codex \
  "给现有 API 增加健康检查，补测试，并确认失败路径有明确错误。"
```

后文的 `auto-moniter ...` 均可替换为 `python3 -m auto_moniter ...`。未安装时需要从本项目根目录执行；`--cwd` 指定 agent 要修改的目标项目，不负责查找 Python 包。`auto-moniter` 是命令名，`auto_moniter` 是 Python 模块名。

## 执行任务

安装后可在任意目录执行。将 `/path/to/project` 替换为目标项目的实际路径；省略 `--cwd` 时使用执行命令时所在的目录。

直接调用 agent CLI，例如 Claude 负责实现、Codex 负责审查：

```bash
auto-moniter run \
  --cwd /path/to/project \
  --provider claude \
  --reviewer codex \
  "给现有 API 增加健康检查，补测试，并确认失败路径有明确错误。"
```

通过 Paseo 启动 Claude 工人，使用 Codex 审查：

```bash
auto-moniter run \
  --cwd /path/to/project \
  --provider paseo \
  --paseo-backend claude \
  --paseo-mode bypass \
  --reviewer codex \
  "给现有 API 增加健康检查，补测试，并确认失败路径有明确错误。"
```

通过 Paseo 启动 Codex 工人：

```bash
auto-moniter run \
  --cwd ~/021-视频专网/falcon/ \
  --provider paseo \
  --paseo-backend codex \
  --mode full-access \
  --reviewer codex \
  "结合现有项目能力检查还有哪些需要完善和不满的地方，进行补充和优化"
```

这里可以省略 `--mode`，Codex 工人默认使用 `full-access`。原有的 `--paseo-mode bypass` 写法也兼容，会自动转换为 Codex 接受的 `full-access`。

直接启动 Grok 工人，并指定 Claude 审查：

```bash
auto-moniter run \
  --cwd /path/to/project \
  --provider grok \
  --mode bypass \
  --reviewer claude \
  --reviewer-mode plan \
  "检查现有功能的缺口，补充实现并运行相关测试。"
```

Grok 的 `bypass` 会转换成原生 `bypassPermissions`。直接调用 Claude 时也可以使用同样的 `--mode bypass` 写法。

必须指定 `--reviewer`。要让监督器相信工人自己的结论，显式加 `--no-review`。

### 模式参数与兼容范围

`--mode` 设置工人模式，`--reviewer-mode` 单独设置审查员模式。使用 Paseo 时也可以继续使用 `--paseo-mode`、`--reviewer-paseo-mode`；同一角色只需指定一种参数。模式按实际后端转换，`--paseo-backend codex/模型名` 同样按 Codex 处理。

| 调用方式 | 工人默认模式 | 审查员默认模式 | 可指定的模式 |
| --- | --- | --- | --- |
| `--provider codex` | `full-access` | `read-only` | `full-access`、`auto`、`read-only` |
| `--provider claude` | `bypassPermissions` | `plan` | `default`、`acceptEdits`、`auto`、`dontAsk`、`bypassPermissions`、`plan` |
| `--provider grok` | `bypassPermissions` | `plan` | `default`、`acceptEdits`、`auto`、`dontAsk`、`bypassPermissions`、`plan` |
| Paseo + Codex | `full-access` | `auto` | `auto`、`auto-review`、`full-access` |
| Paseo + Claude | `bypass` | `plan` | `plan`、`default`、`acceptEdits`、`auto`、`bypass` |
| Paseo + Grok ACP | 后端默认 | 后端默认 | 省略模式，或使用 `default` |
| 直接调用 Gemini | `yolo` | `plan` | `default`、`auto_edit`、`yolo`、`plan` |
| 直接调用 Cursor | `force` | `plan` | `default`、`force`、`plan` |
| 直接调用 Antigravity | `bypass` | `plan` | `default`、`bypass`、`plan` |

常用别名：

- `bypass`、`bypassPermissions`、`bypass-permissions`、`full-access`、`danger-full-access`、`yolo` 表示跳过批准，转换为对应后端的模式。
- `plan`、`read-only`、`readonly` 转换为直接调用 Codex 的 `read-only`，或其他支持后端的 `plan`。
- `accept-edits`、`acceptEdits` 转换为 Claude/Grok 的 `acceptEdits`、Gemini 的 `auto_edit`。
- Codex 的 `default`、`workspace-write` 转换为 `auto`。`auto` 等原生模式保留各后端自己的含义。

Paseo Codex 没有 `plan` 或只读模式，明确指定这类模式会在启动前报错；需要 Codex 只读审查时使用 `--reviewer codex`。Paseo Codex 的默认审查模式 `auto` 允许工作区写入，审查依靠提示词约束及监督器的文件备份、回滚和拒收检查，不提供只读沙箱隔离。

已核验的 Grok Build 1.0.70 ACP 接口只暴露模型和推理选项，没有权限模式切换。`--provider paseo --paseo-backend grok` 应省略模式；指定 `bypass`、`plan` 等会提前报错并提示改用 `--provider grok --mode ...`。直接调用 Grok 可以控制权限模式，plan/default 等模式不会附加 `--always-approve`。

其他 Paseo 自定义后端的模式 ID 原样传递，省略时由后端选择默认值。具体能力取决于该后端及 daemon 版本，可参考 [Paseo CLI 模式说明](https://paseo.sh/docs/cli.md#agent-modes)。`--provider command` 的模式应写在 `--exec` 模板中，不使用 `--mode`。

已知后端的非法模式会在创建运行目录和启动 agent 前退出。运行期间的 CLI 错误会保留 JSON 中的错误码和消息，并在失败日志中打印，例如 `AGENT_CREATE_FAILED: Invalid mode ...`。每轮日志只保存结论、错误和失败检查摘要。

需求也可以放在文件里，或从标准输入读入。`REQUIREMENT.md` 的相对路径以执行命令时所在的目录为准：

```bash
auto-moniter run --cwd /path/to/project --provider claude --reviewer codex -f REQUIREMENT.md

cat REQUIREMENT.md | auto-moniter run --cwd /path/to/project --provider claude --reviewer codex
```

其他常用参数：

| 参数 | 用法 |
| --- | --- |
| `--model MODEL` | 指定工人模型；省略时使用对应 CLI 的默认模型 |
| `--reviewer-model MODEL` | 指定审查员模型 |
| `--mode MODE` | 工人模式，按 provider/backend 转换常用别名；省略时使用表中的默认模式 |
| `--reviewer-mode MODE` | 单独指定审查员模式；省略时使用表中的默认审查模式 |
| `--paseo-mode MODE` | Paseo 工人模式，兼容原有参数写法 |
| `--reviewer-paseo-mode MODE` | Paseo 审查员模式，兼容原有参数写法 |
| `--accept "验收要求"` | 在原始需求之外追加验收条件，例如要求指定测试命令通过 |
| `--run-dir /path/to/run` | 指定本次运行记录目录；相对路径以执行命令时所在的目录为准 |
| `--no-review` | 省略独立审查，仍检查工人的完成结论和文件证据 |

`--accept` 的内容作为提示交给 agent 验证，监督器不会直接把它作为 shell 命令执行。更多参数可运行 `auto-moniter run --help` 查看。

默认没有轮次、失败次数或停滞上限。需要限制总轮数时加 `--max-rounds 20`。`--turn-timeout 45m` 只会结束超时的那一轮并继续重试。

命令还在运行却停在“是否继续”“等待确认”等提示时，监督器会在提示静默 2 秒后结束等待，并发起继续工作的下一轮。没有任何 CLI 输出达到 `--idle-timeout`（默认 `15m`）也会重试。部分 CLI 只在一轮结束时输出 JSON，长时间运行的任务可以增大该值，或设置 `--idle-timeout 0` 关闭无输出检查。

未完成或失败的轮次默认等待 `--retry-delay 5s` 后继续；重复失败/缺口会逐步退避，最多等待 60 秒，等待期间仍可用信号停止。设置 `--retry-delay 0` 可立即续接。

## 查看进度与恢复运行

启动时会打印 `run directory: ...`。默认记录目录位于目标项目的 `.auto-moniter/runs/` 下，每次运行创建一个以时间戳命名的子目录。保持运行终端开启，在另一个已激活虚拟环境的终端查看进度；将 `RUN_ID` 替换为实际子目录名：

```bash
auto-moniter watch /path/to/project/.auto-moniter/runs/RUN_ID
auto-moniter watch /path/to/project/.auto-moniter/runs/RUN_ID --once
```

`watch` 跟随 `state.json` 和 `heartbeat`，打印状态、轮次和最新结论，直到出现 `report.json`；`--once` 只打印一次进度摘要。旧版状态中的完整文件基线也不会输出。每轮的结论、错误和失败检查摘要保存在 `NNN-worker.log` 和 `NNN-reviewer.log` 中。

在运行终端按 `Ctrl+C` 可停止监督器。进程中断后，用原来的目标项目和运行目录接着跑，无需重复输入需求：

```bash
auto-moniter run \
  --resume \
  --run-dir /path/to/project/.auto-moniter/runs/RUN_ID \
  --cwd /path/to/project \
  --provider claude \
  --reviewer codex
```

恢复时需要重新传入原来的工人、审查员配置，包括使用过的模型、模式、Paseo 后端和自定义命令模板；超时和轮次限制等参数也需按需要重新指定。Paseo 续接已有 agent 时沿用该 agent 已有的模式；需要改变模式时应先在 Paseo 中调整，或使用新的运行目录创建新 agent。

`--resume` 支持运行中断（`running`）、信号停止（`stopped`）和旧版因阻塞结束（`blocked`）的运行。它会保留原始文件验收基线、验收要求、会话、失败计数和历史记录，并校验 `--cwd` 与保存的工作区一致。旧版状态没有文件基线时，以恢复时的文件为基线。其他已结束、状态损坏或状态缺失的运行会被拒绝，原有报告保持完整。已有运行目录必须显式加 `--resume`，或改用新目录。

每次运行会写下：

- `requirement.md`：原始需求
- `NNN-worker-prompt.md` / `NNN-reviewer-prompt.md`：发出去的督促
- `NNN-worker.log` / `NNN-reviewer.log`：角色、会话、退出码、中断原因、耗时、结论、错误及失败检查摘要
- `baseline.json.gz`：压缩保存的初始文件基线，仅写入一次，供恢复及验收使用
- `state.json`：会话 id、轮次、缺口、仓库指纹和基线文件引用
- `heartbeat`：当前轮次和子进程 pid
- `report.json`：结束原因

成功命令的源码、目录列表、进程列表等工具输出不写入日志；启动命令只记录可执行文件和操作，不重复打印提示词或内嵌脚本。结论最多保存 16 KB，错误和失败检查输出按条目截断。提示词仍单独保存在 `*-prompt.md`，方便核对需求及验收约束。

快照、仓库指纹和审查备份跳过 `target`、`node_modules`、`.venv`、`build`、`dist` 等生成目录，并遵循 Git 对未跟踪文件的忽略规则。已跟踪的源文件仍受保护。审查只备份这些有效工作区文件，正常结束后删除临时备份。恢复失败时保留备份用于找回工人结果，并生成带失败原因的报告。可处理的异常和 SIGINT/SIGTERM 会写入结束状态及报告，日志会记录中断原因。

恢复旧版运行时会读取原有的内嵌文件基线，迁移到 `baseline.json.gz`，保留原始验收基准；之后的 `state.json` 不再重复存放完整基线。

退出码：`0` 表示任务完成且验收通过，`1` 表示达到显式设置的停滞/失败/轮次上限、目录锁被占用、运行异常或被信号打断，`2` 表示参数或恢复状态错误。

## 循环怎么停

- 工人明确给出 `done`，并且审查员也给出 `done`，均有证据且没有缺口、下一步或阻塞项。结论仍在询问是否继续、缺少必填字段或格式错误时继续监控。`--no-review` 只省略独立审查，仍须工人的明确完成结论和文件证据。
- 双方报告 `blocked` 也会继续。阻塞原因被写入状态和下一轮提示，要求继续可执行的工作并重新检查依赖。
- 显式设置 `--stall-limit N` 后，连续 N 轮缺口集合完全一样，而且工作区内容指纹没变。默认 0，不因此停止。
- 显式设置 `--failure-limit N` 后，工人或审查员连续 N 次进程失败或超时。无法解析的结论不算进程失败，下一轮会要求补结论。默认 0，失败后新开会话并继续。
- `--max-rounds` 或进程收到 SIGINT / SIGTERM。

## 自定义 agent

```bash
auto-moniter run \
  --provider command \
  --exec 'workbuddy --print --cwd {cwd} --prompt-file {prompt_file}' \
  --resume-exec 'workbuddy --resume {session_id} --print --prompt-file {prompt_file}' \
  --reviewer command \
  --reviewer-exec 'workbuddy --print --mode plan --cwd {cwd} --prompt-file {prompt_file}' \
  "实现登录失败三次后的锁定。"
```

模板占位符：`{prompt}`、`{prompt_file}`、`{cwd}`、`{schema}`、`{session_id}`、`{model}`。值已经按 shell 转义。提示正文里的花括号不会被当成占位符。

审查员如果也是自定义命令，用 `--reviewer-exec` 和 `--reviewer-resume-exec`。

## 安全

工人默认使用各自 CLI 的非交互、自动批准模式。Paseo 的 Claude 工人默认使用 `bypass`，Codex 工人默认使用 `full-access`，其他后端保留其默认模式。`--mode`、`--reviewer-mode` 会覆盖对应角色的默认值，选择交互批准模式可能触发等待输入后的重试。审查模式及 Paseo 的隔离限制见上表。只在你信任的仓库里运行。监督器不会替 agent 做 git 提交或推送。

审查开始前会把工作区文件备份到运行目录下的临时目录。审查员修改、删除已有文件或更改符号链接，都会使本轮验收失败；监督器会恢复原始内容、文件权限、符号链接和已有目录，移除审查员新建的文件，再继续工作。审查结束后清理备份。忽略的依赖、构建产物、Git 元数据和运行目录不在还原范围内。大型项目需要为备份保留相应磁盘空间。验收证据必须指向项目内的真实变更文件，运行日志及指向项目外的符号链接不能作为证据。

Paseo 的 `run` 没有提示文件入口。短提示仍放在参数里。UTF-8 编码超过 10 万字节时，正文写入 `.auto-moniter/inbox-prompt.md`，参数里只要求 agent 读取该文件。Paseo 不使用 `--output-schema`。创建/续接先用 `--no-wait` 取得 `agentId` 并立即记录，再通过 `inspect` 和 `logs` 观察状态。等待输入、超时或进程异常时先停止 daemon 中的 agent，再续接同一个 agent；本地 CLI 退出不会被当成 daemon 工作已经结束。结论取 `paseo logs` 最后一段 `---` 分隔的正文。如果输出是 JSON timeline，则取最后一条 `assistant_message`。每轮审查 agent 结束后会归档。

同一个 `run-dir` 有文件锁，第二个监督器会直接退出。收到 SIGINT 或 SIGTERM 时，如果工人是 Paseo agent，监督器会调用 `paseo agent stop`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时工作区与模拟命令，不调用付费模型。回归覆盖询问后继续、阻塞后继续、完成条件、审查还原、Paseo 等待输入续接、输出解析、验收证据、恢复运行、目录锁、超时和信号停止。
