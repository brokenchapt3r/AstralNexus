# 天枢 (AstralNexus)

面向超长程复杂任务的动态异构群体智能协同推理引擎

## 简介

**天枢**是一套面向超长程复杂任务的动态异构群体智能协同推理引擎。系统以"语义任务图"为核心组织形式，通过动态稀疏拓扑路由、结构化低熵通信、分层任务记忆、端云自适应调度和故障注入自恢复机制，实现从自然语言高层意图到可验证交付物的无人干预闭环执行。

## 核心特性

1. **语义任务图** - 将复杂任务表示为动态 DAG，节点表示子任务，边表示依赖、证据、控制流、产物流和通信关系
2. **动态稀疏拓扑路由** - 根据任务语义、依赖关系、失败状态、资源约束和历史表现，动态选择稀疏通信边与子任务执行者
3. **低熵结构化通信协议** - 限制智能体之间自由长篇群聊，所有子任务结果必须收敛到结构化消息
4. **分层任务记忆** - 建立短期工作记忆、中期情景缓存、长期知识沉淀三层结构，保证长链任务中的目标一致性和状态连续性
5. **端云自适应调度** - 根据数据敏感度、实时性、复杂度、成本和模型能力，动态选择本地、边缘或云端推理资源
6. **故障注入自恢复机制** - 支持需求变更、节点失败、工具失败、记忆缺失、资源降级等故障注入和自动恢复

## 架构概览

天枢采用七层架构：

1. **意图解析层** - 将用户高层目标解析为任务合同
2. **语义任务图层** - 将复杂任务表示为动态 DAG
3. **异构角色层** - 管理不同能力智能体角色
4. **认知拓扑路由层** - 动态选择稀疏通信边与子任务执行者
5. **分层任务记忆层** - 建立三层记忆结构
6. **端云自适应调度层** - 动态选择推理资源
7. **观测评测层** - 输出任务图、拓扑变化、角色调度、token/时间消耗、失败恢复、验收结果和可视化报告

## 安装

```bash
# 克隆仓库
git clone <repository-url>
cd AstralNexus

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

## 使用方法

### 基本命令

```bash
# 运行任务
python -m AstralNexus run --task examples/tasks/software_fix.md --config examples/configs/local_cloud.yaml

# 运行评估基准
python -m AstralNexus eval --benchmark benchmarks/software_engineering

# 生成报告
python -m AstralNexus report --run-id <run_id>

# 启动仪表板
python -m AstralNexus dashboard --run-id <run_id>

# 注入故障
python -m AstralNexus inject-fault --run-id <run_id> --type requirement_change --payload change.json
```

### 交互式CLI

```bash
# 启动交互式CLI
python -m AstralNexus.cli.main
```

在CLI中，您可以使用以下命令：

- `/new [goal]` - 开始新的任务会话
- `/run <task-file>` - 运行任务
- `/eval <benchmark>` - 运行评估基准
- `/report <run-id>` - 生成报告
- `/dashboard [run-id]` - 启动仪表板
- `/inject-fault <run-id> <type>` - 注入故障
- `/help` - 显示帮助信息
- `/quit` - 退出程序

## 核心组件

### 1. 语义任务图 (Task Graph)
语义任务图是天枢的全局状态中枢，把长程任务从"线性对话历史"转化为"可查询、可回滚、可验收的结构化过程"。

### 2. 认知拓扑路由器 (Router)
认知拓扑路由器负责决定"下一步由谁做、拿什么上下文、与谁通信、用什么资源"。

### 3. 低熵结构化通信协议 (Message Protocol)
天枢不允许智能体之间自由长篇群聊，所有子任务结果必须收敛到结构化消息。

### 4. 分层任务记忆 (Memory Federation)
天枢采用三级任务记忆：短期工作记忆、中期情景缓存、长期知识沉淀。

### 5. 端云自适应调度器 (Resource Scheduler)
调度器根据任务属性选择推理资源和执行环境。

### 6. 故障注入与自恢复 (Fault Recovery)
故障注入是天枢的冲奖重点，系统不仅要展示"能完成任务"，还要展示"任务被打断后能自己恢复"。

## 开发指南

### 项目结构

```
AstralNexus/
├── cli/                 # 命令行接口
├── core/                # 核心组件
├── agents/              # 智能体实现
├── runtime/             # 运行时组件
├── storage/             # 存储组件
├── tools/               # 工具集
├── benchmarks/          # 评估基准
├── examples/            # 示例
└── docs/                # 文档
```

### 添加新角色

1. 在 `AstralNexus/agents/` 目录下创建新的代理文件
2. 继承 `BaseAgent` 类并实现 `execute` 方法
3. 在 `AstralNexus/core/role_registry.py` 中注册新角色

### 添加新工具

1. 在 `AstralNexus/tools/` 目录下创建新工具文件
2. 实现工具功能
3. 在相应的角色定义中添加工具到 `default_tools` 列表
