# auto-moniter

一个人提需求。`auto-moniter` 盯着 coding agent，直到独立审查认为需求已经落地。

当前交付版本：**0.4.1，本地 Linux CLI**。需要 Python 3.11+；本次完整验证环境为 Linux / Python 3.12。版本变更见 [CHANGELOG.md](CHANGELOG.md)，部署和故障处理见 [OPERATIONS.md](OPERATIONS.md)。

新增独立的 [Web 控制台](console/README.md)，支持创建开发任务、启动/暂停/继续/停止、查看模块验收与执行日志，以及实时 CPU、内存和磁盘监控。在项目根目录执行：

```bash
./start.sh
# 浏览器打开 http://127.0.0.1:8765
```

启动脚本优先使用项目 `.venv`，否则使用 `python3`；支持透传控制台参数，例如 `./start.sh --port 9000`。也可直接执行 `python3 -m console`。

默认会发现本机的标准运行记录和当前项目 `.auto-moniter/` 中的记录。创建任务后可立即启动，也可保存为待启动任务；实际执行复用现有 CLI 和独立审核流程。详细配置见 [console/README.md](console/README.md)。

任务列表、详情和实时进展支持删除已停止/结束或待启动的任务，操作前有确认。删除只移除本控制台登记并持久化隐藏记录，项目、Git 历史和运行日志保留；刷新不会重新发现该任务。运行/暂停及进程回收未确认的任务须先停止，删除不代替存储清理。

安装后先运行两项离线检查，不调用模型：

```bash
auto-moniter doctor --cwd /path/to/project
auto-moniter selftest
```

`doctor` 检查平台、目录和 CLI 是否存在，不代表已登录或 API 可用；`selftest` 在临时目录验证子进程、结论解析、模块验收、检查命令和报告落盘。初次使用建议明确设置预算，并提供实际项目的验收命令：

```bash
auto-moniter run --cwd /path/to/project \
  --provider claude --reviewer codex \
  --guard --max-runtime 2h --max-agent-calls 100 \
  --check 'git diff --check' \
  '实现原始需求中的全部模块，并完成相关测试和集成验证。'
```

`git diff --check` 只检查 diff 格式；应按项目追加真正的测试命令。需要更强的审核文件隔离时加 `--review-isolation strict`，启动时会实测系统是否支持。

工人每轮结束都要给出结论。返回 `continue` 且没有外部阻塞时，监督器把未完成项发回同一个会话继续实现；默认连续 5 轮没有完成有效审核，也会触发一次进度审计，避免 worker 长期偏离需求。完成候选和外部阻塞会立即触发独立审查，检查行为、接线、测试和边界。审查发现缺口后，监督器把缺口发回工人继续做。

审查员默认每轮新开会话，并按后端选择审查模式：直接调用 Codex 使用只读沙箱，Claude、Grok 使用 plan。Paseo 审查仅支持 Claude 的 plan 模式。worker 负责实现与会写入文件的构建、测试；reviewer 只检查代码及已有证据，把修改和补测要求交回 worker。审查不创建工作区备份。工人自己说完成不算数。审查员说完成时，证据必须指向这次运行期间改过、并且出现在当前 diff 里的工作区文件。引用原来就存在的 `README.md`，或只写 “looks good”，都不会通过。`--no-review` 也走这道检查。

默认持续监控，直到工人明确确认任务完成可交付，并且独立审查通过。每次 `run` 处理一整份需求；需求全部验收通过后写报告并退出，当前没有常驻领取新需求的队列入口。结论必须声明 `scope`：`module` 表示当前模块，`requirement` 表示整份原始需求。单个模块通过审核后，monitor 保持运行，关闭该模块的 worker/reviewer 会话，再为原需求中的下一个未完成模块启动新会话；原始需求、已验收模块摘要和文件验收基线继续保留。只有双方都给出 `status=done`、`scope=requirement` 且证据通过检查后，整次运行才结束。未声明 scope、modules 或 module_ids 的新完成结论会被退回补充。询问、等待确认、阻塞、重复缺口、结论格式错误或进程失败，都不会让监控自动结束。监督器把上一轮的问题和输出发回工人的同一会话，要求其在原任务范围内自行作出合理选择并继续。会话失效或进程失败时重新启动，并从已有工作区继续。

缺少真实密钥或外部服务时，监督器要求 agent 完成其他可做的工作、调查替代方案并重新检查依赖；不会伪造密钥、检查结果或完成结论。显式停止、预算或轮次/失败/停滞上限、认证失败、无法安全恢复、审查期间改写源码等情况会提前结束并保存非 `done` 结果，不能视为交付。

## worker、reviewer 与 watcher 的协作

这里的 watcher 是 `run` 启动的 `Supervisor` 监督循环，负责调度和验收。`watch` 子命令只读显示进度，不调度 AI；可选的 `--guard` 看守负责 monitor 异常退出后的恢复，也不代替业务审核。

| 角色 | 职责 |
| --- | --- |
| worker | 按原始需求实现能力、补充测试，提交模块编号、完成范围和证据；尚未满足要求时继续实现 |
| reviewer | 独立只读审计代码和已有证据，返回缺口、修复建议或明确的可交付结论；不修改实现 |
| watcher / Supervisor | 启动并等待 worker，触发 reviewer，将审计缺口交回 worker；执行配置的验收命令，维护模块进度并决定继续或结束 |

协作按顺序执行：`watcher → worker → watcher → reviewer → watcher → worker …`。worker 尚未提交完成候选且没有外部阻塞时，watcher 继续推动实现，并按 `--review-every` 定期审计；进入审核后，缺口和后续动作随下一轮提示词反馈给 worker。最近一份反馈和最后完成的业务审计分别保存，独立于逐轮历史窗口；worker 失败或 reviewer 执行失败、格式错误、等待确认，以及切换会话或恢复运行，都不会因此清掉上一份有效业务审计。新的 reviewer 会收到这些结论及 monitor 验收反馈，独立复核旧问题是否解决；历史结论不能代替当前代码审查。

一项模块能力必须由 worker 与 reviewer 都声明 `done`、共同覆盖该模块编号、具备有效审核证据，并通过配置的模块检查，才会记为已验收。模块通过后关闭双方旧会话，watcher 继续原始需求中的下一个未完成模块。只有双方都以 `scope=requirement` 确认整份需求可交付，所有模块、配置的全局检查和代码版本核对均通过，才写入最终 `status=done` 并退出。后续源码变化需要核对影响范围：用户在模块 `files` 中声明的文件范围及其依赖决定哪些模块需要重新审核；未声明范围或修改共享/未知文件时保守重审。

需要这种双方确认的闭环时，应配置 `--reviewer` 并保留默认审查流程；`--no-review` 会显式跳过独立审核，不满足双方共同确认的要求。

`--review-every 5` 表示连续 5 轮 worker 工作后进行进度审计，默认值为 5；`0` 关闭周期审核，仍会审核完成候选和外部阻塞。计数会跨历史清理和恢复保留，恢复时省略参数沿用原值，显式指定可调整。审核执行失败、结论格式无效，或仅返回 continue 而没有任何具体缺口、动作和阻塞时，不重置计数，也不覆盖有效业务审计。review-first 中，reviewer 的执行/格式错误先在同一候选上重试一次，仍失败则保存并停止；源码与补充要求未变时恢复只续审，不重复调用 worker。该设置按轮次调度，不是按时间中断一个仍在运行的 worker；周期审计计入 `--max-agent-calls` 预算。`status`、`watch` 和最终报告包含 `review_schedule`。

周期审核在 worker 正常返回可审核结论后调度。worker 超时、执行失败或被停止的轮次仍累计间隔计数，但会先处理失败或停止，不直接进入 reviewer；即使计数达到阈值，也需等待后续 worker 正常返回。Web 新建任务的“执行配置与验收标准”可设置审核频率，详情及实时进展展示实际频率、累计轮次与等待原因。其他 CLI 配置在 Web 中的覆盖范围见 [配置核对清单](console/CONFIGURATION_AUDIT.md)。

