# 测试入口与目录口径

- 本目录根下 test_*.py：AstralNexus 的原生/已适配行为测试；包括 test_upstream_*.py，断言直接作用于我方接口。
- deerflow_port/：仍存在失败的可执行移植对照，默认 pytest 继续收集；不能把失败当作通过。
- fixtures/、*_support.py：共用数据与辅助方法，不单独计作测试。
- 上游未适配契约：项目根 `03_验证脚本/上游测试契约/deerflow/`，属于资料，不进入默认 pytest 或 Python 类型检查。

使用 `C:/ProgramData/anaconda3/python.exe`，PYTHONPATH 指向 01_源码；当前环境需设置 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1（第三方插件兼容问题）。正常全量命令为 `python -m pytest 01_源码/AstralNexus/tests`。类型检查仍是 `python -I 03_验证脚本/check_mypy.py`，未增加排除或忽略。

2026-10-11 整理索引、原文件哈希和前后结果在 `02_运行产物/capability_migration/b118_test_organization/`。本次只整理新增测试，未修改业务/框架实现。
