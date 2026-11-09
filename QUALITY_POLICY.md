# 验收可信度与有效进展策略

质量策略由监督方提供，worker/reviewer 只能读取。CLI 使用 `--quality-policy /outside/project/quality.json`；Web 新建任务在高级配置中填写 JSON。新页面预填保护测试、验证进展、10 分钟工作检查点和重复阻塞处理。已有任务不自动换策略，恢复从检查点继承且拒绝替换；需要改变原标准时创建明确的后续任务。

## 需求、模块和证据

先提供固定模块，例如 `modules.json`：

```json
[
  {"id":"API","title":"接口能力","acceptance":["合法请求成功，非法请求明确拒绝"],"files":["src/api/*","tests/api/*"],"checks":["python -m unittest discover -s tests/api"]},
  {"id":"UI","title":"界面集成","acceptance":["展示接口成功与失败结果"],"files":["src/ui/*","tests/ui/*"],"depends_on":["API"]}
]
```

再提供质量策略：

```json
{
  "protect_tests": true,
  "validation_paths": ["scripts/check_contracts.py"],
  "verified_progress": true,
  "no_progress_limit": 12,
  "review_interval_s": 600,
  "review_changed_files": 20,
  "blocked_review_limit": 3,
  "review_tools": "provider",
  "max_tokens": 0,
  "max_cost_usd": 0,
  "requirements": [
    {"id":"R-API","text":"接口必须拒绝非法输入","module_ids":["API"],"evidence_paths":["src/api/handler.py","tests/api/test_invalid.py"]},
    {"id":"R-UI","text":"界面必须展示失败状态","module_ids":["UI"],"evidence_paths":["src/ui/*"]}
  ]
}
```

`validation_paths` 必须指向实际存在的项目内文件或目录。`requirements` 的模块必须在固定模块清单中存在。最终 watcher 检查所有条目对应的模块已验证、证据路径存在且被双方引用，或具有仍有效的联合模块验收证据。缺项拒绝结束，并写入带工作树指纹的 `requirement-coverage.json`；详情页将其显示为上次检查结果，不冒充当前交付确认。

结构化清单不会自动证明自由文本需求没有遗漏：监督方仍须确认条目覆盖原始意图。未提供条目时不声称做过逐条需求覆盖认证。

## 防止修改测试制造通过

启用 `protect_tests` 后，监控保存原有测试文件及常见验收配置的哈希；显式验收命令引用的现有文件、`validation_paths` 也纳入基线。只保存哈希，不复制整个项目。

自动识别采用常见测试目录/文件名规则，不解析混在业务源码中的内联测试。非标准测试布局应通过 `validation_paths` 指定独立验收资产；不能把路径规则识别等同于语义上识别所有测试。

- 已有测试/断言被修改、删除或隐藏时，停止并报告 `action_required`，不进入验收。
- 新测试可以新增。严格 worker 模式把原有基线文件逐个挂载为只读，因此仍可在测试目录新增文件。
- provider worker 模式是事后检测；要阻止写入，必须选择 `--worker-isolation strict`，且运行目录在目标项目外。
- 某些配置（如 package.json、pyproject.toml、Makefile）同时包含依赖与验收入口，因此也会冻结。确需调整时，应通过明确的后续任务和新验收基线处理，不能偷偷更新哈希。
- 恢复前再次核对原基线；已修改且未恢复的文件不会被当作新基线接受。

## 进展与审核检查点

`verified_progress` 不再把文件变化当作验证进展。首次通过某条 watcher 检查、首次联合接受模块、首次由 reviewer 关闭某个 AUD 问题才记录进展。相同检查/模块/问题反复通过不会反复刷新进度。无进展达到 `no_progress_limit` 时保存状态，返回 `action_required`；0 关闭该停止门槛。

`review_interval_s` 给 worker 单轮设置时间上限，与原单轮超时取较小者。到期会终止本轮执行，保留文件；若已有改动，则独立审核当前部分成果，不能把超时视为完成。没有改动时按原失败/恢复策略处理。`review_changed_files` 在轮次返回后按改动文件数提前触发审核，不是执行中逐次拦截；0 关闭对应触发。CLI 的原周期审核仍同时生效。

独立审核重复确认相同外部阻塞，且未取得验证进展，达到 `blocked_review_limit` 后停止重复模型调用，记录 `dependency-block.json` 与可操作原因。worker 在此前仍被要求完成其他独立模块。该机制按明确阻塞文本判断，不会凭自然语言猜测缺少批准；改变措辞仍由无进展门槛及总预算兜底。

## 按影响范围回归

只有监督方可在固定模块中填写 `files` 模式；worker 不能自己扩大或缩小影响范围。全部模块都有范围时，改动使匹配模块及其下游依赖失效，未受影响模块保留接受状态。出现未声明文件、缺少范围、恢复后的首轮无法比较时，保守地重新验证。最终仍执行整体检查和模块检查，不跳过集成验收。

审计问题目前继续采用工作树变化后的保守重开规则；模块影响范围优化不意味着业务问题可自动关闭。

## reviewer 工具边界

`review_tools: "provider"` 沿用所选后端只读模式及配置的 OS 隔离。

`review_tools: "files_only"` 目前仅支持原生 Claude 且要求 strict 审核，不允许附加 CLI 覆盖参数。启动命令固定 Read/Glob/Grep，启用 restricted、安全模式和空的严格 MCP 配置，关闭命令执行、自定义扩展及外部工具；剔除常见部署/数据库环境凭据并隐藏云配置、SSH、Kubernetes、Docker 等凭据目录。watcher 独立执行预先指定的验收命令，reviewer 阅读其结果。

该选项是工具能力限制，不是网络地址防火墙；模型服务本身仍需网络和模型凭据。需要外部服务的严格访问隔离时，还须使用只读账号、独立运行身份或部署网络策略。其他后端配置 files_only 会明确拒绝，不冒充已经实现同等边界。

## 用量与预算

`usage.provider_reported` 记录原生后端明确报告的 token、美元费用和未报告轮数。不会从模型结论中的自述计费，也不会按单价猜算费用；缓存 token 不重复计入 Codex input_tokens。后端返回累计费用时保守累加，可能高估，不保证等于账单。

`max_tokens`、`max_cost_usd` 为 0 时只记录已知数据，不启用相应门槛。设定后，在调用返回并报告用量时核对，达到门槛停止后续调用；若没有所需报告则停止，恢复也不会把未知值清零。这不是单次请求的费用硬上限，尚未被后端报告的子任务/内部请求不在已知用量中。原 `max_agent_calls` 仍统计 worker/reviewer 轮次，不能用它代替供应商请求数或费用账单。
