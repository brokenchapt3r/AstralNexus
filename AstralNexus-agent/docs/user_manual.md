# 用户手册

本手册描述当前代码能够提供的功能。能力边界和已知限制见 [能力状态表](status.md)。

## 1. 环境准备

AstralNexus 要求 Python 3.11+。

```bash
pip install -e .
```

设置模型服务的 API key。变量名由 provider 配置决定，例如：

```powershell
$env:DASHSCOPE_API_KEY = "your-key"
```

当前模型客户端使用 OpenAI-compatible Chat Completions 接口。配置示例：

```yaml
model:
  default: deepseek-v4.1-flash
  provider: dashscope
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
```

配置文件不负责执行策略、物理资源、长期记忆或 Dashboard 配置。

## 2. 运行任务

```bash
python -m AstralNexus.cli.main run \
  --task examples/tasks/software_fix.md \
  --config config.yaml \
  --output ./runs \
  --max-parallel 4
```

`--task` 必须指向 UTF-8 任务说明文件。可选参数：

- `--model`：覆盖配置中的模型名称。
- `--provider`：覆盖 provider 标签。
- `--max-parallel`：限制并行工作节点数量。
- `--output`：指定运行根目录。

运行创建独立目录并保存 SQLite 状态、兼容性状态文件和产物。最终交付应以验收门结果为准，而不是以工作节点是否返回成功为准。

## 3. 恢复任务

```bash
python -m AstralNexus.cli.main resume \
  --run-dir ./runs/<run-id> \
  --max-parallel 4
```

如需在恢复时补充原任务文件，可添加 `--task <path>`；由用户主动恢复时可使用 `--user-resume`。恢复会读取已有运行状态，不会创建一个与原任务无关的新任务。

## 4. 重新验收历史结果

```bash
python -m AstralNexus.cli.main verify-delivery \
  --run-dir ./runs/<run-id>
```

如果已有明确的候选文件，可使用 `--candidate` 指定；`--output` 可指定验收输出位置。该命令适合在验收规则升级后重新检查旧运行，但不能补造原运行中没有保存的证据。

## 5. 查看运行

进入交互模式：

```bash
python -m AstralNexus.cli.main
```

常用交互命令包括：

- `list`、`status`：列出和查看运行。
- `resume`：恢复运行。
- `interrupt`、`inject-requirement`：中断或注入需求变化。
- `report`：生成指定运行的报告。
- `verify-delivery`：重新执行交付验收。
- `dashboard`：启动本地观察界面。
- `tools`、`skills`、`models`：查看当前环境可发现的能力。

使用 `help` 获取当前参数说明。交互命令与一次性子命令的参数形式可能不同，应以帮助输出为准。

Dashboard 也可直接启动：

```bash
python -m AstralNexus.cli.main dashboard --run-id latest --port 8080
```

然后访问 `http://127.0.0.1:8080/`。页面关闭不会停止任务，停止 Dashboard 进程也不会修改任务状态。资源视图展示的是逻辑 local/edge/cloud 租约，不代表任务已在真实远端机器执行。

## 6. 合成场景评测

```bash
python -m AstralNexus.cli.main eval \
  --scenario code_refactor \
  --nodes 20 \
  --seed 42 \
  --output ./eval-runs
```

有效场景：

- `research_project`
- `code_refactor`
- `data_analysis`

`--strict-runtime` 会启用更严格的运行检查。评测是合成回归工具，不是公开 benchmark，也不能直接证明生产性能。当前 CLI 的历史默认场景 `long_research` 无实现，因此必须显式传入有效场景。

## 7. 故障与需求变化

交互命令可向活动运行注入节点、工具、资源、内存或需求变化事件。它们主要用于验证恢复控制流，其中一些是逻辑事件模拟，并不会真的断开机器、删除全部内存或破坏外部服务。

## 8. 常见问题

### 模型请求失败

检查 API key、`base_url`、模型名和目标服务是否兼容 `/chat/completions`。provider 标签本身不会自动切换成厂商原生协议。

### 找不到交付文件

先查看运行目录、数据库登记的 artifacts 和验收输出。工作节点输出文本不一定已经物化为文件。

### 运行看似卡住

用 `status` 或 Dashboard 检查当前节点、等待依赖、验证/恢复状态和最近事件。不要仅通过控制台暂时没有输出判断停止。

### 可以跨运行记住知识吗

当前没有完整的跨运行学习记忆。单次运行的 facts 可以持久化，但不会自动形成共享知识库。