正常审核模式会分别核验 worker 和 reviewer 的完成证据，双方都应引用运行期间变更、仍在当前 diff 中的项目文件；reviewer 的有效证据不能代替 worker 的无效引用。恢复运行时，已保存的文字验收要求 `--accept` 与验收命令、模块定义一样不能替换，省略时沿用原值。

纯删除任务可以引用已删除文件的原路径，前提是该路径存在于本次运行的初始文件哈希基线中，当前确已删除，并通过项目范围及 Git diff 核对。这也适用于审计问题的关闭证据，无须为了验收重新创建已删除文件。未知路径、目录替换、悬空链接、运行记录和指向项目外的路径不能冒充删除证据；没有初始基线时不凭空推断文件曾经存在。

检测到 Git 项目时，Git 状态或项目路径查询失败、超时、命令缺失会拒绝本次文件证据，不会借用非 Git 项目的规则放行。修复 Git 环境后可重新审核。重命名的原路径和新路径都计入变更集合，因此可分别用于核对移除和新增；普通非 Git 项目继续使用初始文件基线核验。

模块定义只从成功执行、没有中断且结论格式有效的轮次登记。失败进程输出、超时或无效结论中的临时计划不会固化为不可修改的清单；已经有效登记的模块定义继续保持不可弱化。

恢复任务时，省略 `--turn-timeout`、`--idle-timeout`、`--retry-delay`、`--check-timeout` 和 `--max-output-bytes` 会沿用检查点中的配置，避免长构建或大输出任务因回到默认参数而被中断。显式传入会覆盖原值，包括显式恢复默认值；例如 `--idle-timeout 15m`，或用 `--idle-timeout 0` 关闭无输出超时。新运行仍采用原默认值：单轮不限时、无输出 15 分钟、初始重试 5 秒、每条验收 10 分钟、捕获输出 8 MiB。旧版检查点没有这些配置时采用默认值，损坏配置拒绝恢复。

### 审计问题逐项关闭

watcher 将有效 reviewer 结论中的 `gaps` 和外部 `blockers` 登记为 `AUD-1`、`AUD-2` 等稳定编号；没有这两类内容时登记具体 `next_actions`。重复的同范围、同文本问题沿用编号。后续 reviewer 换一组意见、只说 done，或者执行失败，都不会删除已登记的未关闭问题。

反馈旧问题时，reviewer 应在文本开头写原编号，例如 `AUD-1: 拒绝用例仍失败` 或 `AUD-1：还缺少边界验证`。watcher 会更新原条目的观察轮次，保留最初描述和模块范围，不会因为改写措辞产生新编号。未知编号会被拒绝；不带编号的不同措辞仍按新发现处理，watcher 不做语义猜测或自动合并问题。

worker 读取清单并修复，不能关闭编号。reviewer 独立检查后，在结论的 `issue_resolutions` 数组逐项给出说明和证据，例如：

```json
"issue_resolutions": [
  {
    "id": "AUD-1",
    "summary": "已检查接口分支和拒绝用例证据，原缺口已解决",
    "evidence": ["src/handler.py:42", "tests/test_handler.py:18"]
  }
]
```

关闭依据可以说明修复已完成，也可以说明原发现不成立，但都必须引用本次运行中变更、处于当前 diff 的项目文件。支持 `文件.md:694：说明`、`文件.md：694：说明` 等中文行号引用。未知编号、重复编号、缺少说明或无效证据会被拒绝；同一结论不能一边报告问题仍存在，一边关闭它。旧结论未提供该数组时视为没有关闭任何问题。reviewer 仍是只读角色，watcher 负责落盘。

模块范围审核发现的问题关联 worker 本轮声明的模块；没有明确模块范围的问题按需求级处理。模块验收必须关闭其关联问题及全部需求级问题；最终交付必须没有任何开放问题。双方共同认可、模块覆盖、独立验收命令和源码版本检查继续生效，关闭问题本身不代表任务已经交付。

`state.json`、`report.json`、`status` 和 `watch` 包含 `audit_issues`，保留问题来源轮次、状态、关闭说明、证据及关闭时的代码指纹。源码变化会重新打开仍保留的关闭记录，要求再次审核。清单独立于逐轮历史窗口保存；恢复旧记录时可从最后一份业务审计导入缺口，无法找回旧版已经丢失的历史问题。

清单总计最多 128 条记录，其中已关闭记录最多保留最近 20 条，开放问题优先保留；单条问题最多 2000 UTF-8 字节，清单 JSON 最多 512 KiB。达到容量时停止为 `budget_exceeded` 并保留当前审计反馈，不会静默删除开放问题。它是有界工作清单，已淘汰的关闭记录仅累计计数，完整详细历史仍受 `--keep-rounds` 控制。证据是否真正解释业务问题仍需 reviewer 判断，watcher 核验结构、编号、文件及代码版本。

同一份有效审核中的关闭和新增按整体结果核对容量，允许清单已满时关闭旧问题、腾出位置后登记新问题；容量不足时整批不提交。关闭请求无效时，不会部分关闭旧问题，其他有效的新发现仍会尽量登记。真正关闭一个开放问题会计为进展，重置停滞计数，避免无源码改动的证据复核被当成空转；重复提交已关闭编号不算新进展。未消费的进展标记跨停止和恢复保存。

支持的工人和审查员：

| 名称 | 实际命令 | 续会话 | 审查模式 |
| --- | --- | --- | --- |
| `paseo` | `paseo run --no-wait` / `paseo send --no-wait --prompt-file`，随后检查 agent 状态 | 同一个 Paseo agent，id 来自 `agentId` | 仅支持 Claude 的 `plan`；其他 Paseo 后端请改用原生只读审查 |
| `claude` | `claude -p`，提示走 stdin | `--resume <id>` | `--permission-mode plan` |
| `codex` | `codex exec -C <cwd> -`，提示走 stdin | `codex exec resume <id>`，工作目录由进程 cwd 传入 | `--sandbox read-only` |
| `grok` | `grok --prompt-file` | `--resume <id>` | `--permission-mode plan` |
| `antigravity` | `agy --print`，提示走 stdin | `--conversation <id>` | `--mode plan` |
| `cursor` | `cursor-agent -p`，提示走 stdin | `--resume <id>` | `--mode plan` |
| `gemini` | `gemini --prompt` | `--resume <id> --prompt` | `--approval-mode plan` |
| `command` | 自定义 shell 模板，仅用于 worker | `{session_id}` | 不支持作为 reviewer |

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

`--mode` 设置工人模式，`--reviewer-mode` 单独设置审查员模式；审查仅接受原生 Codex 的 `read-only` 或其他支持后端的 `plan`，可写模式会被拒绝。使用 Paseo 时也可以继续使用 `--paseo-mode`、`--reviewer-paseo-mode`；同一角色只需指定一种参数。模式按实际后端转换，`--paseo-backend codex/模型名` 同样按 Codex 处理。

| 调用方式 | 工人默认模式 | 审查员默认模式 | 可指定的模式 |
| --- | --- | --- | --- |
| `--provider codex` | `full-access` | `read-only` | `full-access`、`auto`、`read-only` |
| `--provider claude` | `bypassPermissions` | `plan` | `default`、`acceptEdits`、`auto`、`dontAsk`、`bypassPermissions`、`plan` |
| `--provider grok` | `bypassPermissions` | `plan` | `default`、`acceptEdits`、`auto`、`dontAsk`、`bypassPermissions`、`plan` |
| Paseo + Codex | `full-access` | 不支持；使用原生 `--reviewer codex` | `auto`、`auto-review`、`full-access` |
| Paseo + Claude | `bypass` | `plan` | `plan`、`default`、`acceptEdits`、`auto`、`bypass` |
| Paseo + Grok ACP | 后端默认 | 不支持；使用原生 `--reviewer grok` | 省略模式，或使用 `default` |
| 直接调用 Gemini | `yolo` | `plan` | `default`、`auto_edit`、`yolo`、`plan` |
| 直接调用 Cursor | `force` | `plan` | `default`、`force`、`plan` |
| 直接调用 Antigravity | `bypass` | `plan` | `default`、`bypass`、`plan` |

