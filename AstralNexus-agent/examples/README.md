# AstralNexus 示例

## 概述

AstralNexus 示例展示了如何使用系统执行各种任务。

## 示例列表

1. **tasks/** - 任务示例
2. **configs/** - 配置示例

## 任务示例

任务示例展示了不同类型的任务描述：

1. **software_fix.md** - 软件修复任务
2. **research_analysis.md** - 研究分析任务
3. **report_generation.md** - 报告生成任务

## 配置示例

配置示例展示了不同场景下的配置文件：

1. **local_only.yaml** - 仅使用本地资源
2. **local_cloud.yaml** - 混合本地和云端资源
3. **cloud_only.yaml** - 仅使用云端资源

## 使用示例

运行示例任务：

```bash
python -m AstralNexus run --task examples/tasks/software_fix.md --config examples/configs/local_cloud.yaml
```