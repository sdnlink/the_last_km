# 项目代码审查：2026-11-18

审查版本：`72ee46f`（`fix: harden reviewed task publication and recovery`）。本轮重点检查监督状态机、函数与模块验收、缓存审核、控制台启动事务、恢复以及 CI；不是完整安全审计。以下问题尚未修复。

## P1：模块级交付可以进入无法推进的依赖确认循环

位置：`auto_moniter/supervisor.py:2836`、`:1141`、`:1203`、`:1259`；`auto_moniter/function_tasks.py:273`；`auto_moniter/modules.py:113`。

触发：模块 B 依赖 A。A 的实现函数 F1 已经通过函数级验收，但模块 A 仍为 pending。计划校验允许派发 B 的模块级交付 F2，因为 `_validate_function_plan` 只检查条款覆盖，没有检查模块级依赖状态。当前模块级报告契约又只允许报告 B，因此 worker 与 reviewer 无法通过报告同时确认 A。

进一步的问题是，函数评估路径在通用模块依赖检查前已保存 `candidate_approval`。后续依赖检查拒绝 B，却没有将这份批准作废或直接结束该候选；`_pending_review` 和 `_invoke_review` 随后不断复用它，同一轮既不实现新代码，也不取得新审阅。`max_rounds` 不能限制这种同轮复用。

隔离复现使用 2 秒运行预算：worker 调用 2 次、reviewer 调用 4 次，之后又复用审核 3 次，最终 `budget_exceeded`，A、B 均未接受。移除预算后的诊断运行没有自然退出，已手动结束该隔离进程。

建议：派发前检查模块依赖，要求先完成 A 的模块验收，或设计有证据约束的联合模块确认；所有通用门禁失败都须作废不再适用的批准，并对同一候选的重复拒绝设置确定的退出/重新规划路径。

## P1：创建进程后写入任务目录失败，没有回收已创建进程

位置：`console/tasks.py:901`–`:916`、`:918`；`auto_moniter/cli.py:401`。

触发：`Popen` 成功后，`_save(meta)` 因磁盘、权限或其他 I/O 问题抛出 `OSError`。异常分支会记录 failed 并返回启动失败，但没有终止/等待已创建的 child，也没有关闭它的 stdin、移除 `_children` 记录或确保等待线程已经启动。

故障注入结果：保存状态为 failed，`child_terminated=false`、`wait_thread_started=false`、`child_retained=true`。由于需求本来由稍后启动的 `_wait` 线程通过 stdin 提供，真实 CLI 可能一直等待输入，造成接口报告失败、后台进程却仍存活的状态。复现使用 FakeChild，没有启动真实 CLI 或模型。

建议：把“创建进程—持久化身份—发送需求—启动回收线程”作为有明确回滚责任的启动事务；任一后续步骤失败时，关闭管道、终止并等待本次创建的进程，再清理内存登记。增加 Popen 后落盘失败及线程启动失败的故障注入测试。

## P2：复用旧缓存审核时，绕过当前函数报告协议校验

位置：`auto_moniter/supervisor.py:4258`–`:4278`，对照同文件 `:4301` 的新报告校验。

触发：旧版缓存的最终审核只确认当前模块 B，当前 worker 报告已补正为 A、B；候选、检查回执、模型与配置没有变化。缓存快路径只验证保存的回执/上下文或基本 accepted 状态，直接返回，未调用 `_function_reviewer_report_errors`。

隔离复现注入一份匹配当前检查凭据的旧协议批准，同时准备了一份正确的 reviewer 输出。结果仍为 `action_required`，提示要求 `module_ids=["A", "B"]`；实际 reviewer 仅调用 2 次，正确的新输出没有被消费。新增的一次只读补正仅覆盖真实模型返回路径，未覆盖缓存快路径。

建议：所有缓存批准返回前也校验当前任务 ID、交付范围和模块契约。不兼容的缓存批准应失效并进入有限只读补正；仍然有效的源码检查回执可保留。可为批准缓存增加协议版本，但不能只靠版本号替代字段校验。

## P2：CI 没有执行全部前端回归测试

位置：`.github/workflows/verify.yml:31`。

CI 显式列出 5 个 `.mjs` 测试文件，仓库目前共有 13 个。遗漏了代理事件、对话框生命周期/遮罩、派单计划、错误展示、本地化、分页和需求队列这 8 组。它们本次本地全部通过，但未来这些区域出现回归时，现有 CI 仍可能为绿色。

建议：将 Node 检查改为执行 `tests/*.mjs`，或维护一个被“测试文件发现检查”验证完整的列表。

## 验证与证据

- `python -m unittest discover -q`：1019 项通过。
- `node --test tests/*.mjs`：13 组通过。
- `git diff --check`：通过。
- [隔离复现脚本](review-evidence/2026-11-18/reproduce.py) 和 [本次观察结果](review-evidence/2026-11-18/observed.json)。脚本使用临时目录和脚本化 Provider；启动异常用 FakeChild 注入，不修改真实任务或源码。复用仓库测试夹具，适用于所审查版本的源码环境。

正常回归全部通过并不覆盖上述额外故障路径。建议先处理两个 P1，再补缓存兼容与 CI 完整性。结构层面，可逐步将目前约 5200 行的 Supervisor 拆分为协议校验、候选验收、发布事务和恢复策略，并统一跨 watcher/guardian/console 的状态定义；这属于后续维护工作，不作为已确认的独立运行缺陷计数。
