# AstralNexus 命令行接口

## 概述

AstralNexus 命令行接口提供了与系统交互的方式。

## 命令权威

命令名称、别名、分类和帮助文本只在
`AstralNexus.cli.commands.COMMAND_REGISTRY` 注册。运行 `/help` 获取当前
完整列表，Ink TUI、Python 兼容 TUI 与命令分发器均读取同一注册表。
系统命令入口同样复用该注册表；例如 `AstralNexus model list` 与交互式
`/model list` 使用同一处理器。带稳定脚本参数的 `run`、`resume`、`eval`、
`report`、`dashboard`、`stream` 和 `inject-fault` 仍由 argparse 校验参数。

## 使用方法

### 运行任务

```bash
AstralNexus run --task <task-file> --config <config-file>
```

### 运行评估基准

```bash
AstralNexus eval --scenario <scenario-name>
```

### 生成报告

```bash
AstralNexus report --run-id <run-id>
```

### 启动仪表板

```bash
AstralNexus dashboard --run-id <run-id>
```

### 注入故障

```bash
AstralNexus inject-fault --run-id <run-id> --type <fault-type>
```

## 交互式 CLI

无参数启动会进入持续交互循环：

```text
AstralNexus
```

原生 Ink TUI 是首选界面，Python prompt-toolkit TUI 仅作为缺少 Node/构建
产物时的兼容适配器。两者均将 `/run`、`/resume`、`/status`、`/new`、
`/interrupt` 和运行中需求注入委托给唯一的
`_InteractiveRunController`，不会各自创建第二套 Orchestrator。

Ink 本地注册表只保留 `help`、`quit`、`mouse`、`redraw` 等终端展示命令。
模型、工具、Skill、任务和会话状态命令不得在 TypeScript 中保留第二套执行
实现，必须通过 Gateway 调用 Python 命令注册表或交互控制器。

```text
/inject-requirement <run-id> <change.json>
/interrupt 用户要求暂停当前轮次
/resume <run-id>
```

运行证据不会被删除或覆盖；中断会把未完成节点回到 `ready` 并写入 `checkpoint.json`。需求变更会进入 `interrupt_queue`，由 Orchestrator 在安全边界调用 Governor 生成并应用 TaskGraph Patch，再重新分析受影响子图。