常用别名：

- `bypass`、`bypassPermissions`、`bypass-permissions`、`full-access`、`danger-full-access`、`yolo` 表示跳过批准，转换为对应后端的模式。
- `plan`、`read-only`、`readonly` 转换为直接调用 Codex 的 `read-only`，或其他支持后端的 `plan`。
- `accept-edits`、`acceptEdits` 转换为 Claude/Grok 的 `acceptEdits`、Gemini 的 `auto_edit`。
- Codex 的 `default`、`workspace-write` 转换为 `auto`。`auto` 等原生模式保留各后端自己的含义。

Paseo Codex 没有 `plan` 或只读模式，明确指定这类模式会在启动前报错；需要 Codex 只读审查时使用 `--reviewer codex`。Paseo Codex 的 `auto` 允许工作区写入，因此不再接受作为 reviewer；Paseo Grok、自定义 Paseo 后端和自定义 shell 命令同样不提供可用的只读审查模式，会在 worker 启动前报错。Paseo Claude 审查必须使用 plan，并且每轮新建会话；不支持 `--no-fresh-review`，避免续接旧会话的可写权限。

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
| `--reviewer-mode MODE` | 单独指定审查员只读/plan 模式；禁止可写模式 |
| `--paseo-mode MODE` | Paseo 工人模式，兼容原有参数写法 |
| `--reviewer-paseo-mode MODE` | Paseo 审查员模式，兼容原有参数写法 |
| `--accept "验收要求"` | 在原始需求之外追加验收条件，例如要求指定测试命令通过 |
| `--run-dir /path/to/run` | 指定本次运行记录目录；相对路径以执行命令时所在的目录为准 |
| `--no-review` | 省略独立审查，仍检查工人的完成结论和文件证据 |

`--accept` 的内容作为提示交给 agent 验证，监督器不会直接把它作为 shell 命令执行。更多参数可运行 `auto-moniter run --help` 查看。

默认没有轮次、失败次数或停滞上限。需要限制总轮数时加 `--max-rounds 20`。`--turn-timeout 45m` 只会结束超时的那一轮并继续重试。

命令还在运行却停在“是否继续”“等待确认”等提示时，监督器会在提示静默 2 秒后结束等待，并发起继续工作的下一轮。没有任何 CLI 输出达到 `--idle-timeout`（默认 `15m`）也会重试。部分 CLI 只在一轮结束时输出 JSON，长时间运行的任务可以增大该值，或设置 `--idle-timeout 0` 关闭无输出检查。

未完成或失败的轮次默认等待 `--retry-delay 5s` 后继续；重复失败/缺口会逐步退避，最多等待 60 秒，等待期间仍可用信号停止。设置 `--retry-delay 0` 可立即续接。

## 需求与模块清单

worker 首轮需要把完整原始需求拆成 `modules`，每个模块包含稳定的 `id`、`title` 和 `acceptance` 验收条件。reviewer 除了检查当前模块，还应检查清单是否遗漏原始需求。后续结论用 `module_ids` 明确本轮完成、独立验收的模块编号。没有清单或编号的完成声明会被退回。

清单由 monitor 保存在 `state.json`，最终报告也包含模块状态与证据。已有编号的名称、验收条件、依赖和检查命令不能修改；省略已有条目不会删除它们，发现遗漏可以追加新编号。清单最多 200 项，定义合计最多 128 KiB；每项验收证据最多保存 4 条、每条 1 KiB，避免清单再次变成巨量记录。

也可以在启动时用 `--modules modules.json` 提供明确清单：

```json
[
  {"id": "auth", "title": "登录认证", "acceptance": ["有效凭据可以登录", "无效凭据被拒绝"]},
  {"id": "lockout", "title": "失败锁定", "acceptance": ["连续失败三次触发锁定", "锁定状态与登录接口正确联动"]}
]
```

模块只有在 worker 和 reviewer 共同覆盖对应编号、文件证据检查通过后才记为 `accepted`；`--no-review` 时由 worker 单独确认。整体验收要求每个待完成模块都被本轮双方共同覆盖，或已经在此前对同一份代码被验收，不能只声明整份需求完成却漏掉模块。模块通过后关闭旧会话，新会话继续读取同一清单。恢复运行保留已验收状态、证据及轮次，不允许替换已有定义。清单覆盖原始自然语言需求的完整性仍需要 reviewer 判断；monitor 负责强制执行已登记的清单。

## 验收版本、回归与模块依赖

模块验收记录的 `tree` 和最终报告的 `accepted_tree` 绑定工作区内容、权限和符号链接的指纹。后续 worker 改变源码时，monitor 保守地把旧版已验收模块全部标为 pending，保留旧证据并记录失效原因，要求 reviewer 重新确认；即使只改了另一个模块，也会重新检查。这种方式无需文件备份，也不会把过期的验收当成当前代码已通过。

验收命令执行前后会比较工作区指纹。命令即使返回 0，只要改动了源码，也会退回 worker 并要求重新审核；最终关闭 agent 前后还会核对一次。`target`、`build`、运行目录和 Git 忽略的生成文件沿用既有排除规则，正常编译产物不会因此使源码审核失效。

通过 `--modules modules.json` 可以为模块添加 `depends_on` 和 `checks`：

```json
[
  {"id": "auth", "title": "认证", "acceptance": ["认证成功及拒绝路径均正确"],
   "checks": ["python3 -m unittest tests.test_auth -q"]},
  {"id": "lockout", "title": "失败锁定", "acceptance": ["失败三次锁定"],
   "depends_on": ["auth"], "checks": ["python3 -m unittest tests.test_lockout -q"]}
]
```

未知依赖和循环依赖会被拒绝；依赖必须已在当前代码版本验收，或在同一轮由双方共同确认。monitor 按依赖顺序运行相关模块检查。依赖用于约束验收，worker 仍可提前编写后续模块的代码。模块命令只允许来自用户初始配置，AI 可以建议命令，但不能通过结论添加让 monitor 执行的脚本。

## 审查只读隔离

默认 `--review-isolation provider` 沿用原生 Codex 的只读沙箱及其他支持后端的 plan 权限模式。需要操作系统层面的额外隔离时，加 `--review-isolation strict`，仅支持本地原生 reviewer，要求 Linux 和 Bubblewrap (`bwrap`)。

strict 模式在 worker 启动前实际探测读取，以及写入、删除、新建是否受限。随后每次 reviewer 都在只读文件系统挂载及独立 PID 命名空间内执行，临时目录独立；CLI 自身的已存在配置目录可写，项目和运行记录仍为只读。探测失败、没有 bwrap 或使用 Paseo daemon 审查时会直接报错，不会降级为提示词约束。它限制 reviewer 的本地文件写入，不是网络服务的权限隔离。已挂载的配置目录中与项目有硬链接的文件也应避免共享。

## 审核效率与结论复用

review-first 的实现审核优先比较“上一次有效业务审核的 Git tree → 当前候选”，累计差异保留供共享行为和整体验收核对。不能因轮次变化要求重新实现未变化的能力。真实行为、依赖、验收输入或证据改变，以及失败检查，仍需要对应复核；最终交付始终执行配置的固定检查，不以历史通过代替本次验收。

