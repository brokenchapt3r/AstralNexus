# 天枢系统用户手册

## 概述

天枢（AstralNexus）是一个面向超长程复杂任务的动态异构群体智能协同推理引擎。它能够将复杂的任务分解为可管理的子任务，并通过多个专门的智能体协同工作来完成这些任务。

本手册将指导您如何安装、配置和使用天枢系统。

## 系统要求

### 硬件要求

- CPU: 至少双核处理器
- 内存: 至少 8GB RAM
- 存储: 至少 10GB 可用磁盘空间
- 网络: 稳定的互联网连接（用于访问云端模型）

### 软件要求

- 操作系统: Windows 10+, macOS 10.15+, 或 Linux (Ubuntu 20.04+)
- Python: 3.8 或更高版本
- Git: 2.20 或更高版本

## 安装指南

### 1. 克隆仓库

打开终端或命令提示符，执行以下命令：

```bash
git clone <repository-url>
cd AstralNexus
```

### 2. 创建虚拟环境

```bash
python -m venv venv
```

激活虚拟环境：

- Windows:
```bash
venv\Scripts\activate
```

- macOS/Linux:
```bash
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 设置环境变量

设置 AstralNexus 主目录：

- Windows:
```bash
set ASTRALNEXUS_HOME=%USERPROFILE%\AstralNexus
```

- macOS/Linux:
```bash
export ASTRALNEXUS_HOME=$HOME/AstralNexus
```

## 快速入门

### 1. 运行示例任务

天枢项目包含了一些示例任务，您可以使用以下命令运行：

```bash
python -m AstralNexus run --task examples/tasks/software_fix.md --config examples/configs/local_cloud.yaml
```

### 2. 使用交互式 CLI

启动交互式命令行界面：

```bash
python -m AstralNexus.cli.main
```

在 CLI 中，您可以使用以下命令：

- `/new [goal]` - 开始新的任务会话
- `/run <task-file>` - 运行任务
- `/eval <benchmark>` - 运行评估基准
- `/report <run-id>` - 生成报告
- `/dashboard [run-id]` - 启动仪表板
- `/inject-fault <run-id> <type>` - 注入故障
- `/help` - 显示帮助信息
- `/quit` - 退出程序

## 配置指南

### 配置文件结构

天枢使用 YAML 格式的配置文件。默认配置文件位于 `config.yaml`。

主要配置项包括：

#### 模型配置
```yaml
model:
  default: "gpt-4"
  providers:
    - name: "openai"
      api_key: "${OPENAI_API_KEY}"
    - name: "anthropic"
      api_key: "${ANTHROPIC_API_KEY}"
```

#### 执行设置
```yaml
execution:
  max_concurrent_tasks: 5
  task_timeout_seconds: 3600
  retry_attempts: 3
```

#### 资源分配
```yaml
resources:
  local:
    privacy: high
    cost: low
    latency: low
    capabilities: 
      - file_operations
      - terminal_access
      - light_reasoning
  edge:
    privacy: medium
    cost: medium
    latency: medium
    capabilities:
      - retrieval
      - medium_reasoning
  cloud:
    privacy: low
    cost: high
    latency: variable
    capabilities:
      - planning
      - long_context
      - hard_reasoning
```

#### 记忆设置
```yaml
memory:
  short_term_capacity: 100
  medium_term_capacity: 1000
  long_term_capacity: 10000
  compression_threshold_hours: 24
```

#### 跟踪和日志
```yaml
tracing:
  enabled: true
  level: "INFO"
  output_directory: "./logs"
```

#### 仪表板设置
```yaml
dashboard:
  port: 8080
  host: "localhost"
  enable_https: false
```

#### 故障注入设置
```yaml
fault_injection:
  enabled: false
  default_probability: 0.01
  fault_types:
    - requirement_change
    - node_failure
    - tool_failure
    - memory_loss
    - resource_degradation
```

### 环境变量

天枢使用环境变量来存储敏感信息，如 API 密钥：

- `OPENAI_API_KEY`: OpenAI API 密钥
- `ANTHROPIC_API_KEY`: Anthropic API 密钥
- `ASTRALNEXUS_HOME`: AstralNexus 主目录

## 使用指南

### 1. 创建任务文件

任务文件使用 Markdown 格式，描述要执行的任务。例如：

```markdown
# 软件工程任务示例

## 任务目标

为给定代码仓库增加"任务失败后自动恢复并生成报告"的能力。

## 具体要求

1. 保持原有接口兼容
2. 新增单元测试和集成测试
3. 通过所有测试
4. 输出设计说明

## 验收标准

