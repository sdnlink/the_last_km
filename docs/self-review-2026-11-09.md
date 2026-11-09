# 需求状态 2026-11-09

本清单由 worker 维护。状态只用：待实现、待复核、已验证关闭、阻塞/未完成。
watcher 固定验收和 reviewer 关闭尚未发生，不预填这两项成功。

## REQ-001 / M-DELIVERY-GATE

- 状态：待复核
- 变更文件：`auto_moniter/verification.py`、`auto_moniter/supervisor.py`、`tests/test_module_delivery_gate.py`
- 触发案例：`python3 -c "assert 'ModuleNotFoundError: No module named example'"` 退出 0 时保持 `passed`；失败诊断和超时后的环境缺失仍识别；legacy 模块里程碑不执行最终固定检查。
- 测试命令及结果：`.venv/bin/python -m unittest tests.test_module_delivery_gate tests.test_review_first.ReviewFirstTests.test_git_branch_is_created_only_after_review_and_watcher_checks tests.test_supervision.SupervisionTests.test_multiple_checks_run_and_module_milestone_does_not_trigger_final_checks -q`，退出码 0。
- 证据位置：`tests/test_module_delivery_gate.py` 的成功字面量用例；`auto_moniter/supervisor.py` 的 `_delivery_checks` 只在 `final` 或 `review-first` 时纳入 `config.checks`。
- 剩余限制：未由 reviewer 独立关闭，未由 watcher 执行固定全量验收。

## REQ-002 / M-PUBLICATION-BUDGET

- 状态：待复核
- 变更文件：`auto_moniter/publication.py`、`auto_moniter/supervisor.py`、`tests/test_publication_budget_boundary.py`
- 触发案例：提交入口、候选指纹核对期间、发布准备期间、`update-ref` 前，以及启动检查前写入 `control.json` stop。另覆盖检查通过后进入 `_commit_approved_locked` 才耗尽预算，以及 publish 第二个边界写入 stop。
- 测试命令及结果：`.venv/bin/python -m unittest tests.test_publication_budget_boundary -q`，退出码 0，Ran 27 tests。
- 证据位置：`tests/test_publication_budget_boundary.py` 的 `test_supervisor_boundaries_refuse_before_refs_move` 与 `test_commit_fingerprint_and_prepare_stop_refuse_after_checks_pass`。后一用例断言检查已通过、`refs/heads` 与 `refs/auto-moniter/` 不前进、模块未接受。
- 剩余限制：未由 reviewer 独立关闭 AUD-5，未由 watcher 执行固定全量验收。已完成原子发布的保留断言仍在同一文件。

## REQ-003 / M-STOP-EVIDENCE

- 状态：待复核
- 变更文件：`auto_moniter/supervisor.py`、`tests/test_budget_stop_evidence.py`
- 触发案例：需求 reviewer、worker、实现 reviewer 在返回中断结果前推进受控时钟；Paseo launch 后等待完成、预算截断、后续 transcript 为空，以及停止失败。检查启动前读取 stop 和预算。
- 测试命令及结果：`.venv/bin/python -m unittest tests.test_budget_stop_evidence tests.test_shutdown_failures.ShutdownFailureTests.test_paseo_stop_failure_prevents_next_worker_turn tests.test_delivery.DeliveryTests.test_paseo_agents_close_before_delivery_and_cleanup_failures_are_reported -q`，退出码 0。
- 证据位置：`auto_moniter/supervisor.py` 的 `_wait_for_paseo` 与 `_invoke`；`tests/test_budget_stop_evidence.py` 在 finally 恢复 `_paseo_stop`、`paseo_transcript` 和 `run_command`。
- 剩余限制：未由 reviewer 独立关闭 AUD-2 与 AUD-8，未由 watcher 执行固定全量验收。真实模型认证和真实 Paseo 进程未验证。

## 本轮共同限制

- 三个模块都还不是已验证关闭。
- 固定命令 `.venv/bin/python -m unittest discover -s tests -q`、`node --test tests/test_console_requests.mjs tests/test_review_only_boundary.mjs`、`git diff --check` 仍由 watcher 在交付时执行。
- 本次累计时限仍是 3600 秒，本文件不延长、不重置该上限。