reviewer 必须把每个阻塞项写清：AUD 编号、验收条款、文件/符号、触发输入、预期与实际、最小修复范围及验证命令/断言；说明是实际复现还是代码推断，一次列出当前已知的相关入口与缺口。纯措辞偏好不阻塞交付，但失实证据必须修正。worker 按缺口定向修复和验证，不因轮次变化重复两种等价的 unittest 全量发现命令。

reviewer 执行失败、结论格式错误或遗漏候选哈希，先由 reviewer 在同一候选上修复一次，仍计入原调用/时间预算；认证错误直接停止。仍未完成时保存检查点，源码和用户补充要求未变则恢复只续审；源码或需求变化才重新调用 worker。失败尝试有独立、随轮次清理的有界日志，历史失败不会被成功重试覆盖。watcher 验收使用 `sh -c` 继承服务进程的 PATH，避免登录 shell 重设 PATH 造成 worker 测试通过、watcher 找不到同一程序。

模块可由用户声明 `files`（包含实现、测试与共享依赖）及 `depends_on`。未受影响模块和关联 AUD 可保留有效结论；改动其依赖、关闭证据或未知共享文件仍重新验证。AI 不能自行缩小文件范围。没有范围时不会猜测“无影响”。运行复盘和量化证据见 [两小时运行复盘](docs/supervision-efficiency-review.md)。

## Web 临时调整角色与模型

在任务详情中，先点击“停止”并等待检查点保存，再点击“临时调整角色”。分别选择 Worker 和 Reviewer 执行器及模型，保存后点击“恢复执行”。待启动任务也可以直接调整。配置仅影响当前任务，不修改系统环境或其他任务；API Key 继续从 Web 服务启动环境继承。

例如：Worker 选择 `grok`、模型填写 `grok-4.5`；Reviewer 选择 `codex`、模型填写 `gpt-6.1-sol`。模型留空时使用对应执行器默认模型；同一执行器的 Reviewer 模型留空时沿用 Worker 模型。不同执行器不共享模型名。

恢复时重新建立双方独立会话，保留原始需求、需求细化、验收命令、审核结论、模块进度、隔离策略和累计预算。临时切换只支持原生执行器；daemon、自定义命令以及带有不兼容后端附加参数的任务须建立后续任务。角色调整历史仅保留最近 20 次，运行中禁止修改，避免并行 worker 写入。

## 运行控制、错误恢复与事件记录

```bash
auto-moniter pause /path/to/project/.auto-moniter/runs/RUN_ID
auto-moniter continue /path/to/project/.auto-moniter/runs/RUN_ID
auto-moniter stop /path/to/project/.auto-moniter/runs/RUN_ID
```

pause 在轮次边界生效，等待当前 worker/reviewer 完成本轮；不会把执行到一半的文件强行回滚。continue 清除暂停/停止请求，让仍运行的 monitor 继续。stop 会中断受监控子进程并完成 agent 收尾；看守也会读取停止请求，禁止再次重启。已退出的 monitor 不会被 continue 自动启动：先清除请求，再用 `run --resume`（按需加 `--guard`）恢复。暂停期间仍计入显式总运行时间预算。

进程错误会分类处理并记录事件：认证/登录错误结束为可恢复的 `action_required`，修好凭据后手动 resume；限流增加重试退避；网络错误按已有退避重试；会话失效或上下文超限关闭失效会话并改用新会话。代码验收失败继续交回 worker 修复。错误分类基于后端返回的错误文本，是启发式判断，无法识别的错误走一般重试策略。

`events.jsonl` 记录 agent 启动、审核拒绝、检查结果、模块失效/验收、停滞恢复、暂停及结束原因，不记录源码和完整工具输出。单文件最多约 256 KiB，满后覆盖轮转到 `events.previous.jsonl`，只保留当前及上一份，总量约 512 KiB。它是有界事件历史；每轮提示词和结论文件按 `--keep-rounds` 的保留窗口清理。

## 长任务历史保留与进程收尾

默认只保留最近 100 轮的详细记录及 monitor 生成的提示词、结论、角色日志和验收日志。`--keep-rounds N` 调整窗口，`--keep-rounds 0` 保留全部。恢复时省略该参数会沿用保存的窗口，显式指定可调整；增加窗口不能找回已经淘汰的历史。

淘汰历史仅匹配当前运行目录中固定格式的 `NNN-worker/reviewer-prompt.md`、`NNN-worker/reviewer-text.md`、角色 `.log` 和 `NNN-check-NN.log`，不会递归删除目录，不跟随符号链接，也不清理源码、任意笔记或其他运行目录。模块清单、当前缺口、验收基线、累计调用和耗时、真实总轮数及连续失败计数继续保存；`status` 和 `watch` 显示真实总轮数。报告中的 `records` 是保留窗口，`history` 明确记录已淘汰数量，不冒充完整历史。

### 日志和审计数据会不会占用大量空间

已取消全量工作区备份，不会再因 `review-backup-*` 复制源码而产生此前约 576 MB 的占用。但保留轮数不等于总容量封顶：默认 `--max-run-bytes 0` 不限制运行目录总大小，复杂需求和多次运行仍可能累计较大数据。

| 数据 | 当前保留规则 |
| --- | --- |
| 审计事件 | 当前及上一份轮转日志，总量约 512 KiB |
| 当前审计问题清单 | 最多 128 条，已关闭记录最多 20 条，JSON 最多 512 KiB；随状态覆盖写入 |
| 每轮日志、结论、提示词 | 默认保留最近 100 轮 |
| AI 结论 | 每份最多约 16 KB；角色日志还包含有截断的错误和失败检查摘要 |
| monitor 验收命令输出 | 每条日志保留约 24 KB 输出尾部；检查命令越多，日志总量越大 |
| 状态、心跳、看守状态、告警 | 覆盖写入，状态内详细轮次遵循历史窗口 |
| 文件哈希基线 | 压缩保存一次，体积取决于有效文件数量，不复制文件内容 |

提示词和状态记录没有独立的文件大小上限，不能把“结论最多 16 KB”理解为整个运行目录很小。旧运行目录不会自动清理，可用下方 `cleanup` 命令显式管理；`--keep-rounds` 只清理当前运行的逐轮文件。Claude、Codex、Paseo 自己保存的会话日志、系统 journal，以及项目构建产物和 agent 自行写入的文件，不受此保留窗口控制。

希望减少占用时，可在原启动命令中追加：

```bash
--keep-rounds 20 --max-run-bytes 104857600
```

这会保留最近 20 轮详细历史，并在当前运行目录达到约 100 MiB 时停止、尽量保存诊断。容量检查每秒采样，是软预算，写入期间可能短暂超过；它不限制所有历史运行的合计大小。需要磁盘占用绝对封顶时，应在部署文件系统配置配额，并另行管理旧运行目录、后端会话日志和系统日志。

### 查看占用与清理旧运行

```bash
# 查看指定项目各次运行的空间占用，不启动 AI
auto-moniter runs -C /path/to/project

# 默认仅预览：保留最近 5 次结束记录，只选择超过 7 天的成功记录
auto-moniter cleanup -C /path/to/project

# 确认预览后，按相同规则执行删除
auto-moniter cleanup -C /path/to/project --apply

# 自定义期限和数量；失败记录也需清理时显式增加 --include-failed
auto-moniter cleanup -C /path/to/project --older-than 30d --keep-runs 10 --apply

# 旧版项目内目录或自定义运行目录：明确指定运行目录的共同父目录
auto-moniter runs -C /path/to/project --runs-dir /path/to/project/.auto-moniter/runs
auto-moniter cleanup -C /path/to/project --runs-dir /path/to/project/.auto-moniter/runs
```

