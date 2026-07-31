# 天枢比赛交付入口

## 本地命令

```text
AstralNexus run <任务描述>
AstralNexus eval --benchmark smoke
AstralNexus report
AstralNexus dashboard
AstralNexus inject-fault --run-id <run_id> --type requirement_change --payload <payload>
```

## 运行产物

```text
ASTRALNEXUS_HOME/runs/<run_id>/
  manifest.json
  trace.jsonl
  faults.jsonl
  report.md
  dashboard.html
  artifacts/
```

所有对外演示材料、报告标题、日志前缀和界面标题统一使用“天枢”。

