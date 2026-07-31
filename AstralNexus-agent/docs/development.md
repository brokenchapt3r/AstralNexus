# 天枢系统开发指南

## 概述

本文档为开发者提供了天枢系统的开发指南，包括环境搭建、代码结构、开发流程和最佳实践等内容。

## 环境搭建

### 系统要求

- Python 3.8+
- pip 或 conda 包管理器
- Git 版本控制系统
- SQLite 3.35+ (用于存储)
- 可选：Docker (用于容器化部署)

### 安装步骤

1. 克隆仓库：
```bash
git clone <repository-url>
cd AstralNexus
```

2. 创建虚拟环境：
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\Scripts\activate  # Windows
```

3. 安装依赖：
```bash
pip install -r requirements.txt
```

4. 设置环境变量：
```bash
export ASTRALNEXUS_HOME=/path/to/astralnexus
```

### 开发工具推荐

- VS Code 或 PyCharm 作为 IDE
- Black 作为代码格式化工具
- Flake8 作为代码检查工具
- pytest 作为测试框架
- Sphinx 作为文档生成工具

## 代码结构

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
├── docs/                # 文档
├── tests/               # 测试
├── requirements.txt     # 依赖列表
├── config.yaml         # 默认配置
├── README.md           # 项目说明
└── __init__.py         # 包初始化文件
```

### 核心模块详解

#### 1. cli/ - 命令行接口

包含命令行界面的实现，处理用户输入并调用相应的功能模块。

主要文件：
- `main.py`: 主程序入口
- `commands.py`: 命令定义和处理

#### 2. core/ - 核心组件

包含系统的核心功能模块，如任务图、路由、记忆、调度等。

主要文件：
- `task_graph.py`: 任务图定义和操作
- `router.py`: 认知拓扑路由器
- `memory_federation.py`: 分层任务记忆
- `resource_scheduler.py`: 资源调度器
- `fault_recovery.py`: 故障恢复机制
- `message_protocol.py`: 消息协议
- `orchestrator.py`: 任务协调器
- `agent_registry.py`: 智能体注册表
- `role_registry.py`: 角色注册表
- `role_worker.py`: 角色工作者
- `artifacts.py`: 产物管理
- `identity.py`: 系统身份
- `evaluator.py`: 评估器
- `state_machine.py`: 状态机

#### 3. agents/ - 智能体实现

包含各种智能体角色的具体实现。

主要文件：
- `base.py`: 智能体基类
- `planner.py`: 规划者
- `researcher.py`: 研究员
- `coder.py`: 编码员
- `verifier.py`: 验证员
- `reviewer.py`: 审查员
- `memory_curator.py`: 记忆管理员
- `resource_broker.py`: 资源经纪人
- `recovery_agent.py`: 恢复代理
- `writer.py`: 作家

#### 4. runtime/ - 运行时组件

包含运行时所需的组件，如上下文管理、模型客户端、工具执行器等。

主要文件：
- `agent.py`: 运行时代理
- `context.py`: 上下文管理器
- `model_client.py`: 模型客户端
- `tool_executor.py`: 工具执行器
- `trace.py`: 轨迹记录器
- `types.py`: 运行时类型定义
- `workspace.py`: 工作区边界
- `memory.py`: 运行时记忆

#### 5. storage/ - 存储组件

包含数据存储相关的实现。

主要文件：
- `sqlite_store.py`: SQLite 存储实现

#### 6. tools/ - 工具集

包含各种工具的实现，供智能体使用。

#### 7. benchmarks/ - 评估基准

包含各种评估基准测试用例。

#### 8. examples/ - 示例

包含使用示例和配置示例。

#### 9. docs/ - 文档

包含项目文档。

#### 10. tests/ - 测试

包含单元测试和集成测试。

## 开发流程

### 1. 分支管理

采用 Git Flow 分支管理策略：

- `main`: 主分支，包含生产就绪的代码
- `develop`: 开发分支，包含最新的开发代码
- `feature/*`: 功能分支，用于开发新功能
- `bugfix/*`: 修复分支，用于修复 bug
- `release/*`: 发布分支，用于准备新版本发布

### 2. 开发步骤

1. 从 `develop` 分支创建功能分支：
```bash
git checkout develop
git pull origin develop
git checkout -b feature/your-feature-name
```

2. 进行开发并提交代码：
```bash
git add .
git commit -m "Add feature: your feature description"
```