默认位置遵循 `AUTO_MONITER_STATE_DIR` / XDG 配置，按项目区分。两个命令输出 JSON，包含每次运行的状态、大小和保留原因；`total_bytes` 是扫描到的文件及链接自身的逻辑大小，`allocated_bytes` 是文件系统块占用估算，硬链接等情况会重复计数。`candidate_bytes` 表示计划清理量，`removed_bytes` 表示已删除目录在扫描时的大小，均不是磁盘实际释放量保证。只检查父目录中的真实子目录，不跟随目录链接，也不创建不存在的状态目录。

清理默认只接受状态与报告均为 `done` 的记录；`--include-failed` 扩展到 `failed`、`stalled` 和 `max_rounds`。最近 N 次已结束记录（包括失败记录）始终保留，年龄按结束报告的修改时间计算。`stopped`、`blocked`、`budget_exceeded`、`action_required`、`recovery_failed` 和未结束任务始终保留，便于恢复。运行锁或看守锁被持有、残留进程身份记录、项目不匹配、格式损坏、未知状态版本、缺少运行锁或包含挂载点时，均跳过清理；不会仅凭心跳过期判定任务可删除。

`--apply` 在持有运行锁期间重新核对状态、报告时间和目录身份，再删除整个选中的运行目录，包括其中的提示词和报告；源码目录不会因此被清理。删除失败会在 `errors` 中返回原因并以退出码 1 结束，便于运维脚本检查。清理不处理旧 `review-backup-*`、后端会话缓存或系统日志，也不会恢复已经删除的历史。

CLI 主进程退出不再等同于本轮结束：monitor 会检查本次进程组内是否仍有活动子进程，包括已关闭 stdout/stderr 的后台任务。发现遗留任务后先尝试终止，必要时强制结束，并将本轮标为 `background processes`，交回 worker 修复收尾逻辑；不会把仍有后台任务的结论当作成功交付。主动创建新 session 或迁移出原进程组的程序不在此检查范围内；Paseo daemon 的 agent 继续走独立的 stop/archive 流程。

关键状态、报告和进程身份记录使用临时文件写入、同步内容、原子替换并同步父目录；初始需求和压缩文件基线也会同步内容。失败时清理临时文件，不保留旧版本备份。该机制减少异常退出造成的半文件和未落盘状态，不能替代磁盘故障恢复。

## 资源预算与运行健康

- `--max-runtime 2h`：本次需求累计活动运行时长上限；默认 0 不限制。恢复运行继续累计，停止期间不计时。异常退出按最后保存的状态与心跳恢复计时，最后一次心跳之后的时间可能未记入。
- `--max-agent-calls 100`：worker/reviewer 总轮次调用上限，跨恢复累计；Paseo inspect/logs 查询和 monitor 验收命令不算模型调用。默认 0 不限制。
- `--max-output-bytes 8388608`：每个受监控子进程的 stdout/stderr 合计上限，默认 8 MiB。流式读取达到上限后终止该进程组，报告 `output limit`，该轮不能被当作交付成功。该参数限制捕获输出，不是 AI 工具或整个进程树的内存限额。

运行时长或调用预算耗尽后，monitor 停止工作并保存 `budget_exceeded` 状态及进度，不会声称已交付。可以用原运行目录 `--resume` 并显式增加对应上限继续；省略时沿用已保存上限，不能通过重复恢复清零用量。

```bash
auto-moniter status /path/to/project/.auto-moniter/runs/RUN_ID
auto-moniter status /path/to/project/.auto-moniter/runs/RUN_ID --stale-after 90s
```

`status` 是只读诊断，返回当前阶段、心跳年龄、运行锁、历史中断原因、资源用量以及待完成/已验收模块编号。`active` 表示运行锁被持有且心跳新鲜，`heartbeat_stale` 表示持锁但心跳过期，`monitor_exited` 表示运行锁已释放且没有看守持锁，`recovering` 表示看守仍持锁但 monitor 当前未持锁。`finished` 需要格式有效的结束报告与状态一致，且运行锁和看守锁均已释放；成功报告还须没有残留进程身份记录。`delivery_confirmed=true` 才表示这次读取确认了成功交付；原始 `status=done` 单独不足以确认。`report_warning` 提示报告损坏、状态不匹配或残留进程记录。心跳过期是待排查信号，不等同于断言 AI 已卡死。Paseo 等待权限/输入时还会记录对应阶段。

## 由 monitor 独立验收

`--accept` 是交给 AI 阅读的验收要求；`--check` 是由 monitor 自己执行的 shell 命令。只有 worker 和 reviewer 都确认整份原始需求完成，且所有命令返回 0，才允许交付。未配置命令时仍采用已有的 AI 审查与文件证据检查。

```bash
auto-moniter run --cwd /path/to/project \
  --provider claude --reviewer codex \
  --check 'python3 -m unittest discover -s tests -q' \
  --check 'git diff --check' \
  --check-timeout 10m \
  '实现原始需求中的所有模块，并完成集成验证。'
```

检查在目标工作区执行，可生成测试输出；reviewer 仍仅做只读审查。每条命令默认最多运行 10 分钟，可以用 `--check-timeout` 调整，不能关闭超时。失败或超时会把错误及日志路径交回 worker 继续修复，修复并重新审查后再次执行。日志保存在 `NNN-check-NN.log`，每条最多保留约 24 KB 的输出尾部。模块级验收只执行双方共同认可模块及其依赖的检查命令，不执行整体验收命令；模块里程碑也仅记录共同认可的编号。整体验收执行全局命令以及全部验收模块的检查。命令在初次运行时保存，恢复运行自动沿用，不接受替换为另一组命令。

同一工作区的所有运行共用项目外状态根目录下的哈希锁，并兼容 `.auto-moniter/workspace.lock`，使用不同 `--run-dir` 也不能并发修改同一项目；不同工作区可独立运行。锁随 monitor 退出释放，遗留锁文件不表示进程仍在运行。

默认连续 3 轮没有工作区或模块验收进展时，monitor 关闭当前会话、清除会话 ID，并要求新会话重新诊断、尝试不同方法，同时保留需求、模块成果和错误证据。即使每轮更换缺口的措辞，也不能绕过这项检测；实际文件变化或新模块验收会重置计数。`--restart-after-stall N` 调整阈值，`0` 关闭自动换会话。它与停止阈值 `--stall-limit` 独立：默认继续干活而非停住；达到显式停止阈值则停止，不再启动新会话。

## 异常退出后的恢复核对

受监控的本地子进程启动时，会在运行目录保存小型 `process.json` 身份记录，正常退出后立即删除。Linux 上 `--resume` 会先检查机器启动标识、PID 命名空间、用户、PID 与启动时间，再停止身份匹配的遗留进程组。PID 已被其他进程复用时不会向该进程发信号。如果原进程组仍有人在运行但身份无法核实，则停止恢复，不启动新 worker。

恢复 Paseo 运行时，先 inspect 已保存的 worker 和活动 reviewer，再停止旧 worker、停止并归档旧 reviewer。已归档的 worker 不会被续接；已停止的 worker 可以继续同一会话。Paseo daemon 不可达、状态无效或停止失败时保存 `recovery_failed`，修复连接后可用同一目录再次 `--resume`。

新运行会保存 worker/reviewer 的 provider、后端、权限模式、可执行路径、命令模板和额外参数的配置指纹。恢复必须匹配，防止把旧会话误接到另一个后端，或无意间取消独立审查。模板与参数只保存哈希；模型及资源预算仍可按需要调整。旧版没有配置指纹的运行仍可恢复，但无法回溯核对旧配置。

显式达到轮次、停滞、失败上限时，也会停止本次运行的后台 agent，并在报告中保留停止失败原因。普通运行需要再次启动 monitor 才会恢复；启用下文的 `--guard` 时，可由独立父进程自动恢复意外退出的 monitor。Paseo 创建 agent 到返回 ID 之间的极短窗口仍无法仅靠本地已保存 ID 找回。

