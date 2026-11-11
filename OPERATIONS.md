# auto-moniter 0.4.1 运维说明

本版本提供单机 Linux CLI 和 Web 控制台：对一份原始需求持续推动 worker 实现、reviewer 只读验收，完成全部模块后收尾退出。目标是保留现有代码和进度、明确记录失败、不凭 AI 自述直接交付。Web 启动与管理见 `console/README.md`。

## 安装与验收

```bash
python3 -m venv .venv
.venv/bin/python -m pip install /path/to/auto_moniter-0.4.1-py3-none-any.whl
.venv/bin/auto-moniter --version
.venv/bin/auto-moniter doctor --cwd /path/to/project
.venv/bin/auto-moniter selftest
```

也可以从源码 `pip install .`。要求 Python 3.11+、Linux `/proc`、POSIX 进程组和文件锁；完整测试在 Python 3.12 完成。无需第三方 Python 运行时依赖。agent CLI 需另行安装并配置登录；monitor 在创建运行记录或启动 worker 前检查所选内置 CLI 可执行文件是否存在。

其他语言的函数范围校验使用可选依赖：在 monitor 的 Python 环境安装 `pip install 'auto-moniter[languages]'`（源码安装用 `pip install '.[languages]'`）。`doctor` 的 `function_languages` 显示已注册语言的可用状态；Reviewer 同样会收到解析器目录。Watcher 按扩展名选择本地语法规则，不执行目标代码、不在任务运行时安装或下载解析器。函数语法错误、重名或缺少解析器会明确拒绝，不会降级为整文件修改。离线部署可事先下载带依赖的 wheel，再使用 `pip install --no-index --find-links=/path/to/wheels 'auto-moniter[languages]'`。

恢复时若出现 `Cannot resume checkpoint`，检查 `resume-error.json` 和控制台的具体异常。保护覆盖检查点载入、Git 历史初始化及遗留进程回收：全部成功前不会用不完整运行状态覆盖原 `state.json`、报告、交接或文件哈希基线；修复后继续使用原检查点。追加运行时间或模型调用额度只能解决预算不足，不能修复程序异常。恢复窗口中的“调用轮次”指 worker、reviewer 及需求完善的模型调度次数，不是 worker 开发轮数。

`selftest` 是离线本地 fixture，不使用模型、不检查登录。实际后端的 CLI 版本、认证和模型输出质量必须在目标环境验证。不要把离线测试通过理解为所有厂商后端都完成实机认证。

## 日常运行

1. 在项目自己的环境中确认测试命令可以独立执行。
2. 启动时提供需求和 `--check`，复杂需求可用 `--modules` 明确模块和验收条件。
3. 无人值守可启用 `--guard`，并设置总时长、调用预算。默认历史保留最近 100 轮。
4. 通过 `status RUN_DIR` 查看模块、预算、心跳和看守状态；`watch RUN_DIR` 跟随最近结论。
5. 最终以 `report.json` 的 `status=done`、模块状态和 `accepted_tree` 为交付记录。记录代表当时已审核的工作区版本；交付后继续改动代码，需要重新验收。
6. 用 `runs -C PROJECT` 查看各次运行占用；`cleanup -C PROJECT` 预览旧记录清理，追加 `--apply` 执行。默认保留最近 5 次结束记录，仅删除超过 7 天的成功运行。旧版目录用 `--runs-dir PROJECT/.auto-moniter/runs` 指定。

独立审查默认最多间隔 5 轮 worker 工作（`--review-every 5`）；完成候选及阻塞会立即触发。`0` 关闭周期审核。该计数和最近有效业务审计跨历史清理、失败及恢复保存；reviewer 执行失败或结论无效不会重置审核计数，也不会覆盖有效业务意见。周期审核使用模型调用预算，长时间的单个 worker 轮次仍由超时与无输出检查控制。

`audit_issues` 是 watcher 保存的逐项审计清单。worker 负责修复，reviewer 用 `issue_resolutions` 引用 `AUD-*` 编号并提供独立关闭说明和有效文件证据；省略旧问题不会关闭它。模块验收须清除关联及需求级问题，整体验收须清除全部开放问题。源码变化使保留的关闭记录重新待审。最多保留 128 条记录及其中最近 20 条关闭记录，清单最多 512 KiB；容量耗尽保存 `budget_exceeded`，禁止以删除开放问题的方式腾空间。完整格式与边界见 README。

