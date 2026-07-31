# AstralNexus 存储组件

## 概述

AstralNexus 存储组件提供了数据持久化的功能。

## 组件列表

1. **sqlite_store.py** - SQLite 存储实现

## SQLite 存储

`SQLiteTaskGraphStore` 类使用 SQLite 数据库存储任务图、消息、事实、产物、指标和故障等数据。

## 存储结构

存储结构包括以下表：

1. **runs** - 运行记录
2. **nodes** - 任务节点
3. **edges** - 任务边
4. **messages** - 消息记录
5. **facts** - 事实记录
6. **artifacts** - 产物记录
7. **metrics** - 指标记录
8. **state_events** - 状态事件记录

## 使用方法

```python
from AstralNexus.storage.sqlite_store import SQLiteTaskGraphStore

# 创建存储实例
store = SQLiteTaskGraphStore(Path("task_graph.db"))

# 创建运行记录
store.create_run("run-001", "Test goal", ["constraint1", "constraint2"])

# 添加节点
node_data = {
    "node_id": "T-001",
    "goal": "Test task",
    "role": "planner"
}
store.add_node(node_data)
```