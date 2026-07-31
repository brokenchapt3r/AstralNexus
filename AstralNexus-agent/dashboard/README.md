# AstralNexus Live Dashboard

实时控制台消费运行目录中的 `trace.jsonl` 与 `agent_lifecycle.jsonl`，因此
不需要轮询数据库，也不会生成第二套业务状态。它把审计事件投影为五个彼此独立
但可关联的视图状态：

- Task Graph：任务节点、依赖边、增量 Patch 与 Ready-Wave。
- Agent Population：真实 Agent 实例、角色、能力、模型和生命周期。
- Communication Topology：结构化消息、证据路由和控制平面结果提交。
- Resource State：端、边、云资源与 Agent/任务分配。
- Runtime Metrics：并行宽度、Token、通信、恢复和成功率。

## 启动

推荐通过 CLI 启动，端口默认 `8080`：

```powershell
$env:PYTHONPATH = "C:\Users\Finitude\Documents\方寸实习\天枢_AstralNexus项目\01_源码"
python -m AstralNexus.cli.main dashboard --run-id latest --port 8080
```

也可以直接指定运行根目录：

```powershell
python -m AstralNexus.dashboard.live_server `
  --home "$env:USERPROFILE\.astralnexus" --run-id latest --port 8080
```

`--run-id latest` 优先跟随 CLI 写入的 `current_run.json`，因此会展示当前正在
执行的任务；只有没有活动指针时才按最近修改时间回退。需要查看历史任务时，
请显式传入具体的 `--run-id`。

打开 `http://127.0.0.1:8080/`。主图可以在 `Task DAG`、`Agent Topology`
和 `Resource View` 间切换；顶部显示群体规模、活跃 Agent、当前并行 Wave、
通信、恢复与 Token 节省指标。右侧可查看任务输入/输出、Agent 能力与资源归属，
底部显示 SSE 控制面事件流。

页面关闭不会停止任务；按 `Ctrl+C` 停止仪表板进程。历史运行若尚未记录显式
`agent.communication.link_created`，控制台会把已经发生的
`task.structured_message` 投影为 Worker 到 Governor 的结果提交链，并将该链
标记为兼容投影，不虚构 Worker 间通信。