旧问题反馈使用 `AUD-1: ...` 或 `AUD-1：...` 保留原编号和范围。同一份审核可关闭旧问题后腾出容量登记新问题；有效关闭计为进展，避免因没有源码改动而误触发换会话。模块检查及里程碑只处理双方共同认可的模块集合，并包含所需依赖检查。

`--no-review` 是显式信任 worker 的弱模式，仍须通过模块及文件证据、配置的命令检查。默认权限隔离由后端 CLI 提供；需要系统级文件隔离时使用本地 reviewer 加 `--review-isolation strict`，要求 Bubblewrap，探测失败会拒绝启动。

## 停止与恢复

Git 项目默认逐轮写入目标仓库的 `auto-moniter/<运行标识>` 分支；项目内 `.auto-moniter/history/<运行标识>` 保存结论，运行目录 `git-history.json` 保存分支检查点。恢复需要同时保留目标仓库、此检查点及 `state.json`。业务 HEAD 和暂存区保持原状，reviewer 按记录的前后提交执行 Git diff 并引用完整 worker 提交号。需要强制保证留痕时指定 `--git-history required`。

出现 `Git audit branch changed`、`Git checkpoint is ahead of run state` 时，先备份运行目录，使用 `git log`、`git show` 对照项目内结论与两个检查点核对最后已完成轮次。保留原审计分支，修复保存状态后再恢复；无法确认一致性时创建新运行，从当前项目建立新基线。不要通过删除审计分支或关闭 Git 历史绕过检查。Git 提交失败会停止派发和交付，检查仓库权限、磁盘空间及 Git 错误后处理。

Git 对象及项目内结论文件不会随 `--keep-rounds` 或运行目录清理而删除；源码快照会增加目标仓库磁盘占用。目标目录为仓库子目录时，仅记录该项目范围。恢复旧版任务只对后续轮次建立 Git 记录。

单轮超时、无输出超时、重试间隔、验收命令超时及捕获输出上限会随检查点保存。恢复时省略对应参数沿用原值，显式指定才覆盖；旧版缺少配置的检查点使用原默认值。长时间构建恢复后不会因此自动回到较短的默认超时。

正常运行、看守启动预检和异常收尾均按相同顺序取得项目外工作区锁、项目内兼容锁及运行锁。项目内 `.auto-moniter` 被清理也不会绕过外部锁；锁被其他任务持有时，看守拒绝启动，异常收尾拒绝恢复进程或写回检查点。应先确认已有任务状态，再重试。

Paseo 在超时、等待权限、调用失败或主动停止后，若停止 agent 的请求失败，会保存 `recovery_failed` 和 agent ID，禁止继续派发下一轮；停止或达到轮次上限时收尾失败也使用此可恢复状态。修复 daemon 连接后通过原目录 resume，先核对并停止旧 agent，再继续工作。本地 worker 或验收进程仍活着、或进程组收尾抛出异常时，会保留 `process.json`，不会提前删除恢复身份记录；正常完成并回收后才删除。

看守在 monitor 异常退出后读取新报告时，会核对 `state.json` 的结束状态是否一致；缺少或损坏状态、状态未完成、成功报告仍残留进程身份记录时，不会仅凭报告声称交付。完整可恢复检查点仍按原恢复规则处理。

| 现象/状态 | 处理 |
| --- | --- |
| 想暂时暂停 | `pause RUN_DIR`，在轮次边界暂停；`continue RUN_DIR` 清除暂停请求 |
| 想停止 | `stop RUN_DIR`；看守不会因此重启 |
| `budget_exceeded` | 保留目录，用原配置 `run --resume --run-dir RUN_DIR` 并提高相应预算 |
| `action_required` | 修复认证或登录，再手动 resume；不会自动反复请求无效凭据 |
| `recovery_failed` | 检查 daemon 连接或遗留进程身份，处理原因后 resume；不要删基线来绕过核对 |
| `heartbeat_stale` | 先检查活动阶段、进程和日志；心跳过期不等于 AI 卡死，看守不因此自动杀进程 |
| monitor 被强制杀死 | 启用 guard 时在重启上限内自动恢复；否则使用原目录 resume |
| `failed` / `stalled` / `max_rounds` | 是明确结束结果，看守不自动循环；查看报告，修复环境或调整需求后启动新运行目录 |
| `locked` | 已有 monitor/看守持锁；先确认已有进程，锁文件存在本身不表示正在运行 |
| `stop` 后想重新运行 | 先 `continue RUN_DIR` 清除持久停止请求，再 `run --resume` |

