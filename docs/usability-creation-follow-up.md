# M-CREATE 当前版本复核

本检查点覆盖人的 R1（创建流程、配置及说明易用性）和 R2（校验、接线与失败路径），提交 M-CREATE 供独立 reviewer 验收。HEAD 仍为 `96619f1b97138212d81627da764c4067fc0ca9c0`；已有创建校验未重写。前序运行因业务 HEAD 外部变化失败的记录和版本绑定保持原样，不作为当前交付证据。

## 需求与当前源码、验证的对应

| 功能／源码符号 | 预期行为与当前审查结论 | 实际验证 |
| --- | --- | --- |
| 内容和项目目录；`console/tasks.py:TaskStore.create` | title/requirement 必填并规范化。显式空白 cwd、缺失或不可写目录、文件路径、NUL、过长路径、符号链接环均拒绝，提示可操作；省略 cwd 的旧 API 保持控制台默认项目。失败前不保存或启动 | `tests/test_task_creation_validation.py` 覆盖这些边界，断言 `_save/_launch` 未调用且任务与运行目录为空；真实 HTTP 非法 JSON/数组/目录/角色请求返回400 |
| JSON、类型与范围；`console/settings.py:settings`、`TaskStore.create` | modules 必须为数组，quality_policy 由原策略校验器检查形状和字段；不将字符串、布尔、浮点、负数或越界数字隐式转成合法配置。check_timeout 大于0；保护路径须存在于项目内，链接解析失败返回错误 | 既有结构错误、数字边界、保护路径用例通过；未修改策略或既有受保护断言 |
| 独立角色、模型、后端与隔离；`create/settings/_new_args` | 角色/后端先规范化再校验保存；两个角色模型及后端分别传入真实 CLI parser。Paseo strict 组合、非 Claude plan reviewer、无 strict worker 的保护路径均按原边界拒绝；strict worker 在保存前要求运行目录位于项目外 | 既有角色规范化、独立模型接线、strict worker、保护路径及隔离组合用例通过。模型交互与弹窗生命周期的其他证据见前两份续作记录，不能据本文件推定其已独立验收 |
| 默认值、单位和0；`create.html` → `create/_new_args` → CLI parser | Web 默认120分钟、100次调用、轮数上限0、每5轮审核、单轮/空闲/检查超时1800/900/600秒、保留50轮。分钟转秒且保留0；超时0与容量0关闭对应限制，保留轮数0不按轮数裁剪，check_timeout 不可为0 | 既有0值、上限、持久化恢复读取和真实 parser 用例通过；审核频率0/1/2/5分别保存和传入参数，未改变运行策略 |
| 完成标准和验收配置；`TaskStore.create/_new_args`、`console/acceptance.py:select` | 新表单的文字标准通过 acceptance_plan.criteria 保存为 acceptance，再进入 --accept；所选检查由服务端重建并传入 --check。省略引导验收的旧 API 保持兼容，不能用旧 API 默认值描述新页面的必填条件 | `test_guided_selection_persists_purpose_and_actual_cli_acceptance` 核对持久化及真实 parser；`test_guided_task_runs_actual_cli_and_persists_check_snapshot` 实际运行本地 CLI 夹具并保存检查快照 |
| 保存待启动及立即启动；`create/_check_start/_launch` | 默认旧 API 保存待启动；Web auto_start 明确发送布尔。启动预检失败不登记半创建任务；合法保存后可启动，实际执行继续使用既有独立会话流程 | 既有保存/重新读取/取消、缺执行器不留半任务、真实 CLI 启动与控制测试通过；CLI夹具不调用付费模型 |
| 真实启动失败；`_launch/_wait` 与 `console/diagnostics.py` | 校验通过后的启动失败和输入校验失败分开。保留真实失败登记、启动配置、日志和非零退出码，不把最后模型结论当作交付 | 新增 `tests/test_creation_launch_outcomes.py`：Popen OSError 保留failed登记及原需求/launch.json；非Git项目的实际 CLI required-Git预检退出后，详情显示失败与Git原始原因、日志存在、模型调用标记不存在；测试回收自身子进程 |
| 审核节奏及提交说明；表单、CLI帮助、README、CONFIGURATION_AUDIT、WORKFLOW | 基线已正确区分默认累计5轮审核、完成/阻塞立即送审、业务分支提交与隐藏审计引用。新发现表单和CLI帮助仍将0表述为“仅完成/阻塞审核”，与质量检查点提前审核不符；现统一为关闭按轮次审核，仍保留完成/阻塞及质量策略检查点，并同步两个README。只修改说明 | 对照 `Supervisor.run` 的 periodic_review 及质量阈值逻辑；既有 `test_changed_file_threshold_forces_audit_before_normal_round_frequency` 通过；实际 `run --help` 已显示质量检查点说明。`docs/cli-reference.md` 中已有对应例外说明，无需重复修改 |

## 本检查点的实际结果

- `/home/skn/code/auto_moniter/.venv/bin/python -m unittest tests.test_task_creation_validation -q`：退出0，12项通过，0.695秒。
- `/home/skn/code/auto_moniter/.venv/bin/python -m unittest tests.test_console tests.test_console_pages -q`：退出0，14项通过，5.538秒；含实际本地CLI控制流程、服务端页面及API边界回归。
- `/home/skn/code/auto_moniter/.venv/bin/python -m unittest tests.test_creation_launch_outcomes tests.test_acceptance_guide.AcceptanceGuideTests.test_guided_selection_persists_purpose_and_actual_cli_acceptance tests.test_acceptance_guide.AcceptanceGuideTests.test_guided_task_runs_actual_cli_and_persists_check_snapshot -q`：退出0，4项通过，1.368秒。新增测试初次误用不存在的 failure.message 字段，首次命令退出1；对照真实公开契约改为 reason/raw_summary 后通过，没有为测试更改业务实现。
- `/home/skn/code/auto_moniter/.venv/bin/python -m unittest tests.test_quality_policy.QualityPolicyTests.test_changed_file_threshold_forces_audit_before_normal_round_frequency -q`：退出0，1项通过，0.169秒。
- `/home/skn/code/auto_moniter/.venv/bin/python -m auto_moniter run --help`：退出0，实际帮助说明0只关闭按轮次审核，保留质量检查点。
- `git diff --check`：退出0，无输出。

未修改的 `console/tasks.py` blob 为 `6c0d4398f12612964b91a672d782f18e7c50284c`，`console/settings.py` 为 `f620507d252d401a7487effd906728450eff5c40`。本检查点表单 blob 为 `297445179e098713be8fcbbe5db20562ef363f5e`，CLI blob 为 `653c53e44557ed87dd4748a3fd25e7882f208a7b`（后者仅帮助文本变化）。

本次未运行Python全量固定检查，也未因文案改动重复执行无输入变化的Node套件；已有Node证据见 `usability-dialog-lifecycle-follow-up.md`，最终候选仍须原样执行三条固定命令。未验证真实账户鉴权、外部模型或浏览器人工操作。M-CREATE 的 worker 复核不代表独立审核通过；M-AGENTS 的 AUD-1、AUD-2 修复仍待 reviewer 决定关闭，其余 M-CLI、M-ACCEPTANCE、M-NAV-OUTPUT、M-LIFECYCLE、M-DIRECTIONS、M-REVIEW-GATES、M-STOP-PUBLISH、M-INTEGRATION 全部保留待验收。worker 未暂存、提交或修改Git引用。
