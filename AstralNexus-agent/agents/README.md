# AstralNexus 智能体

## 概述

AstralNexus 智能体是系统中执行具体任务的组件，每个智能体都有特定的角色和能力。

## 智能体列表

1. **base.py** - 智能体基类
2. **planner.py** - 规划者
3. **researcher.py** - 研究员
4. **coder.py** - 编码员
5. **verifier.py** - 验证员
6. **reviewer.py** - 审查员
7. **memory_curator.py** - 记忆管理员
8. **resource_broker.py** - 资源经纪人
9. **recovery_agent.py** - 恢复代理
10. **writer.py** - 作家

## 智能体基类

`BaseAgent` 类是所有智能体的基类，定义了智能体的基本接口和通用功能。

## 规划者

`Planner` 智能体负责将复杂任务分解为子任务并创建任务图。

## 研究员

`Researcher` 智能体负责收集和分析信息。

## 编码员

`Coder` 智能体负责编写和修改代码。

## 验证员

`Verifier` 智能体负责验证任务结果。

## 审查员

`Reviewer` 智能体负责审查代码和文档。

## 记忆管理员

`MemoryCurator` 智能体负责管理任务记忆。

## 资源经纪人

`ResourceBroker` 智能体负责管理计算资源。

## 恢复代理

`RecoveryAgent` 智能体负责处理故障和恢复。

## 作家

`Writer` 智能体负责生成文档和报告。

## 创建新智能体

要创建新的智能体，请继承 `BaseAgent` 类并实现 `execute` 方法：

```python
from AstralNexus.agents.base import BaseAgent

class MyAgent(BaseAgent):
    def execute(self, context):
        # 实现智能体的逻辑
        pass
```