# README 与 Web 配置覆盖核对

检查日期：2026-11-06。依据根目录 README、console/README、CLI 参数定义、新建任务表单、请求序列化、后端校验及实际启动参数逐项核对。

结论：**尚未全部覆盖**。Web 具备基础任务配置与运行操作；若干高级 CLI 参数仍没有页面配置入口。恢复已有任务会保留原启动参数，这不等同于能在页面修改这些参数。

## 已有输入及选择入口

入口为“新建开发任务”，高级项位于“执行配置与验收标准”。

| README / CLI 配置 | 页面入口 | 实际接线与边界 |
| --- | --- | --- |
| 原始需求、`--cwd` | 开发需求、项目目录 | 需求通过 stdin 传给 CLI；当前表单不提供 `--file` 文件读取入口 |
| `--provider`、`--reviewer` | 执行器、独立审查器 | 提供 7 种内置执行器选项；`command` 不在 Web 选项中；命令存在不代表已登录，后端组合仍需满足 CLI 能力限制 |
| `--model`、`--reviewer-model` | 执行模型、审查模型 | 独立传入；审查模型为空时沿用非空执行模型，否则使用后端默认模型。已修正表单提示 |
| `--paseo-backend` | Paseo 后端 | 使用 Paseo 时展示；审查器没有独立 backend 输入 |
| `--check` | 验收命令 | 每行一条，最多 20 条、每条 2000 字符；传入全局检查列表 |
| `--max-runtime` | 运行预算（分钟） | 传 CLI 前换算为秒，默认 120 分钟；0 不限，最大 43200 分钟 |
| `--max-agent-calls` | 模型调用上限 | 包括 worker/reviewer，默认 100；0 不限，最大 100000 |
| `--max-rounds` | 执行轮次上限 | 默认 0 不限，最大 100000 |
| `--review-every` | 审核频率（本次补齐） | 0 至 100000，默认 5；1 表示每轮正常结果后审核；0 关闭周期审核但保留完成/阻塞审核 |
| `--resume`、pause/continue/stop | 恢复执行、暂停、停止等按钮 | 按实际状态展示；恢复沿用 launch.json 与检查点，当前没有运行参数编辑表单 |
| Web 任务名称、创建后立即启动 | 对应输入框与复选框 | 用于控制台元数据及启动操作，不是 CLI run 的独立参数 |

审核频率已核对从表单数字输入、JSON 数字转换、后端整数校验、任务定义持久化到 `--review-every` 启动参数的完整路径。新建任务缺少此字段时采用默认 5；已有启动配置明确指定的值在恢复时继续保留。旧版托管任务缺少启动配置和新字段时，从检查点继承审核频率，避免用默认 5 覆盖已保存的 2。

现有任务尚无“恢复前修改参数”表单。特别是累计运行预算已耗尽的任务，直接用原预算恢复可能立即再次停止；需通过 CLI 明确调整预算，或另建后续任务。这属于 Web 配置入口的缺项，不能把“恢复”按钮视为自动增加预算。

## Web 采用固定策略的配置

| 配置 | Web 行为 |
| --- | --- |
| `--no-review` | 新建任务始终配置 reviewer，没有关闭独立审核的开关 |
| `--fresh-review` / `--no-fresh-review` | 新建任务采用 CLI 默认的新审查会话策略，没有切换入口 |
| `--git-history` | 新建任务固定 required，页面只读展示 Git 记录；没有 auto/off 选择 |
| `--guard` | 新建任务固定启用看守，没有关闭开关 |
| `--run-dir` | 新任务目录由控制台数据目录与任务编号生成，没有逐任务输入 |

这些项目有执行策略，但不属于页面可配置项。使用 CLI 创建并导入的任务按自身保存配置恢复，不会套用新建任务的全部固定策略。

## README 中存在、Web 仍缺少配置入口的项目

