# AstralNexus 运行时组件

## 概述

AstralNexus 运行时组件提供了系统执行任务所需的核心功能。

## 组件列表

1. **agent.py** - 运行时代理
2. **context.py** - 上下文管理器
3. **model_client.py** - 模型客户端
4. **tool_executor.py** - 工具执行器
5. **trace.py** - 轨迹记录器
6. **types.py** - 运行时类型定义
7. **workspace.py** - 工作区边界
8. **memory.py** - 运行时记忆

## 运行时代理

`RuntimeAgent` 类是 AstralNexus 系统的入口点，负责执行任务。

## 上下文管理器

`ContextManager` 类管理任务执行的上下文，包括提示构建和压缩。

## 模型客户端

`ModelClient` 类提供与各种模型提供者的接口。

## 工具执行器

`ToolExecutor` 类负责执行各种工具。

## 轨迹记录器

`TraceRecorder` 类记录任务执行的轨迹，用于调试和评估。

## 运行时类型

`types.py` 文件定义了运行时使用的各种类型。

## 工作区边界

`WorkspaceBoundary` 类管理任务执行的工作区。

## 运行时记忆

`RuntimeMemory` 类管理任务执行期间的记忆。