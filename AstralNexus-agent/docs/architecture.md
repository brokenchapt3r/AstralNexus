# 架构说明

AstralNexus 当前是一个以受治理 TaskGraph 为中心的本地多智能体运行时。架构目标是让规划、执行、验证、恢复和交付都有可追踪状态，而不是把一次模型调用包装成“代理”。

## 主执行链路

```text
CLI / Workbench
       |
       v
Governor + Orchestrator facade
       |
       v
Planning -> TaskGraph -> ready waves -> workers/tools
       |                         |
       |                         v
       |                  verification/recovery
       |                         |
       v                         v
SQLite state <---------- artifacts/messages/facts
       |
       v
delivery materialization -> acceptance gate -> result
```

`core/orchestrator.py` 主要保留兼容导出；编排实现已经拆到 `core/orchestration/`。新增逻辑应优先放入职责对应的子模块，避免重新形成单体 orchestrator。

## 核心模块

### TaskGraph 与规划

`core/task_graph.py` 定义任务节点、依赖和图状态。规划器生成初始图，运行中可通过受治理补丁修改图。补丁需要经过结构和所有权校验，不能把 LLM 输出直接视为可信状态变更。

### Governor 与角色

Governor 负责角色实例、执行边界和工作流程。`agent/`、`agents/` 提供基础代理和角色实现。角色按配置和任务创建，不应在文档中描述成永远固定的线性流水线。

### Orchestration

`core/orchestration/` 负责规划、上下文构建、波次执行、验证、恢复、交付和相关治理逻辑。它协调模块，但持久化、工具调用和模型访问分别由下层组件承担。

### Runtime

`runtime/` 提供模型客户端、本地子进程、工作区、追踪、插件宿主和其他执行基础设施。当前模型协议是 OpenAI-compatible Chat Completions；当前资源标签不会自动映射到真实边缘或云端执行器。

### Storage

`storage/sqlite_store.py` 是每次运行的持久化事实来源，覆盖运行、节点、边、消息、事实、产物、指标、故障、恢复、资源决策、约束版本和协调器围栏等信息。`runtime_state.json` 是运行级状态与兼容性快照，不应替代事务性数据库。

### Tools

`tools/` 提供集中式注册、自动发现、schema、审批和分发，并包含内置工具、工具集、MCP 与技能适配。工具可用性由安装环境和安全策略决定。

### Delivery

执行完成与交付通过是两个阶段。系统先收集或物化交付候选，再由验收门依据需求、产物和证据判定。`verify-delivery` 可对历史运行重新执行这一步。

## 状态与一致性

- TaskGraph 是调度状态，不等同于交付证据。
- SQLite 是运行事实的主要持久化来源。
- 协调器所有权和 commit fence 用于降低多个恢复进程同时写入的风险。
- 外部工具副作用通常无法由数据库事务自动回滚，工具应尽量设计成幂等。
- 事件流和 Dashboard 是观察面，不应成为核心状态的唯一来源。

## 当前边界

以下概念存在设计或部分代码，但不是完整架构能力：

- 分层内存不等于跨运行长期学习或向量知识库。
- local/edge/cloud 资源对象不等于真实异构集群调度。
- 敏感度字段不等于已经强制执行本地隔离和自动脱敏。
- Workbench 内部路由不等于稳定、鉴权的公共 REST API。
- native/gate 插件能力不包含 sidecar 插件进程。

完整状态见 [能力状态表](status.md)。