## 独立看守 monitor

需要 monitor 意外退出后自动恢复时，在原有 `run` 命令加 `--guard`。Linux 上会保留一个独立父进程，看守真正执行任务的 monitor 子进程；默认不启用。

```bash
auto-moniter run --cwd /path/to/project \
  --provider claude --reviewer codex \
  --guard --guard-restarts 3 --guard-delay 5s \
  --max-runtime 2h --max-agent-calls 100 \
  '实现原始需求中的全部模块，并通过集成验证。'
```

- 只有 monitor 意外退出、且没有留下本次有效结束结果时才恢复；新 monitor 沿用同一个运行目录，并先清理身份匹配的遗留进程和旧会话。
- 正常交付、人为 SIGINT/SIGTERM、预算耗尽、达到显式轮次/停滞/失败上限，或返回明确运行错误时，都不会自动重启。心跳过期本身也不触发强制重启。
- `--guard-restarts` 默认最多 3 次自动重启，`0` 关闭自动重启；次数跨 `--resume` 累计。`--guard-delay` 默认 5 秒，连续重启按指数退避，最多等待 60 秒。
- 达到重启上限后，停止能核验身份的遗留任务并保留进度；若清理失败则报告 `recovery_failed`。模块进度、验收命令、原始文件基线、累计模型调用次数及运行时长均保留。
- Ctrl+C 或向看守父进程发送 SIGTERM 会停止 monitor，等待其完成 agent 收尾，不会把这次停止当成故障。若在重启等待期停止，看守也会清理已保存的遗留任务。
- `guardian.lock` 防止同一运行目录出现两个看守；`guardian.json` 只覆盖保存当前状态、PID、重启次数及原因，不保存完整工具输出，也不生成工作区备份。`status` 会显示看守记录。

手动继续已有运行时，使用原来的 provider 配置，加 `--resume --run-dir ... --guard`。看守父进程自身被强制杀死或机器重启后仍需手动启动，或由系统服务管理；本功能只恢复它看守的 monitor 子进程。它不会无限重启，也不会因一次心跳过期就杀死仍在工作的 monitor。

## 查看进度与恢复运行

启动时会打印 `run directory: ...`。默认记录目录位于 `$XDG_STATE_HOME/auto-moniter/<项目哈希>/runs/`（未设置时为 `~/.local/state/auto-moniter/...`），与项目分离；也可设置 `AUTO_MONITER_STATE_DIR`。每次运行创建时间戳目录，启动时打印完整路径。旧 `.auto-moniter/runs/` 或显式 `--run-dir` 仍支持，但不会自动迁移。保持运行终端开启，在另一个已激活虚拟环境的终端查看进度；将 `RUN_ID` 替换为实际子目录名：

```bash
auto-moniter watch /path/to/project/.auto-moniter/runs/RUN_ID
auto-moniter watch /path/to/project/.auto-moniter/runs/RUN_ID --once
```

`watch` 采用与 `status` 相同的状态核对，打印状态、轮次、最新结论和诊断。它等待有效结束报告与状态一致、运行及看守均释放锁后退出：确认 done 返回 0，其他结束状态返回 1；进程及看守均未持锁而没有完整结束记录时也返回 1，避免无限等待。恢复过程中的旧报告不会覆盖当前状态，看守持锁的重启间隙会继续等待。`--once` 只打印一次 JSON 摘要，读取成功返回 0，不代表交付成功；应查看 `delivery_confirmed`。这些信息是读取时的快照，不能预知随后启动的新进程。旧版状态中的完整文件基线不会输出。每轮结论、模块编号、错误和失败检查摘要保存在 `NNN-worker.log` 和 `NNN-reviewer.log` 中。

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

`--resume` 支持运行中断（`running`）、信号停止（`stopped`）、预算耗尽（`budget_exceeded`）、恢复核对失败（`recovery_failed`）、认证错误（`action_required`）和旧版因阻塞结束（`blocked`）的运行。它会保留原始文件验收基线、验收要求、会话、失败计数和历史记录，并校验 `--cwd` 与保存的工作区一致。旧版状态没有文件基线时，以恢复时的文件为基线。其他已结束、状态损坏或状态缺失的运行会被拒绝，原有报告保持完整。已有运行目录必须显式加 `--resume`，或改用新目录。

每次运行会写下（逐轮文件遵循历史保留窗口）：

- `requirement.md`：原始需求
- `NNN-worker-prompt.md` / `NNN-reviewer-prompt.md`：发出去的督促
- `NNN-worker.log` / `NNN-reviewer.log`：角色、会话、退出码、中断原因、耗时、包含 scope 的结论、错误及失败检查摘要
- `baseline.json.gz`：压缩保存的初始文件基线，仅写入一次，供恢复及验收使用
- `state.json`：会话 id、轮次、缺口、仓库指纹和基线文件引用
- `heartbeat`：当前轮次和子进程 pid
- `report.json`：结束原因

成功命令的源码、目录列表、进程列表等工具输出不写入日志；启动命令只记录可执行文件和操作，不重复打印提示词或内嵌脚本。结论最多保存 16 KB，错误和失败检查输出按条目截断。提示词仍单独保存在 `*-prompt.md`，方便核对需求及验收约束。

快照和仓库指纹跳过 `target`、`node_modules`、`.venv`、`build`、`dist` 等生成目录，并遵循 Git 对未跟踪文件的忽略规则。审查前后仅计算文件内容、权限和符号链接的指纹，不复制工作区，不生成 `review-backup-*`。如果工作区在审查期间发生变化，运行会失败并生成诊断报告；不会自动回滚或继续覆盖现场。可处理的异常和 SIGINT/SIGTERM 会写入结束状态及报告，日志会记录中断原因。旧版遗留备份不会自动删除。

恢复旧版运行时会读取原有的内嵌文件基线，迁移到 `baseline.json.gz`，保留原始验收基准；之后的 `state.json` 不再重复存放完整基线。

退出码：`0` 表示任务完成且验收通过，`1` 表示达到显式设置的停滞/失败/轮次上限、目录锁被占用、运行异常或被信号打断，`2` 表示参数或恢复状态错误。

## 循环怎么停

- 工人和审查员都明确给出 `status=done`、`scope=requirement`，均有证据且没有缺口、下一步或阻塞项。模块级 `done` 仅触发模块切换，不结束 monitor。结论仍在询问是否继续、缺少必填字段或格式错误时继续监控。`--no-review` 只省略独立审查，仍须工人给出 `scope=requirement` 的明确完成结论和文件证据。
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
  --reviewer codex \
  "实现登录失败三次后的锁定。"
