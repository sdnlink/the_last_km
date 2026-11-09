# 功能与配置审查记录

本轮原始要求：检查功能和配置是否符合使用习惯，修复不便之处；review 功能层面的问题并修复。结束条件仍是独立 reviewer 认为项目可交付、输出可以让人看懂，并通过原有 watcher 门禁。

本记录从 `506f978a40e5e07a39a3f407a3b8141a7edc50c9` 的干净工作区开始。历史自检文件仅作源码导航，不作为本轮通过证据。以下 M-CREATE 是本次提交复核的范围；其他模块仍待逐项审查，不能由本模块或下面的回归结果推定完成。

## 完整范围与源码对应

| 模块 | 对应要求及预期行为 | 源码入口 | 状态与后续验证 |
| --- | --- | --- | --- |
| M-CREATE | R1：创建需求、表单默认值/单位/0、输入校验、隔离组合、保存和立即启动；配置说明与实际策略一致 | `console/tasks.py:TaskStore.create/_new_args`、`console/settings.py:settings`、`console/static/fragments/create.html`、`console/CONFIGURATION_AUDIT.md`、`WORKFLOW.md` | 本次已修复并检查，待独立复核；明细见下表 |
| M-CLI | R1/R2：启动脚本、CLI 参数/默认值/单位/帮助、status/watch/doctor/selftest/providers 的输出和失败提示；runs/cleanup/history 默认预览、确认及数据保留；部署配置与实现一致 | `start.sh`、`auto_moniter/cli.py:build_parser/main`、`auto_moniter/preflight.py`、`auto_moniter/selftest.py`、`auto_moniter/retention.py`、`auto_moniter/git_maintenance.py`、`deploy/`、`OPERATIONS.md` | 待逐项审查，包含非法参数/目录、缺程序、无数据和预览不写入；外部新增的 CLI 说明也须纳入核对 |
| M-AGENTS | R1：worker/reviewer 独立模型与 Paseo 后端、自定义模型、目录刷新、已有值保留；发现不冒充鉴权 | `console/static/dialogs.js:modelChoices/bindModels`、`console/agent_catalog.py:discover`、`auto_moniter/supervisor.py:_reviewer_model` | 待完成。代码阅读发现切换任一角色时会重建双方模型选项；自定义选择可能被重置。刷新执行器在请求前捕获角色值、返回后重新赋值，可能覆盖请求期间的新选择。需新增事件/延迟响应回归验证并修复 |
| M-ACCEPTANCE | R2：扫描不执行项目代码或安装依赖；预检与测试分开；入口版本、伪造 ID、未选择和三种模式边界；固定模块命令不被静默删除 | `console/acceptance.py:scan/select/preflight`、`console/tasks.py:acceptance_options/create`、`console/static/dialogs.js:acceptancePlan/applyVerificationModeToCreatePayload` | 待逐项审查；既有 `tests/test_acceptance_guide.py` 已在本次相关套件中执行，不等于整个模块验收 |
| M-NAV-OUTPUT | R1/H1：页面直达/刷新/返回、键盘入口、编辑内容与过期请求隔离；当前角色/阶段/结论、日志快照与资源未知值可理解 | `console/static/core.js`、`console/static/pages/*.js`、`console/static/ui.js`、`console/workflow.py`、`console/activity.py`、`console/metrics.py`、`console/conclusions.py` | 待逐项审查和交互/失败路径验证 |
| M-LIFECYCLE | R2：启动、暂停请求/取消、继续、停止、恢复、累计预算和原配置；损坏状态独立可管理；删除确认/持久隐藏/保留数据；工作区互斥及 linked worktree | `console/tasks.py:_check_start/action/_catalog/_view`、`console/recovery.py`、`console/deletion.py`、`auto_moniter/locking.py` | 待逐项审查；本次既有控制台用例覆盖部分真实 CLI 控制及删除流程，不能据此确认所有恢复/损坏/并发路径 |
| M-DIRECTIONS | R2：补充要求的保存/发送/返回/满足分开；不可变验收条件和终态边界；运行中角色变更、原生/Paseo 兼容限制 | `auto_moniter/directions.py`、`auto_moniter/role_changes.py`、`console/roles.py`、`console/tasks.py:add_direction/action`、`console/static/dialogs.js` | 待逐项审查及正常/拒绝/重试路径验证 |
| M-REVIEW-GATES | R2/H1：需求完善只读、非法/needs_input/阻塞不启动 worker；联合完成、模块/需求覆盖、AUD 有效关闭、文件和版本证据；源码或检查副作用使旧证据失效 | `auto_moniter/requirements_review.py`、`auto_moniter/supervisor.py:_elaborate_requirement/_delivery_checks/_finish_locked`、`auto_moniter/modules.py`、`auto_moniter/quality.py`、`auto_moniter/audit.py`、`auto_moniter/health.py` | 待逐项审查，不能把需求 ready、单方 done 或旧报告视为交付 |
| M-STOP-PUBLISH | R2：缺程序/认证/断网/超时/进程残留如实报告；停止/预算耗尽后不新调用、验收或发布；原子提交保留、恢复不重复发布、不覆盖无关暂存 | `auto_moniter/failures.py`、`auto_moniter/processes.py`、`auto_moniter/supervisor.py:_invoke/_publication_boundary`、`auto_moniter/publication.py`、`auto_moniter/work_checkpoint.py` | 待逐项审查及边界故障验证；不修改历史审计，不由 worker 执行发布 |
| M-INTEGRATION | R1/R2/H1：逐项审查记录覆盖完整，修复有重现和当前证据；最终候选执行三条原样固定命令；独立 reviewer 认可可交付和输出可读，watcher 保留最终检查/提交职责 | 本文件、所有上述模块、固定命令输出、独立 review 和 watcher 记录 | 待其他模块通过后验证；真实模型认证和外部服务调用不能由离线夹具推定 |

