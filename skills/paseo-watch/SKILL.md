---
name: paseo-watch
description: Inspect Auto Monitor tasks on a Paseo schedule or heartbeat, verify real delivery, and diagnose stalled checks, reviews, and recovery.
---

# 巡检 Auto Monitor

在 Auto Monitor 源码目录执行 `auto-moniter watch --console-port 8000`。源码安装可使用 `.venv/bin/python -m auto_moniter`。端口以当前控制台配置为准。

1. 读取任务清单，记录任务 ID、工作区、阶段、轮次、模块进度和累计预算。优先检查验收失败、心跳异常、恢复等待以及多轮没有可验证进展的任务。
2. 按需读取 `/api/tasks/<id>`，核对运行目录的 `state.json`、`events.jsonl` 或保留的报告和检查日志。记录最近业务提交、当前候选、AUD 和通过或失败的实际检查。通过 Git 验证提交已进目标分支，通过回执验证检查属于同一候选。一次定向测试通过、Worker 自述完成和 Reviewer 的局部认可都不足以证明最终交付。
3. 对比前次巡检。同一候选和同一失败反复出现时，定位具体检查、版本变化或派工条件。确认是 Auto Monitor 的可复现缺陷后，在原授权范围内修复。先核对工作区锁及暂停、停止、删除记录。别的项目由其自己的任务继续执行，巡视只读。
4. 将重要变化与证据追加到项目现有的本地巡检记录，简短报告新交付、风险或确切阻塞。已删除任务保留为历史状态，日志缺失如实记录。退出时等待 Paseo 下一次触发。

保持单写入和累计预算。候选交付继续使用独立 Reviewer 与原固定检查，由 Watcher 在批准后自动本地提交。保留检查回执与 Git 历史，不推送远程，不通过重派任务绕过停机状态或预算。
