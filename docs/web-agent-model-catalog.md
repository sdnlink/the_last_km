# Web 执行器与模型目录

新建任务直接提供两列配置：Worker 执行器、Worker 模型，以及 Reviewer 执行器、Reviewer 模型。使用 Paseo 时显示各角色独立的后端下拉框。切换执行器或后端时更新模型列表；模型 ID 不跨后端自动沿用。需要使用未枚举模型时，选择“自定义模型 ID”，明确填写后再提交。

控制台启动和“重新枚举执行器与模型”执行同一目录查询。刷新不会重置任务名称、需求、验收方案和已选模型。临时角色窗口同样使用模型列表，并保留已保存但当前目录没有的 ID；现有临时角色切换仍限制为原生执行器。

| 目录 | 来源 |
| --- | --- |
| 本机执行器 | 受支持 CLI 的可执行文件发现 |
| Paseo 后端及模型 | `paseo provider ls --json`、`paseo provider models <id> --json` |
| Grok 模型 | `grok models`；失败时保留本机缓存 |
| Codex 模型 | 本机模型缓存与 Paseo provider 目录 |
| Claude 模型 | 本机指定的模型配置与 Paseo provider 目录 |
| Antigravity 模型 | 已可用的 Paseo provider 目录 |
| Cursor 模型 | `cursor-agent models` |

Gemini 或其他未取得模型列表的执行器保留默认模型和自定义入口，不用硬编码的模型名冒充账户可用目录。可执行文件存在、daemon 后端可用、模型目录存在和账户真正允许推理是不同状态；页面仅声明实际枚举结果，不自动进行付费推理认证。

每条命令有 4 秒超时，复用现有受限输出及子进程清理。先枚举后端，再并发查询可用后端模型。缓存文件读取限制为 2 MiB，模型输出只包含经过筛选的 ID、显示名称和来源；不透传身份、API Key、认证头或服务端点。API 的刷新入口受既有 Host、Origin 和 CSRF 校验保护。

配置通过 `/api/config` 的 `agent_catalog` 提供，刷新使用 `POST /api/agents/refresh`。任务字段 `provider`、`reviewer`、`model`、`reviewer_model` 保持原契约，新增 `reviewer_paseo_backend` 明确区分两个角色。恢复配置携带相同的 CLI 参数。Paseo reviewer 仍只允许 Claude plan，并受原有隔离规则约束；不同 Paseo 后端的 reviewer 默认模型不会错误沿用 worker 模型。

本机启动实测：Claude 17、Codex 2、Grok 14、Antigravity 13 个模型；Cursor/Gemini 未取得模型目录。列表可能随安装、配置、账户和服务变化，以上数量只是本次观测。

验证：Python 全量 617 项通过（106.291 秒）；三份 Node 测试通过；真实 Chrome 验证新建表单下拉、Paseo/Grok 模型联动、刷新保留内容、原生临时角色回显及自定义模型保存，无浏览器异常。测试任务设为不启动并清理，未调用付费模型。自动化入口：`tests/test_agent_catalog.py`、`tests/test_agent_model_choices.mjs`。最终布局调整后重新执行三份 Node 测试及浏览器验证。