## M-CREATE 审查结果

| 功能/源码符号 | 预期行为与依据 | 问题及处理 | 验证 |
| --- | --- | --- | --- |
| 项目路径 `TaskStore.create` | 显式空路径不能选择其他项目；省略字段的旧 API 沿用控制台默认项目 | 已复现：`cwd: ' '` 被解析为控制台进程目录，且成功保存。现拒绝空白；路径最多 4000 字符，与扫描入口一致；符号链接环/无法展开目录返回可操作错误 | 新增 `test_explicit_empty_directory_cannot_select_the_server_working_directory`、`test_omitted_directory_preserves_legacy_default_and_saved_task_is_startable`、`test_directory_errors_are_actionable_and_do_not_save_a_task`；真实 HTTP 400 用例 |
| 角色/后端与隔离 `TaskStore.create` → `settings` | 校验、持久化、CLI 使用同一组值 | 已复现：`reviewer: ' paseo '` 绕过原生 strict 限制，保存为 `paseo`。现先规范化角色、后端和模型再校验；合法的 ` claude ` 后端亦能正确处理，worker/reviewer 值分别进入 CLI | 新增 `test_whitespace_cannot_bypass_paseo_isolation_restrictions` 与 `test_normalized_roles_backends_and_independent_models_reach_the_cli`，用真实 parser 核对参数 |
| strict worker 存储 `TaskStore.create/_check_start` | 运行目录必须在项目外，这是既有 strict worker 启动限制 | 原先只在启动时发现。现保存前拒绝已知无效组合并提示外部 `--data-dir`；合法外部数据目录不变 | 新增 `test_strict_worker_requires_external_storage_even_when_saved_without_starting` |
| 保护路径 `settings` | 必须是项目内存在的相对路径；解析错误不打断 HTTP 请求 | 链接环原先抛未处理的 RuntimeError；现给出保护路径校验错误 | 新增 `test_protect_path_loop_returns_a_validation_error`；既有恢复/隔离用例回归 |
| 模块 JSON `settings/ModuleLedger` | 模块清单必须是数组；空数组明确表示没有固定清单 | 新增测试首次运行发现 `null` 和 `{}` 被内部默认值逻辑视作空计划；现在 API 边界拒绝这两类错误形状，不改通用台账兼容逻辑 | 新增 `test_invalid_structured_settings_are_rejected_before_save`；首次测试退出 1（2 个失败），修复后退出 0 |
| 内容、JSON 与数值 `create/settings` | 空需求、无效 JSON/形状、错误类型或越界数字不保存任务、不启动进程 | 其余所列边界未发现需修改的行为：已有类型/范围校验拒绝布尔、字符串、负数、浮点及溢出；保留默认值和上限，不进行隐式数字转换 | 新增结构错误与数字类型/上限用例、HTTP 非法 JSON 用例；失败时断言 `_save`/`_launch` 未调用且目录为空 |
| 表单默认值、0 语义与 CLI `create.html` → `create/_new_args` | 时间单位清楚，0 不能被默认值替换；验收超时须正数 | 保持默认值：120 分钟、100 调用、0 轮次上限、5 轮审核、1800/900/600 秒。补充单轮/空闲超时 0、保留轮数 0 的不同含义，以及输入数值上限；调用上限仍是调用轮次，不假称请求数量 | 新增 `test_zero_values_and_maxima_are_persisted_and_forwarded_without_defaults`；既有 `test_review_frequency_is_validated_saved_and_forwarded_to_cli` 验证 0/1/2/5 |
| 待启动/立即启动 `TaskStore.create/_launch` | 保存后可继续，启动前检查失败不留下误导登记；正常流程保留完成标准与配置 | 未改运行策略；新增验证省略路径的持久化和 start 入口、缺执行器预检不留半任务。已真实启动的任务仍按已有方式记录启动/运行失败，不伪报成功 | 新增自动启动预检失败用例；既有 `test_real_cli_launch_pause_continue_stop_and_resume` 与 `test_guided_task_runs_actual_cli_and_persists_check_snapshot`（本地假执行器，不调用模型） |
| 配置/流程说明 `CONFIGURATION_AUDIT.md`、`WORKFLOW.md` | 说明必须与已有策略相符 | 更正“每轮正常实现送审”为默认累计 5 轮及即时触发例外；更正业务分支与隐藏审计引用职责；移除“独立 reviewer backend/文字标准仅 CLI 支持”的过时描述。依据 `Supervisor.run` 的 periodic_review、`TaskStore._new_args` 和现有 publication 实现；没有为文档改动调度策略 | 直接核对源码和表单；既有审核频率及真实 CLI 回归通过 |

