# AstralNexus

AstralNexus 是一个仍在演进中的多智能体任务编排框架。当前版本已经具备任务图规划、受治理的并行执行、运行状态持久化、恢复、验证与交付验收等核心链路；资源调度、长期记忆、故障注入和评测仍属于有限实现或实验能力。

项目当前能力边界以 [能力状态表](status.md) 为准。文档中的“规划能力”不等于已经完成的功能承诺。

## 当前可用能力

- 将自然语言任务规划为带依赖关系的 `TaskGraph`，按就绪波次执行。
- 通过 Governor、角色配置、工具注册表和结构化消息治理执行过程。
- 将任务、消息、事实、产物、指标和恢复记录持久化到每次运行的 SQLite 数据库。
- 支持中断、需求变更、断点恢复和协调器所有权保护。
- 检查实际执行结果与必需文件落盘；模型生成的业务判据只作诊断。默认交付使用输出差异与 `present_files` 的确定性呈现检查。
- 提供终端命令、事件流以及本地 Dashboard/Workbench。
- 支持 OpenAI-compatible Chat Completions 接口。

## 安装

要求 Python 3.11 或更高版本。

```bash
pip install -e .
```

配置模型密钥，例如：

```powershell
$env:DASHSCOPE_API_KEY = "your-key"
```

根目录 `config.yaml` 当前只用于选择模型、提供商标签和兼容接口地址。环境变量仍是密钥来源。

## 快速开始

一次性运行：

```bash
python -m AstralNexus.cli.main run --task examples/tasks/software_fix.md --config config.yaml --output ./runs
```

恢复已有运行：

```bash
python -m AstralNexus.cli.main resume --run-dir ./runs/<run-id>
```

离线复查已有交付（独立诊断，不改写历史运行结论）：

```bash
python -m AstralNexus.cli.main verify-delivery --run-dir ./runs/<run-id>
```

运行合成场景评测（实验功能）：

```bash
python -m AstralNexus.cli.main eval --scenario research_project --nodes 20 --seed 42
```

当前有效场景为 `research_project`、`code_refactor` 和 `data_analysis`。请显式传入 `--scenario`；CLI 的历史默认值 `long_research` 目前没有对应生成器。

交互模式：

```bash
python -m AstralNexus.cli.main
```

在交互模式中可使用 `help` 查看 `run`、`resume`、`status`、`interrupt`、`report`、`verify-delivery`、`dashboard` 等命令。

## 完成口径

默认模式只证明执行与交付呈现：没有新增或修改的输出时按普通对话处理；有输出时，Lead 必须调用 `present_files` 呈现至少一个本轮产物。系统先持久化唯一 `run.delivery`，再提交最终运行状态。`completed` 不表示全部业务需求已经通过语义验收。

在交互对话或网页中显式输入 `/goal <完成条件>` 才启用独立 Goal 评估。Goal 使用单一 Lead 执行工具回合，不强制先拆成任务图。评估器只使用可见对话和工具证据，不重新读文件或执行命令；仅 `goal_not_met_yet` 可自动继续，最多 8 次，连续 2 次无新事实停止。缺证据、需要用户输入、外部等待和运行失败均停止。取消或失去所有权不会清除 Goal。

工作文件位于运行工作区；最终用户文件位于 `outputs/`。默认交付 Lead 只呈现文件，不能重写产品。发布时绑定同一份快照的字节、交付凭据与运行围栏。

## 运行产物

每次运行通常在输出目录下创建独立的运行目录，包含：

- `task_graph.sqlite`：任务图及检查点。
- `.astralnexus_state/executions.sqlite`：工具执行回执与唯一交付凭据。
- `runtime_state.json`：运行级状态和兼容性快照。
- `outputs/`：本轮最终用户文件；`deliverables/vN/`：冻结发布版本。
- 交付候选与验收结果：由运行流程或 `verify-delivery` 生成，具体路径以命令输出为准。

不要把数据库或事件日志当作用户交付物。默认呈现事实、显式 Goal 结论与独立业务测试是不同范围的证据。

## 项目结构

Python 包位于 `src/AstralNexus/`。测试、文档、示例和开发脚本分别位于仓库顶层的 `tests/`、`docs/`、`examples/`、`scripts/`；下列 core/runtime/tools 等目录均相对于 Python 包。

```text
AstralNexus/
├── core/                 # 任务图、Governor 与 orchestration 实现
├── agent/ 和 agents/     # 基础代理、角色实现与角色配置
├── runtime/              # 模型客户端、执行环境、追踪与会话基础设施
├── tools/                # 工具注册、内置工具、工具集、MCP 与技能适配
├── storage/              # SQLite 持久化
├── cli/                  # CLI 与交互入口
├── dashboard/            # 本地 Dashboard/Workbench
├── eval/                 # 合成场景评测
├── examples/             # 任务示例
├── docs/                 # 用户、架构、开发和能力状态文档
└── tests/                # 自动化测试
```

## 文档

- [能力状态与限制](status.md)
- [用户手册](user_manual.md)
- [架构说明](architecture.md)
- [开发指南](development.md)

## 开发验证

```bash
python -m compileall -q src/AstralNexus
pytest -q
```

完整测试可能包含需要模型密钥或外部环境的用例。修改时应先运行与变更直接相关的窄测试，再按环境能力扩大范围。

## 当前不应依赖的能力

以下内容尚不能作为稳定承诺：跨运行长期学习记忆、向量语义检索、真实边缘/云端执行后端、敏感任务强制本地执行、原生 Anthropic 协议、完整命名 benchmark 套件、版本化公共 REST API、sidecar 插件进程。详见 [能力状态表](status.md)。

## 许可证

MIT，见 [LICENSE](../src/AstralNexus/LICENSE)。
