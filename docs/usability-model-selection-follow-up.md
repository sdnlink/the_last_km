# 执行器与模型选择续作审查（M-AGENTS）

本检查点继续人的 R1（功能、配置及输出易用性）和 R2（现有功能问题修复），只提交 M-AGENTS 供独立 reviewer 核验，不构成整轮交付结论。当前基线为 `96619f1b97138212d81627da764c4067fc0ca9c0` 加工作区改动。前序运行 `198e3dca7c784819a349069863a578d2` 因外部业务 HEAD 变化失败；旧记录、检查点及版本绑定保持原样。历史文档中的通过记录和模型数量不是本次证据。

## 发现、复现与最小修复

| 需求／功能与源码 | 预期行为 | 当前检查及处理 |
| --- | --- | --- |
| R1：角色间独立选择；`console/static/dialogs.js:modelChoices/bindModels` | 改变 reviewer 的执行器或后端不能清除 worker 自定义模型，反向亦然；改变本角色后失效的模型回到默认 | 新建窗口选择 worker 自定义 ID，再将 reviewer 切到 Paseo，基线会清空 worker 选择并隐藏输入。事件监听现只重建所属角色的模型选项；本角色执行器及 Paseo 后端变化仍重置无效模型。相同修复用于临时角色窗口 |
| R1：目录刷新竞态；同文件 `bind('create')` | 等待响应时的编辑仍有效；两个角色的执行器、后端、模型匹配 | 基线请求前捕获表单，响应后恢复旧执行器；请求中将 worker 改成 Paseo/Grok 会被恢复成 Codex。现在 POST 刷新和配置 GET 返回后才读取当前角色与模型，再同步重建目录；后端和自定义输入保留当前值。不重建需求、完成标准或验收选项 |
| R1：关闭后重开；`openCreate` 与 create close 监听 | 旧刷新结果不写入新窗口 | 基线旧请求会覆盖重新打开后的角色和目录。现在按窗口打开/关闭的序号丢弃旧成功或失败响应，并在关闭时释放刷新按钮；同一次窗口内重复点击不重复发起刷新 |
| R1：角色窗口目录；`openRoles` | 列表与该角色窗口当前配置一致 | 原监听优先使用新建窗口的配置，可能是旧目录。角色窗口现单独保存当前目录；事件测试验证新出现模型可选、已保存但未发现模型保留，并验证双向自定义选择互不清除 |
| R1：刷新说明与错误 | 区分发现、鉴权、实际业务验证，查询失败可重试 | 刷新后继续显示目录来源说明及“不等同于账户鉴权或功能验收通过”。空目录和未安装 CLI 时保留所选模型、默认/自定义入口及保存待启动提示；查询失败保留编辑，按钮可重试，实际重试成功路径已验证 |
| R1/R2：持久化及真实 CLI 参数；`console/tasks.py:create/_new_args`、`auto_moniter/supervisor.py:_reviewer_model` | worker/reviewer 模型及 Paseo 后端独立进入配置；仅同执行器及后端继承未指定的 reviewer 模型 | 阅读源码并运行既有 Python 用例，未发现需要重写的接线。`test_separate_worker_and_reviewer_backends_reach_real_parser` 使用 TaskStore 和真实 CLI parser；`test_different_paseo_backends_do_not_share_model_namespace` 验证不同后端不串模型、同后端继承及显式 reviewer 模型优先。新增事件测试从刷新中编辑一直验证到实际创建处理器发出的 API 请求体 |
| R1/R2：目录缺失、查询失败与隐私；`console/agent_catalog.py:discover/model_rows` | 发现不冒充模型调用；失败保留缓存，不透传凭据 | 既有测试覆盖缺失/损坏缓存、命令超时、后端可用性、字段白名单和本地缓存回退。保持这些正确实现；Paseo reviewer 的 Claude plan 和只读隔离限制未改动，既有角色调整/边界测试通过 |

## 当前候选的验证

新增回归仅写入新文件 `tests/test_agent_model_events.mjs`。它模拟浏览器 select 重建时的值重置、真实事件监听和延迟 fetch，调用实际 dialogs/core 代码；不启动模型或外部服务，不修改既有测试。

1. 初次在基线运行新增的前六项事件用例：退出 1，2 通过、4 失败，确认上述角色清值、旧快照覆盖、窗口旧响应和刷新说明缺失。
2. 修复后的定向命令 `/home/skn/.local/bin/node --test tests/test_agent_model_events.mjs`：退出 0，8 通过、0 失败。覆盖 POST 及后续配置 GET 等待期间的编辑、原生/Paseo 后端命名空间、自定义/目录外模型、HTML 转义、空目录、缺 CLI、失败后实际重试、关闭重开、角色窗口及创建请求体。
3. 原样固定命令 `/home/skn/.local/bin/node --test tests/test_agent_model_choices.mjs tests/test_console_requests.mjs tests/test_review_only_boundary.mjs`：退出 0，17 通过、0 失败。保留原有静态选项、请求取消/超时、语义审核及固定模块命令边界。
4. `/home/skn/code/auto_moniter/.venv/bin/python -m unittest tests.test_agent_catalog tests.test_role_changes -q`：退出 0，18 tests，0.668 秒，OK。使用本地夹具核对目录、配置、parser、角色切换授权及兼容限制。
5. `git diff --check`：退出 0，无输出。

Node 输出已有 `MODULE_TYPELESS_PACKAGE_JSON` 提示，来源是上级 `/home/skn/package.json` 的模块类型配置；不影响退出码，本轮未修改该文件或安装依赖。本轮未做真实浏览器操作、账户鉴权或付费模型调用，不用离线夹具推定这些能力已验证。

本检查点尚不代表 M-CREATE、M-CLI、M-ACCEPTANCE、M-NAV-OUTPUT、M-LIFECYCLE、M-DIRECTIONS、M-REVIEW-GATES、M-STOP-PUBLISH 或 M-INTEGRATION 通过；全部仍须当前版本独立核验。Python 全量固定命令尚未在本检查点执行，应在最终候选集成时与另外两条固定命令原样执行。只有独立 reviewer 接受全部原需求及输出可读性，且 watcher 的最终检查和本地提交门禁通过后，才能结束整轮。worker 未暂存、提交或修改 Git refs。