## 当前验证证据

检查后观察到 HEAD 已由外部推进到 `02bb346b75fd2437768d0cc1080821cff2b18d46`，该提交仅修改 `README.md` 并新增 `docs/cli-reference.md`，未改变本次受测运行源码；完整保留这些外部改动。当前候选是该 HEAD 加本次工作区改动。本 worker 未执行暂存、提交或 Git refs 修改。既有受保护测试及断言未修改。检查输出存在本轮工具记录，以下为实际执行摘要：

| 命令 | 退出码/结果 |
| --- | --- |
| `/home/skn/code/auto_moniter/.venv/bin/python -m unittest tests.test_task_creation_validation -q` | 修复模块 JSON 形状后：0，12 tests，OK |
| `/home/skn/code/auto_moniter/.venv/bin/python -m unittest tests.test_console tests.test_console_pages tests.test_agent_catalog tests.test_acceptance_guide tests.test_console_recovery tests.test_console_delete -q` | 0，55 tests，11.932s，OK；其中真实 CLI 使用本地夹具 |
| `/home/skn/.local/bin/node --test tests/test_agent_model_choices.mjs tests/test_console_requests.mjs tests/test_review_only_boundary.mjs` | 0，17 passed，0 failed；Node 提示已有 package.json 模块类型未声明，不影响本次检查 |
| `git diff --check` | 0，无输出；记录完成后再次检查 |

最终交付仍须在最终源码版本执行完整固定命令 `/home/skn/code/auto_moniter/.venv/bin/python -m unittest discover -s tests -q`，以及上表原样 Node 命令和 `git diff --check`。本次是 M-CREATE 检查点，不是整个需求完成或独立 reviewer 审核通过；未验证真实账户鉴权/模型调用，也未进行浏览器人工操作或远程发布。