| 类别 | 尚未提供的输入 / 选择 | 当前行为 |
| --- | --- | --- |
| 权限模式 | `--mode`、`--reviewer-mode`、`--paseo-mode`、`--reviewer-paseo-mode` | 采用所选后端默认模式；审查仍受只读/plan 校验 |
| 审查 Paseo 后端 | `--reviewer-paseo-backend`（CLI help 提供） | 采用 CLI 对共享 backend 的回退逻辑 |
| 额外验收标准 | `--accept` | 只能写进原始需求或补充要求，不能独立配置此参数 |
| 预定义模块与依赖 | `--modules`、模块 acceptance / checks / depends_on | 可查看运行中模块，不能在 Web 新建时提供结构化模块清单 |
| 超时与重试 | `--turn-timeout`、`--idle-timeout`、`--retry-delay`、`--check-timeout` | 新建任务采用 CLI 默认值；原任务恢复时沿用保存值 |
| 停滞及失败策略 | `--stall-limit`、`--failure-limit`、`--restart-after-stall` | 没有页面参数编辑入口 |
| 日志及容量限制 | `--keep-rounds`、`--max-output-bytes`、`--max-run-bytes` | 可查看资源与日志，不能在表单设置保留窗口和这些容量预算 |
| 原生子进程限制 | `--memory-bytes`、`--file-bytes` | 资源展示不等于能配置执行限制；表单未提供 |
| 文件隔离与保护 | `--worker-isolation`、`--review-isolation`、`--protect` | 新建任务采用 provider 隔离；不能选择 strict 或配置只读保护路径 |
| 看守恢复参数 | `--guard-restarts`、`--guard-delay`、`--guard-stop-timeout` | 新建任务仅固定启用 guard，具体参数沿用 CLI 默认值 |
| 后端附加参数 | `--extra-arg`（CLI help 提供）、`--reviewer-extra-arg` | 没有逐项参数输入，审查参数仍须遵守 CLI 的权限限制 |
| 自定义工人命令 | `--provider command`、`--exec`、`--resume-exec` | Web 不支持这类任务的新建及导入恢复，须使用 CLI |
| 需求文件 | `--file` | Web 只提供需求文本输入，未提供服务器文件选择或上传入口 |

`--reviewer-exec`、`--reviewer-resume-exec` 为 CLI 保留参数，自定义命令 reviewer 已不受支持，不应作为待补 Web 功能。

执行器下拉目前也未按 Paseo 后端过滤 reviewer 的有效组合。README 规定 Paseo reviewer 仅支持 Claude plan；如果共享 backend 选择 Codex/Grok，同时选择 Paseo reviewer，会在 CLI 启动预检被拒绝。存在下拉选项不等于所有组合都可执行，这仍是待完善的页面校验。

## 控制台部署、观察及运维配置

| 配置 | 页面情况 |
| --- | --- |
| console 的 `--port`、`--cwd`、`--data-dir`、`--runs-dir` | 仅启动命令配置；任务的“项目目录”不等于控制台默认目录或发现根目录设置 |
| `AUTO_MONITER_STATE_DIR`、XDG 状态目录、`CODEX_HOME` | 由服务进程环境提供，没有 Web 环境变量编辑入口 |
| 页面刷新间隔、状态过期阈值、日志显示容量 | 使用实现中的固定值，没有对应 `watch --interval` / `--once`、`status --stale-after` 的页面偏好设置入口 |
| `runs` / `cleanup` 及 `--older-than`、`--keep-runs`、`--include-failed`、`--apply` | Web 未提供运行存储清理页面 |
| `providers`、`doctor`、`selftest` | 新建表单能看到执行器是否存在；没有完整诊断/自检页面 |
| systemd MemoryMax / TasksMax / 文件系统配额及 CLI 登录/API 配置 | 属于部署或执行器环境，需在系统及对应 CLI 中配置 |

## reviewer 等待现象的核对

本次截图对应记录中，reviewer 在第 2 轮实际完成了独立审核，记录了两项业务缺口；第 3 轮 worker 超时，第 4、5 轮 worker 被停止，第 6 轮仍在执行。采样时保存的审核频率是 2，累计间隔为 3。

实现会先处理失败/停止分支，再判断周期审核，因此“已达到计数阈值”并不代表 reviewer 正在运行；需等下一轮 worker 正常返回可审核结果。页面已补充实际频率、累计间隔、最近审核轮次和等待原因，防止将串行等待误读为没有配置或从未执行 reviewer。未改动现有任务的进程、原始需求或保存的审核频率。

后续复核：第 6 轮 worker 正常返回后，watcher 已调用第 6 轮 reviewer，审核耗时 326.4 秒并提出四项运维文档缺口。该轮 worker 与 reviewer 结论均已进入目标项目 Git 历史。部分审计项关闭证据被 watcher 拒绝，因此累计审核间隔未重置，后续仍需再次审核，不能将模型自述“已关闭”当作系统已经验收。

随后该运行以 `budget_exceeded` 结束，记录的累计运行时长为 7200.064 秒、原上限为 7200 秒。本次配置核对及页面修订未调整该预算。