resume 应沿用原 cwd、provider、后端、权限、命令模板及额外参数。模型、预算和周期审核间隔允许调整；模块定义、文字验收要求 `--accept` 及验收命令不能替换，省略时继承已保存配置。新状态含 `state_version=1`；旧版无版本字段按既有格式读取，未知未来版本拒绝写回。

## 空间、进程和边界

CLI 退出并回收后，收尾先探测原进程组是否还存在；确认不存在时直接结束检查，减少读取输出尾部期间反复扫描宿主机 `/proc` 的开销。存在或无法确认时仍执行原来的成员核对和后台进程清理。该优化不缓存进程状态，也不跳过遗留进程检查。

- 审核不创建 `review-backup-*`。Git 项目另有逐轮源码快照和结论提交，运行目录保留文件哈希基线、当前模块状态及有限日志。旧备份不会自动删除。
- 事件日志总量约 512 KiB。逐轮文件及详细 records 默认保留最近 100 轮；单条输出受捕获上限控制。单个巨大需求或提示词仍需相应空间，没有整个目录的硬配额。
- 旧运行目录通过 `cleanup` 显式清理，不自动删除。可恢复任务、持锁任务、残留进程记录、异常元数据及挂载目录均保留；失败结果默认保留，需 `--include-failed` 才可选中。空间统计是估算，清理不涵盖后端会话及系统日志。完整规则见 README。
- CLI 退出后，仍在本次进程组内的后台任务会被清理，本轮拒收。主动脱离进程组的程序、远端服务及外部副作用不由此机制完全管理。
- monitor 遗留本地进程恢复基于 Linux PID、启动时间、启动标识、命名空间及用户身份。无法确认身份时停止，避免误杀其他任务。
- guard 只看守它的 monitor 子进程；父进程或机器重启后应手动恢复，或由现有系统服务管理。没有自动部署到系统服务。
- 测试命令运行在真实项目中；已跟踪源码发生变化会使旧验收失效，Git 忽略的构建产物不参与源码指纹。
- 本工具依赖可信的目标项目与执行用户。worker 可写项目，能够接触项目中的测试和控制文件；该工具不是对抗恶意 agent 或不可信代码的安全边界。

## 开发验证与构建

```bash
python3 -m unittest discover -q
python3 -m compileall -q auto_moniter tests
git diff --check
python3 -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
```

构建环境需要 setuptools>=68 和 wheel；运行环境不需要。发布前应在全新虚拟环境中安装 wheel，并在源码目录外执行 `--version`、`doctor` 和 `selftest`。测试使用临时工作区及模拟 agent，不发送消息、不调用付费模型。

## 加固部署与尚待实测的门槛

默认 run-dir 已移到 XDG 状态目录并设 0700。对生产原生后端启用 worker/reviewer strict 隔离；--protect 指定不可变验收目录。现有 run-dir 可继续恢复，strict worker 要求项目外目录。仅凭目录移动无法约束同 UID 的完全访问 worker。

`deploy/task.example.json` 与 `deploy/auto-moniter@.service` 是未安装模板。创建专用低权限用户、配置它的 CLI 凭据、准备受保护验收文件、挂载项目磁盘配额，核对路径后再安装。MemoryMax=8G、TasksMax=256、LimitFSIZE=1G 和 KillMode=control-group 是示例硬限制；可按项目调整。TimeoutStopSec=90 是整个服务停止上限，包含 guardian 30 秒优雅停止及后续核对。

--max-run-bytes 是每秒采样的软预算，--file-bytes 是原生子进程单文件硬限制，--memory-bytes 是每进程虚拟内存限制；它们不代替 cgroup 总内存和文件系统总配额。daemon agent 不受本地 worker wrapper 约束。

alert.json 是本地告警记录，日志也进入 journal；由部署方已有监控采集，不会自动通知外部。已知凭据模式会从诊断输出脱敏，提示词和执行配置仍是敏感文件；状态目录及备份访问应限制给执行用户。

上线前需记录精确 CLI 版本并执行 tools/certify_backend.py 的 --execute 验收，再完成真实项目 24 小时故障与耐久测试。离线 tools/soak.py 仅覆盖重复 lifecycle。CI 配置已提供，但仓库尚未提交或推送，不能视为云端 CI 已通过。提交、tag、上线及系统服务安装应走部署方发布流程。
