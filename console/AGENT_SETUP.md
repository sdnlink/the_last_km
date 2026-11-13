# Agent 安装与配置

未完成初始化时，访问首页 `/` 会打开 `/setup`；左侧“工作概览”始终打开 `/overview`，不受初始化状态影响。勾选要使用的 Agent，点击“安装选中项”，再填写默认模型与认证，点击“保存并进入控制台”。之后可随时从左侧“Agent 管理”返回修改。已有任务仍可从原地址访问。

安装仅由按钮触发，不会因打开页面自动下载。检测到已有 CLI 时直接复用，保留现有安装和登录。登录型执行器需要在本机终端按各自官方说明完成登录；页面的“检测 CLI”只执行版本查询。

| Agent | 安装来源 | 可用角色 |
| --- | --- | --- |
| [Codex](https://developers.openai.com/codex/cli/) | npm `@openai/codex` | worker / reviewer |
| [Grok](https://docs.x.ai/build/overview) | 官方 `https://x.ai/cli/install.sh` | worker / reviewer |
| [Claude Code](https://code.claude.com/docs/en/setup) | npm `@anthropic-ai/claude-code` | worker / reviewer |
| [Antigravity CLI](https://antigravity.google/docs/getting-started?tab=cli) | 官方 `https://antigravity.google/cli/install.sh`，命令 `agy` | worker / reviewer |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | npm `@google/gemini-cli` | worker / reviewer |
| [Copilot CLI](https://github.com/github/copilot-cli) | npm `@github/copilot` | worker |
| [Hermes](https://hermes-agent.nousresearch.com/docs/getting-started/installation) | 官方 `https://hermes-agent.nousresearch.com/install.sh`，跳过交互式配置 | worker |
| [Kimi Code CLI](https://github.com/MoonshotAI/kimi-code) | npm `@moonshot-ai/kimi-code` | worker |
| [CodeBuddy](https://www.codebuddy.ai/docs/cli/installation) | npm `@tencent-ai/codebuddy-code` | worker / reviewer |
| [GLM](https://docs.z.ai/devpack/tool/claude) | 复用或安装 Claude Code，使用 GLM 的 Anthropic 兼容接口 | worker / reviewer |
| [DeepSeek](https://api-docs.deepseek.com/guides/anthropic_api) | 复用或安装 Claude Code，使用 DeepSeek 的 Anthropic 兼容接口 | worker / reviewer |

保留已有 Cursor Agent、Paseo 选项，可从官方链接手动安装后检测。Copilot、Hermes、Kimi 的当前适配器不提供独立只读审核模式，选择这些 worker 时，需要搭配支持只读模式的 reviewer；界面会禁用不支持的 reviewer 选项，服务端也会拒绝。它们和 CodeBuddy 每轮启动新 CLI 会话，监督器继续提供任务及上一轮反馈。

npm 安装需要本机已有 Node.js 和 npm，安装位置为控制台数据目录下的 `agent-tools/npm`，不要求写入系统全局目录。安装程序会检查 npm 包的 Node.js 版本要求；例如当前 Kimi Code CLI 要求 Node.js 24.15.0 或更高版本。Grok、Hermes、Antigravity 使用各自官方脚本的默认用户目录。安装失败会保留输出，可修复网络或依赖后重试，或填写已安装 CLI 的绝对路径。安装进度只在当前控制台进程中保留。

## 配置与认证

- **启用**：选中的 Agent 在新建任务和角色选择中可用。未安装的选项会标明状态，启动时会再次检测。
- **默认模型**：留空沿用 CLI 默认值。填写后会加入任务表单的模型选项，新任务默认使用它；任务中仍可单独选择模型。
- **服务地址**：只在已接入对应配置能力的 Agent 上显示。留空沿用本机配置，GLM、DeepSeek 使用卡片中的默认兼容接口地址。Codex 自定义地址使用 Responses 协议。
- **认证方式**：沿用本机 CLI 登录，或输入 API Key / Token。密钥保存后只显示“已保存”，留空保存会保留旧值；点击“清除已保存密钥”后保存可删除。
- **可执行文件位置**：通常留空自动检测，也可以填写绝对路径。检测范围包括 PATH、控制台 npm 安装目录及已知的用户安装位置。

GLM、DeepSeek 必须分别配置对应服务的 API Key，避免意外使用 Claude 的登录账号。默认地址分别为 `https://api.z.ai/api/anthropic` 与 `https://api.deepseek.com/anthropic`，模型 ID 以各自服务实际支持的值为准。

Kimi 的 API Key 模式需要填写模型 ID；“接口类型”支持 `kimi`、`openai`、`anthropic`，默认使用 Kimi 原生接口，自定义接口需填写对应服务地址。运行时使用官方 `KIMI_MODEL_*` 临时配置，任务指定的模型也会应用到临时配置，不改写已有 Kimi 配置文件。参考 [Kimi 环境变量说明](https://moonshotai.github.io/kimi-code/en/configuration/env-vars.html)。

配置保存在 `<data-dir>/agents.json`，权限为 `0600`。模型进程启动前按所选 Agent 注入环境变量，API Key 不进入命令参数，worker 与 reviewer 分别使用各自的配置，控制台 API 不返回密钥。安装和版本检测不验证账户额度或实际模型调用能力。

控制台启动的任务会自动加载配置。如需在命令行使用同一份配置，可显式设置 `AUTO_MONITER_AGENT_SETTINGS` 为该文件的绝对路径，再运行 `python3 -m auto_moniter run ...`。未指定该变量的既有 CLI 用法继续沿用本机配置。