```

模板占位符：`{prompt}`、`{prompt_file}`、`{cwd}`、`{schema}`、`{session_id}`、`{model}`。值已经按 shell 转义。提示正文里的花括号不会被当成占位符。

自定义 shell 模板仅支持 worker。reviewer 请选择提供只读或 plan 模式的原生后端；自定义命令审查会在运行开始前被拒绝。

## 安全

工人默认使用各自 CLI 的非交互、自动批准模式。Paseo 的 Claude 工人默认使用 `bypass`，Codex 工人默认使用 `full-access`，其他后端保留其默认模式。`--mode` 可覆盖工人默认模式，选择交互批准模式可能触发等待输入后的重试。`--reviewer-mode` 仅允许只读或 plan 模式，`--reviewer-extra-arg` 不允许覆盖权限、配置、工作目录、后端或会话。审查模式及 Paseo 的隔离限制见上表。只在你信任的仓库里运行。新任务经独立审核与固定检查后，监督器将业务代码提交到启动时检出的业务分支，将审计结论写入隐藏引用；不执行推送。

### 目标项目中的 Git 追溯与审核

新建 `review-first` 任务绑定启动时检出的本地业务分支。worker 实现、reviewer 只读审核、watcher 固定检查全部通过后，watcher 在该业务分支创建本地代码提交。正常 `git log` 能看到批准后的代码；提交不包含 `.auto-moniter` 审计文件，也不会自动推送。任务必须从业务分支启动，detached HEAD 和旧审计分支会被拒绝。

审计结论独立保存于 `refs/auto-moniter/<运行标识>`（新任务审计提交不包含业务源码快照），不会出现在 `git branch` 的普通分支列表。Web 分开展示“业务分支/业务提交”与“审计引用/审计提交”；只有审计快照的旧任务明确标为历史模式，不能误称代码已提交到 main。

提交使用私有索引构造准确的业务 tree。项目为仓库子目录时保留其他目录；无关的用户暂存内容保留，若已暂存文件与本次业务提交重叠则停止并提示处理，不覆盖暂存内容。代码分支和审计引用通过带旧值校验的 Git 引用事务更新，暂存区通过原子替换同步；中断恢复依赖有界的发布日志，不重复创建业务提交。提交冲突解决后，未变化候选可复用已完成审核，仍独立执行固定检查。未批准候选由 `refs/auto-moniter-work/<运行标识>` 临时保留，避免 Git gc 丢失断点；任务归档时一并移除，不显示为普通分支。

reviewer 审核不可变候选 tree 与上次有效业务审核以来的差异，并能核对准确的业务提交 diff（包含任务启动前已有、纳入候选的项目修改）。最终完成证据仍必须包含完整候选哈希及真实变更文件。已提交模块的证据相对任务初始基线核验，不会仅因业务 HEAD 推进而失效。

任务启动前已删除的受 Git 跟踪源码也纳入业务提交 diff，审核通过后正确提交删除；项目为仓库子目录时，其他目录和排除的构建产物仍保留。

```bash
# 业务代码只在独立批准和固定检查后本地提交
# 先自行检出应接收代码的业务分支，再启动任务
auto-moniter run '实现需求' -C /path/to/project --provider codex --reviewer codex --git-history required

# 当前业务分支的代码提交
git log --oneline

# 独立审计记录；使用任务详情给出的实际标识与提交号
git log --oneline refs/auto-moniter/<运行标识>
git show <审计提交>:.auto-moniter/history/<运行标识>/001-worker.json
```

历史任务沿用保存的提交策略，升级不会自动把旧审计快照发布到业务分支。旧 `auto-moniter/<标识>` 分支仍可读取，并可显式迁移到隐藏引用；迁移保留提交号、审计内容、恢复配置及 Web 追溯。`legacy` 流程的新记录也使用隐藏引用，保留原来的每轮审计策略，不自动发布未经独立批准的业务代码。

### Git 审计迁移、归档与清理

新任务成功结束并释放运行锁后，自动按保留策略维护同一运行根目录内的历史：默认保留最近 5 个结束任务，超过 7 天的已成功任务先生成并校验 Git bundle，再删除审计引用及项目内的旧结论副本。归档默认至少保留最近 10 份，且只有超过 90 天又超出数量的归档才过期。运行目录、业务代码提交及当前任务不会因此删除。

停止、阻塞、预算耗尽、恢复异常、运行锁被占用、遗留进程或发布恢复尚未完成的任务始终保留。失败/stalled/max_rounds 默认保留，只有显式 `--include-failed` 才纳入清理。归档包位于目标仓库的 Git 公共目录 `auto-moniter-archives/`，不在业务工作区内；计入原有 Git 历史容量预算。删除引用不代表 Git 对象立即释放，不强制执行全库 gc；Git 正常垃圾回收后才可能回收无引用对象。

显式清理从未获批的失败任务时，只移除归属和旧值均匹配的候选引用，并保存无批准历史的清理记录，不生成源码归档、不改业务代码。清理中断后可重试；候选引用被外部修改时保留现场并报错。运行目录清理同样先处理该引用，避免删除检查点后遗留无主候选。

```bash
# 以下操作默认仅预览；--runs-dir 可重复，用于自定义运行根目录
auto-moniter history list -C /path/to/project
auto-moniter history migrate -C /path/to/project
auto-moniter history migrate -C /path/to/project --apply

# 自定义保留策略，仍须 --apply 才实施
auto-moniter history cleanup -C /path/to/project --older-than 7d --keep-runs 5 \
  --keep-archives 10 --archive-older-than 90d
auto-moniter history cleanup -C /path/to/project --older-than 7d --keep-runs 5 --apply