3. 推送分支并创建 Pull Request：
```bash
git push origin feature/your-feature-name
```

4. 代码审查和合并：
- 等待团队成员审查代码
- 根据反馈进行修改
- 审查通过后合并到 `develop` 分支

### 3. 代码规范

#### 命名约定

- 类名采用 PascalCase 命名法
- 函数和变量名采用 snake_case 命名法
- 常量名采用 UPPER_SNAKE_CASE 命名法
- 私有成员以单下划线开头

#### 代码格式

使用 Black 进行代码格式化：
```bash
black .
```

#### 代码检查

使用 Flake8 进行代码检查：
```bash
flake8 .
```

#### 类型注解

尽可能为函数参数和返回值添加类型注解。

#### 文档字符串

为所有公共类和函数添加文档字符串，使用 Google 风格。

### 4. 测试

#### 单元测试

为每个模块编写单元测试，确保代码的正确性。

#### 集成测试

编写集成测试，验证模块之间的交互。

#### 测试覆盖率

目标测试覆盖率达到 80% 以上。

运行测试：
```bash
python -m pytest tests/
```

### 5. 文档

#### 代码文档

为所有公共接口添加详细的文档字符串。

#### 项目文档

在 `docs/` 目录下维护项目文档，包括：
- 架构文档
- 算法文档
- 评估文档
- 开发指南
- 用户手册

使用 Sphinx 生成文档：
```bash
cd docs
make html
```

## 最佳实践

### 1. 设计原则

#### 单一职责原则
每个类和函数应该只有一个改变的理由。

#### 开闭原则
对扩展开放，对修改关闭。

#### 里氏替换原则
子类应该能够替换其父类而不影响程序的正确性。

#### 接口隔离原则
客户端不应该依赖它不需要的接口。

#### 依赖倒置原则
高层模块不应该依赖低层模块，两者都应该依赖抽象。

### 2. 错误处理

- 使用异常而不是返回码来处理错误
- 提供有意义的错误消息
- 记录错误日志以便调试
- 优雅地处理异常情况

### 3. 日志记录

- 使用适当的日志级别（DEBUG, INFO, WARNING, ERROR, CRITICAL）
- 记录足够的信息以便调试，但不要记录敏感信息
- 使用结构化日志以便于分析

### 4. 性能优化

- 避免不必要的计算
- 使用适当的数据结构
- 缓存昂贵的计算结果
- 优化数据库查询

### 5. 安全性

- 验证所有输入
- 避免 SQL 注入
- 保护敏感信息
- 使用安全的密码学实践

### 6. 可维护性

- 编写清晰、简洁的代码
- 添加适当的注释
- 保持一致的代码风格
- 定期重构代码以改善设计

## 贡献指南

### 报告问题

如果发现 bug 或有改进建议，请在 GitHub 上提交 issue。

### 提交代码

1. Fork 仓库
2. 创建功能分支
3. 提交更改
4. 推送到分支
5. 创建 Pull Request

### 代码审查

所有 Pull Request 都需要经过代码审查才能合并。

### 测试要求

所有新功能都需要有相应的测试用例。

## 发布流程

### 版本号规则

采用语义化版本号（SemVer）：
- MAJOR: 不兼容的 API 修改
- MINOR: 向后兼容的功能新增
- PATCH: 向后兼容的问题修正

### 发布步骤

1. 从 `develop` 分支创建 `release/x.y.z` 分支
2. 更新版本号
3. 更新 CHANGELOG
4. 进行最终测试
5. 合并到 `main` 和 `develop` 分支
6. 在 `main` 分支上打标签
7. 发布到 PyPI

## 常见问题解答

### 如何添加新的智能体角色？

1. 在 `agents/` 目录下创建新的智能体文件
2. 继承 `BaseAgent` 类并实现 `execute` 方法
3. 在 `core/role_registry.py` 中注册新角色

### 如何添加新的工具？

1. 在 `tools/` 目录下创建新工具文件
2. 实现工具功能
3. 在相应的角色定义中添加工具到 `default_tools` 列表

### 如何配置不同的资源？

在 `config.yaml` 文件中配置不同的资源，包括本地、边缘和云端资源的属性和能力。

### 如何运行基准测试？

使用命令行工具运行基准测试：
```bash
python -m AstralNexus eval --benchmark <benchmark-name>
```
