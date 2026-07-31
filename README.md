# 天枢（AstralNexus）

面向长程复杂任务的 AI 多智能体编排框架，提供自主规划、动态任务图、工具与技能集成、分层记忆、故障恢复，以及交互式终端界面。

## 仓库结构

| 目录 | 说明 |
| --- | --- |
| `AstralNexus-agent/` | Python 核心引擎、CLI、测试、示例和项目文档 |
| `tui/` | 基于 React 与 Ink 的 TypeScript 终端界面 |
| `apps/` | Python 与 TUI 共用的应用层资源 |
| `skills/` | AstralNexus 技能清单与扩展资源 |
| `AstralNexus.cmd` | Windows 快速启动脚本 |

## Python 核心

```powershell
cd AstralNexus-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
astralnexus --help
```

更完整的架构、命令和开发说明请参阅 [`AstralNexus-agent/README.md`](AstralNexus-agent/README.md)。

## TUI

```powershell
cd tui
npm ci
npm run build
```

TUI 的运行机制和开发命令请参阅 [`tui/README.md`](tui/README.md)。

## License

项目许可证见 [`LICENSE`](LICENSE)。TUI 所含 Hermes 相关组件的许可说明见 [`HERMES_TUI_LICENSE`](HERMES_TUI_LICENSE)。