# 从尚未过期的 bundle 恢复审计引用；不修改业务分支或重新执行任务
auto-moniter history restore /path/to/task-run -C /path/to/project --apply
```

普通 `cleanup --apply` 删除旧运行目录前也会先归档对应 Git 历史，失败则保留该运行目录。运行目录已清理时仍可用原路径恢复其审计引用，通过 `git log/show` 追溯；不会复造已删除的任务执行状态。归档过期后无法承诺恢复已被 Git 垃圾回收的旧审计对象。

迁移、归档和恢复都在运行锁与工作区租约下执行，拒绝覆盖冲突引用、已检出的旧审计分支、符号链接或不匹配的任务身份。归档中的业务结论仍以 `watcher_result` 和 AUD 台账为准；reviewer 的关闭申请不等于系统已接受。

本次改造验证：Python 全量回归 536 项通过；两份前端 Node 测试通过。新增用例覆盖业务提交门禁、暂存保护、子目录发布、引用事务失败、提交中断恢复、已提交证据复用、候选 GC 保护、旧分支迁移、归档验证失败保留、归档恢复及年龄/数量清理。本项目已有两个旧审计分支已迁移，原提交号保留，真实 Web 历史弹窗读取通过。

后续可靠性排查已修复跨轮删除漏提交、损坏 bundle 误通过、候选引用与检查点之间的中断，以及索引半写时恢复失败。完整回归 544 项通过；运行目录清理、审计维护及 Web 删除会保留待恢复的发布/候选检查点。复现与验证说明见 [提交与归档可靠性检查](docs/git-publication-reliability.md)。

最新排查还修复任务启动前的删除漏提交，以及未批准失败任务清理后残留候选引用。新增 7 项回归覆盖子目录范围、中断重试、外部引用保护和运行目录清理；Python 全量 551 项、两份 Node 前端测试通过，真实 Web 控制台只读检查正常。

非 Git 目录在 `auto` 模式下继续原流程；`required` 会提前拒绝非 Git 项目。`--git-history off` 显式关闭新运行的 Git 留痕，已留痕运行恢复时禁止关闭。

审查员只能读取工作区和已有验证证据，不负责实现修复或执行会生成文件的构建、测试。需要修改代码或补充验证时，reviewer 返回缺口，由 worker 完成后再审查。监督器只检查审查前后指纹，不保存文件副本；检测到变化立即停止，保留现场供检查。原生 Codex 使用只读沙箱，其他支持后端使用 plan 权限模式，各后端的隔离能力取决于其 CLI。验收证据必须指向项目内的真实变更文件，运行日志及指向项目外的符号链接不能作为证据。

Paseo 的 `run` 没有提示文件入口。短提示仍放在参数里。UTF-8 编码超过 10 万字节时，正文写入 `.auto-moniter/inbox-prompt.md`，参数里只要求 agent 读取该文件。Paseo 不使用 `--output-schema`。创建/续接先用 `--no-wait` 取得 `agentId` 并立即记录，再通过 `inspect` 和 `logs` 观察状态。等待输入、超时或进程异常时先停止 daemon 中的 agent，再续接同一个 agent；本地 CLI 退出不会被当成 daemon 工作已经结束。结论取 `paseo logs` 最后一段 `---` 分隔的正文。如果输出是 JSON timeline，则取最后一条 `assistant_message`。每轮审查 agent 结束后会停止并归档，失败时停止运行并报告。每个模块验收通过以及整份需求验收通过后，也会停止并归档当前 Paseo worker；模块切换前收尾必须成功，收尾失败不会写成成功交付。原生 CLI 的 worker/reviewer 子进程则在每轮返回前已等待退出并回收。

同一个工作区及同一个 `run-dir` 都有文件锁，冲突的第二个监督器会直接退出。收到 SIGINT 或 SIGTERM 时，如果工人是 Paseo agent，监督器会调用 `paseo agent stop`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时工作区与模拟命令，不调用付费模型。独立验收覆盖失败返工、超时、命令恢复一致性、模块与整体检查区分；另覆盖同项目跨运行目录互斥、停滞自动换会话。回归覆盖询问后继续、阻塞后继续、完成条件、模块验收后关闭会话并继续原始需求、整份需求完成判定、无备份只读审查、权限覆盖拒绝、审查期间文件变化检测、Paseo 等待输入续接、输出解析、验收证据、恢复运行、目录锁、超时和信号停止。

## 0.4.0 上线加固配置

默认状态已移出项目；目录权限为 0700。原生 worker 可加 `--worker-isolation strict --protect tests --protect pyproject.toml`，由 bwrap 将状态和所选验收文件挂载只读，项目其余部分可写；需要先准备独立验收文件，不应将待开发源码设为 protect。该模式不支持 Paseo daemon，状态目录必须在项目外。只移动目录不能隔离同 UID 的无限权限 worker；需要启用此模式，部署时再用独立用户和服务边界。

`--guard-stop-timeout 30s` 限制优雅停止等待，超时发送 SIGKILL，再进行有期限的遗留任务核对。不能核验身份或清理失败时报告 recovery_failed，不继续开新 agent。

`--memory-bytes` 限制原生 subprocess 每进程虚拟地址空间，`--file-bytes` 限制继承的单文件写入大小；不支持远端 daemon agent。`--max-run-bytes` 每秒检查一次运行目录总文件大小，达到预算后停止并尽量保存诊断，不是文件系统硬配额。整个进程树的内存、任务数由 [systemd 模板](deploy/auto-moniter@.service) 的 MemoryMax/TasksMax/KillMode 限制；项目总体磁盘硬配额需在部署文件系统配置。

结论、错误输出、事件、最终报告和 CLI 摘要对敏感环境变量、Bearer、常见 password/api_key/token 字段及 URL 密码做脱敏。原始需求、可执行验收命令和提示词为保证执行准确性仍可能包含秘密，保存在私有目录；脱敏是启发式处理，不保证识别所有秘密，生产凭据应通过受限环境提供。异常状态会覆盖写入 `alert.json`，可由现有监控采集，不会自行发送邮件或外部消息。

服务部署示例在 `deploy/`：manifest 按参数数组启动，不拼 shell；systemd 在 abnormal exit 时恢复服务，不对预算/认证退出无限重启。需要管理员按实际路径、用户、CLI 登录环境及磁盘配额配置，仓库不会自动安装系统服务。

`tools/certify_backend.py` 提供固定 worker/reviewer CLI 版本的真实小任务验收，只有明确传 `--execute` 才调用模型。`tools/soak.py --hours 24 --output soak.json` 提供离线重复生命周期耐久入口；二者不是同一项测试。此次只执行离线短时 smoke，真实后端认证和 24 小时连续验收仍待部署环境完成。`.github/workflows/verify.yml` 定义 Python 3.11/3.12 测试、构建、独立安装验证；未提交/推送时不会在 GitHub 实际运行。

## 输入到交付的运行补齐（2026-11-08）

Web 新建任务已补齐固定模块、验收模式、严格审核/worker 隔离与保护路径、单轮/空闲/验收超时、日志保留和容量限制。新页面默认要求独立验收命令；选择“仅审核”不代表功能测试通过。恢复窗口支持明确追加累计时间及 worker/reviewer 调用轮次，不清零用量。详细默认值、边界和配置示例见 [控制台配置核对](console/CONFIGURATION_AUDIT.md)。

工作区使用祖先共享、目标独占的层级锁；同一 Git 工作树的各子目录使用共同工作树租约。父子项目不能同时写入，独立 linked worktree 不受此限制。

新增 `--max-history-bytes` 和 `--max-agent-state-bytes`，分别限制共享 Git 对象/项目审计历史、已知本机执行器会话目录的合计字节；0 关闭。每 10 秒采样，超限停止且保存原因，不删除历史；这是运行停止预算，不是文件系统硬配额。原有 `--max-run-bytes` 仅限制运行目录。共享会话可能来自其他任务，Paseo 或未登记的第三方存储需要部署层配额。

`auto-moniter selftest` 现在执行两个 worker 与两个独立 reviewer 本地子进程，验证首轮审核拒绝、反馈修复、AUD 显式关闭、验收命令及报告输出，不调用模型。真实后端认证仍使用 `tools/certify_backend.py`，支持 `--model`、`--reviewer-model` 和 CLI 版本固定；只有实际执行并成功的认证才证明对应后端当时可用。

NDR 文档监督有独立的只读静态门禁：

```bash
/path/to/validation-python tools/check_documentation.py \
  --project /path/to/022_NDR_SIEM \
  --baseline /outside/project/documentation-baseline.json.gz
```

该入口专门适配 NDR 现有 100 焦点审阅器。可信基线必须在目标项目外，由监督方在确认输入后保存；非 Markdown 资产变化仍失败。只有历史记录中已修订 Markdown 的哈希差异被明确归为历史证据，其他断言不变。输出带当前工作树指纹及历史文档列表，不改历史验证记录，不代表当前运行或生产验收通过。原审阅器的严格历史哈希检查保持原样；旧任务参数不会被静默替换，应使用新配置创建后续任务。

## 质量策略：从持续活动到可验证交付

新增 [质量策略](QUALITY_POLICY.md)：固定需求与模块/文件证据映射、保护现有测试与检查脚本、以首次检查通过/模块接受/AUD 关闭判断进展，并提供按时间与文件改动数触发审核、重复外部阻塞停止、按模块文件范围和依赖安排回归。新 Web 表单预填策略；CLI 使用 `--quality-policy <JSON文件>`。原任务恢复保持原策略，不静默升级。

可选的 `files_only` 审核工具模式目前仅支持原生 Claude + strict，禁用命令、自定义扩展和 MCP；其他后端不声称具备相同能力。后端实际报告的 token/费用显示在 watcher 详情，并可设定停止预算；缺失报告明确计为未知，不当作零费用。具体语义、例子和限制见策略文档。

## 无需手填命令的验收配置

新建任务增加“完成标准与检查方案”：选择类型、用文字写完成标准，点击扫描后勾选已有检查，再检查运行前提。目的、耗时、副作用先展示，命令保留在展开详情和高级配置中。没有现成检查可明确创建“先制定验收方案”的单独任务，不把方案完成当作功能验收通过。

详情页显示实际检查的版本、结果分类和输出日志，区分环境缺失、检查未通过、超时与中断。具体操作、支持的入口及能力边界见 [引导式验收说明](ACCEPTANCE_GUIDE.md)。旧任务及原命令 API 保持兼容。

## 需求先审与审核后提交

新建带审核的任务默认先由 reviewer 只读完善需求，watcher 固化补充后交给 worker；每轮正常实现结果再次送审。缺口反馈给 worker 继续修复，只有双方确认及 watcher 检查通过后才生成本地审计分支提交；未通过轮次保留运行记录，不创建提交。旧任务恢复沿用原流程，详见 [工作流程](WORKFLOW.md)。

Grok 未登录时会明确返回需要处理，不再作为普通失败反复重试。请在控制台运行用户下完成 Grok 登录或配置服务的 XAI_API_KEY 后恢复；安装 CLI 并不代表认证成功。