- 代码修改不影响现有功能
- 新增的自动恢复机制能够处理常见的任务失败场景
- 生成的报告包含失败原因、恢复过程和最终状态
- 所有新增代码都有相应的测试覆盖
```

### 2. 运行任务

使用以下命令运行任务：

```bash
python -m AstralNexus run --task <task-file> --config <config-file>
```

例如：

```bash
python -m AstralNexus run --task examples/tasks/software_fix.md --config examples/configs/local_cloud.yaml
```

### 3. 查看运行结果

任务完成后，结果将保存在运行目录中。默认情况下，运行目录位于 `$ASTRALNEXUS_HOME/runs/` 下。

### 4. 生成报告

使用以下命令生成任务报告：

```bash
python -m AstralNexus report --run-id <run-id>
```

### 5. 启动仪表板

使用以下命令启动 Web 仪表板：

```bash
python -m AstralNexus dashboard --run-id <run-id>
```

然后在浏览器中访问 `http://localhost:8080` 查看仪表板。

### 6. 故障注入

天枢支持故障注入以测试系统的鲁棒性：

```bash
python -m AstralNexus inject-fault --run-id <run-id> --type <fault-type> --payload <payload-file>
```

支持的故障类型包括：
- `requirement_change`: 需求变更
- `node_failure`: 节点失败
- `tool_failure`: 工具失败
- `memory_loss`: 记忆缺失
- `resource_degradation`: 资源降级

## 评估基准

天枢包含了多个评估基准，用于测试系统在不同场景下的性能：

### 1. 软件工程基准

测试系统在软件工程任务中的表现：

```bash
python -m AstralNexus eval --benchmark benchmarks/software_engineering
```

### 2. 研究报告基准

测试系统在跨域研究和决策报告任务中的表现：

```bash
python -m AstralNexus eval --benchmark benchmarks/research_report
```

### 3. 故障注入基准

测试系统的故障检测和恢复能力：

```bash
python -m AstralNexus eval --benchmark benchmarks/fault_injection
```

## 故障排除

### 常见问题

#### 1. 安装依赖时出现错误

确保您的 Python 版本符合要求，并尝试升级 pip：

```bash
pip install --upgrade pip
```

#### 2. 运行任务时出现 API 密钥错误

检查您的环境变量是否正确设置了 API 密钥：

```bash
echo $OPENAI_API_KEY  # macOS/Linux
echo %OPENAI_API_KEY%  # Windows
```

#### 3. 任务执行时间过长

您可以调整配置文件中的超时设置：

```yaml
execution:
  task_timeout_seconds: 7200  # 增加到 2 小时
```

#### 4. 内存不足

减少并发任务数量：

```yaml
execution:
  max_concurrent_tasks: 3  # 减少到 3 个并发任务
```

### 日志查看

天枢会在 `$ASTRALNEXUS_HOME/logs/` 目录下生成日志文件。查看日志可以帮助诊断问题。

## 最佳实践

### 1. 任务设计

- 清晰定义任务目标和验收标准
- 将复杂任务分解为较小的子任务
- 提供足够的上下文信息

### 2. 配置优化

- 根据任务特点选择合适的资源分配策略
- 合理设置超时和重试参数
- 根据需要启用或禁用故障注入

### 3. 性能调优

- 监控资源使用情况
- 定期清理历史运行数据
- 根据需要调整记忆容量设置

### 4. 安全考虑

- 妥善保管 API 密钥等敏感信息
- 定期更新依赖包以修复安全漏洞
- 在处理敏感数据时使用本地资源

## 高级功能

### 1. 自定义智能体角色

您可以创建自定义的智能体角色来处理特定类型的任务。详细信息请参阅开发指南。

### 2. 自定义工具

您可以为智能体添加自定义工具以扩展其能力。详细信息请参阅开发指南。

### 3. 插件系统

天枢支持插件系统，允许您扩展系统的功能。详细信息请参阅开发指南。

## 社区和支持

### 获取帮助

如果您在使用天枢时遇到问题，可以通过以下方式获取帮助：

1. 查看文档和 FAQ
2. 在 GitHub 上提交 issue
3. 加入社区讨论组

### 贡献代码

欢迎贡献代码、报告问题或提出改进建议。请遵循以下步骤：

1. Fork 仓库
2. 创建功能分支
3. 提交更改
4. 推送到分支
5. 创建 Pull Request

### 反馈和建议

我们非常重视用户的反馈和建议。如果您有任何想法或建议，请通过以下方式联系我们：

- 提交 Issue
- 发送邮件至 [feedback@astralnexus.ai](mailto:feedback@astralnexus.ai)

## 许可证

天枢采用 MIT 许可证。详细信息请查看项目根目录下的 LICENSE 文件。

## 版本历史

有关版本历史和更新日志，请查看 CHANGELOG.md 文件。

## 致谢

感谢所有为天枢项目做出贡献的开发者和研究人员。