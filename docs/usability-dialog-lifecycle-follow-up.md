# 弹窗异步生命周期修复（AUD-1、AUD-2）

本检查点延续 M-AGENTS 的独立审查反馈，同时涉及 R1 的 M-NAV-OUTPUT、M-CREATE 交互边界。保留上一检查点的角色独立模型选择、目录刷新修复和验证证据，未重写历史审查记录。当前 HEAD 仍为 `96619f1b97138212d81627da764c4067fc0ca9c0`；受测 `console/static/dialogs.js` 的 Git blob 为 `b6b987275f3267c3e4b0a1f666e0e71b4e047296`。这不是审核通过或提交记录。

| 问题／需求 | 实际复现与修复 | 当前证据 |
| --- | --- | --- |
| AUD-1：角色、恢复、删除弹窗的读取串任务；R1／M-AGENTS、M-NAV-OUTPUT | 延迟 A 的详情 GET，先打开 B 并编辑，再返回 A，原实现会恢复 A 的目标或表单；旧错误也会弹出。`confirmDelete/openResume/openRoles` 现各有窗口序号，每次打开立即使旧目标失效，在每个 await 后核对序号；关闭使未完成读取失效。只有当前读取可以填写字段、设置提交目标、显示错误或打开窗口 | 新增 `tests/test_dialog_lifecycle.mjs` 的三组读取用例：修复前定向运行9项全部失败，退出1；修复后乱序读取、旧错误、读取期间关闭均通过。实际提交 URL 保持为 B，删除确认 body 同样保持 B |
| AUD-2：旧提交结果作用于新窗口；R1／M-AGENTS、M-NAV-OUTPUT、M-CREATE | 发送 A 的 POST 后关闭窗口，打开并提交 B，再返回 A 的成功或失败。角色/恢复/删除按窗口序号和任务 ID 校验关闭、错误及跳转；创建按窗口序号校验跳转及错误。旧结果仅通过标有原任务 ID／名称的全局消息反馈，不关闭新窗口，不丢弃新草稿，不改写 B 的错误及等待按钮。删除只有当前确认窗口成功时才允许原有详情跳转 | 每个操作均覆盖迟到成功和失败，并断言 B 仍打开、编辑保留、无错误串入、无旧导航、B 的待处理按钮不被 A 的 finally 解锁。删除用例额外将底层页面设为 A 的详情，确保旧成功也不能借原详情跳转丢弃 B |
| 同任务重复提交与重开；R1／M-AGENTS、M-CREATE | 三类任务操作的等待集合按任务 ID 保留，窗口关闭不冒充请求取消；重新打开同一任务仍不能重复发送。原请求结束后释放该任务按钮，但不关闭后来打开的窗口。新建按当前窗口阻止重复发送；新窗口作为独立草稿处理 | 重复触发真实监听、关闭重开后再次触发，断言相同任务仅1个 POST。角色、恢复、删除及新建的当前失败均保留输入、显示当前错误并允许实际成功重试 |
| 创建前的异步扫描／预检；R1／M-CREATE、M-NAV-OUTPUT | 关闭使扫描及预检结果失效；提交等待自动扫描后先检查窗口身份，防止旧草稿继续创建。结果与窗口及扫描上下文绑定，按钮与各自请求绑定；修改目录导致旧结果失效仍可重新扫描 | 关闭后旧扫描不能发出创建 POST；扫描时修改目录不展示旧结果并释放扫描按钮；旧预检失败及 finally 不覆盖或解锁新窗口正在执行的预检。仍明确显示“尚未执行业务检查” |

## 实际验证

- `/home/skn/.local/bin/node --test --test-name-pattern='late task read|late read errors|closing during read' tests/test_dialog_lifecycle.mjs`：修复前退出1，9项失败，直接确认 AUD-1 的源码推断。此前一次完整故障复现已输出5个失败，但因旧代码没有重复提交保护，后续重复提交夹具等待未完成，已结束该测试进程；不将这次中断记作完整检查结果。
- `/home/skn/.local/bin/node --test tests/test_agent_model_events.mjs tests/test_dialog_lifecycle.mjs`：修复后退出0，38项通过、0失败；其中上一检查点模型事件8项保持通过，新生命周期30项通过。
- `/home/skn/.local/bin/node --test tests/test_agent_model_choices.mjs tests/test_console_requests.mjs tests/test_review_only_boundary.mjs`：退出0，17项通过、0失败。原有模型、请求超时/取消、review_only/planning 和固定模块命令边界保持通过。
- `git diff --check`：退出0，无输出。

新增生命周期测试调用真实 dialogs/core 处理器，通过本地 DOM/fetch 夹具控制请求顺序；未启动真实模型或进行浏览器人工操作。Node 仍输出上级 package.json 的已有模块类型提示。本次没有修改 Python 运行行为，未重复全量发现；最终候选仍须原样执行三条固定检查，并由 watcher 执行交付门禁。

worker 只实现修复，不关闭 AUD ID。AUD-1、AUD-2 仍待独立 reviewer 核验后决定关闭。M-AGENTS 尚无联合验收；M-CREATE、M-CLI、M-ACCEPTANCE、M-NAV-OUTPUT、M-LIFECYCLE、M-DIRECTIONS、M-REVIEW-GATES、M-STOP-PUBLISH、M-INTEGRATION 的其余范围仍须逐项核验。没有把单个模块当作整轮交付，未暂存、提交、修改 Git refs、安装依赖或操作旧运行记录；既有测试和上一检查点模型事件测试均保持不变。
