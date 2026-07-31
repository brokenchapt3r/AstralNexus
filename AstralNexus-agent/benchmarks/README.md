# AstralNexus 基准测试

## 概述

AstralNexus 基准测试用于评估系统在各种任务中的性能和效果。

## 基准测试列表

1. **software_engineering** - 软件工程自动化构建基准测试
2. **research_report** - 跨域资料研究与决策报告基准测试
3. **fault_injection** - 故障注入与自恢复基准测试

## 运行基准测试

使用以下命令运行基准测试：

```bash
python -m AstralNexus eval --benchmark <benchmark-name>
```

例如：

```bash
python -m AstralNexus eval --benchmark software_engineering
```

## 基准测试结构

每个基准测试都包含以下文件：

- `README.md` - 基准测试说明
- `test_cases/` - 测试用例
- `evaluation.py` - 评估脚本
- `results/` - 测试结果

## 添加新基准测试

要添加新的基准测试，请按照以下步骤操作：

1. 在 `benchmarks/` 目录下创建新的子目录
2. 创建 `README.md` 文件描述基准测试
3. 添加测试用例和评估脚本
4. 在主评估系统中注册新的基准测试