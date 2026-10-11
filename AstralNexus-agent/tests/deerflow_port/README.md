# 可执行的 DeerFlow 移植检查

来源：deer-flow revision `2814bd5d498a`。本目录保留真实执行断言的 5 个文件；兼容入口只有 deerflow_compat.py。没有 sys.path/sys.modules 注入，也不导入 DeerFlow 的生产模块来冒充 AstralNexus 实现。

| 文件 | 作用 | 整理基线 |
|---|---|---|
| test_deerflow_acceptance_selection_scope.py | 测试选择范围与 shell 证据绑定 | 9 passed / 12 failed |
| test_token_usage.py | 消息 token metadata 对照 | 4 passed / 10 skipped |
| test_subagent_status_contract.py | 子代理状态契约存在性 | 1 failed / 28 skipped |
| test_bench_tool_result_probe_cleanup.py | 上游探针清理隔离 | 2 failed |
| test_sandbox_network_proxy_workflow.py | 上游代理工作流权限 | 2 failed |

失败保留为失败。后两项依赖上游脚本/工作流，不能仅凭缺文件认定 AstralNexus 生产缺陷；需先明确等价实现和适配目标，不得复制空文件让断言变绿。

180 个全跳过文件及 125 份原始契约已移至 `03_验证脚本/上游测试契约/deerflow/`，以 `.py.txt` 保存，不再作为已移植测试计数。跨框架行为已正确适配的原生用例仍在父目录 test_upstream_*.py。

运行：在项目根用 Anaconda Python 执行 `python -m pytest 01_源码/AstralNexus/tests/deerflow_port -q`，设置 PYTHONPATH=01_源码、PYTEST_DISABLE_PLUGIN_AUTOLOAD=1。本批记录在 b118_test_organization。

## B119 修复后的范围

上述表格保留整理时基线。命令选择检查已修复；零测试与 unknown 的断言使用我方三态语义。探针绑定我方真实外置模块；工作流检查绑定实际 CI/发布，不再要求不存在的 DeerFlow 镜像；状态契约校验我方枚举与终结行为。原断言在 b119_failed_tests/*.before.txt 保留。当前定向 36 passed / 10 skipped，10 个跳过仍是未支持的 DeerFlowClient API。
