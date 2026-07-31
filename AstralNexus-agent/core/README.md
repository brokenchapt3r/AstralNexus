# AstralNexus 核心组件

## 概述

AstralNexus 核心组件提供了系统的基础功能，包括任务图、路由、记忆、调度等。

## 组件列表

1. **task_graph.py** - 任务图定义和操作
2. **router.py** - 认知拓扑路由器
3. **memory_federation.py** - 分层任务记忆
4. **resource_scheduler.py** - 资源调度器
5. **fault_recovery.py** - 故障恢复机制
6. **message_protocol.py** - 消息协议
7. **orchestrator.py** - 任务协调器
8. **agent_registry.py** - 智能体注册表
9. **role_registry.py** - 角色注册表
10. **role_worker.py** - 角色工作者
11. **artifacts.py** - 产物管理
12. **identity.py** - 系统身份
13. **evaluator.py** - 评估器
14. **state_machine.py** - 状态机
15. ~~orchestrated_runner.py~~ - 已移除，功能合并至 orchestrator.py

## 任务图

`TaskGraph` 类表示任务的有向无环图结构，是系统的核心数据结构。

## 路由器

`Router` 类负责根据任务特征和系统状态动态选择执行路径。

## 记忆联邦

`MemoryFederation` 类管理分层任务记忆，包括短期、中期和长期记忆。

## 资源调度器

`ResourceScheduler` 类根据任务需求和资源状态动态分配计算资源。

## 故障恢复

`FaultRecovery` 类处理系统中的各种故障并实现自动恢复。

## 消息协议

`MessageProtocol` 类定义智能体之间的通信协议。

## 协调器

`Orchestrator` 类协调多个智能体的工作。

## 智能体注册表

`AgentRegistry` 类管理所有可用的智能体。

## 角色注册表

`RoleRegistry` 类管理所有可用的角色。

## 角色工作者

`RoleWorker` 类实现具体的角色功能。

## 产物管理

`Artifacts` 类管理任务执行过程中产生的各种产物。

## 系统身份

`Identity` 类管理系统的身份信息。

## 评估器

`Evaluator` 类评估任务执行的效果。

## 状态机

`StateMachine` 类管理任务节点的状态转换。

## 编排运行器

`OrchestratedRunner` 类负责编排和执行复杂的任务流程。