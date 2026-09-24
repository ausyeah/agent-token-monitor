<div align="center">

# OpenCode Token Monitor

### 面向 OpenCode Zen 免费计划的本地用量仪表盘

**不拿 API Key，不上传会话，不猜额度。** 只在本机 OpenCode 运行时读取它已经保存的本地数据库，把每一次模型请求变成可追溯的 Token、缓存和成本统计。

[![Windows](https://img.shields.io/badge/Windows-10%2B-0078D4?logo=windows&logoColor=white)](https://github.com/ausyeah/opencode-usage/releases/latest)
[![WebView2](https://img.shields.io/badge/WebView2-required-0078D4)](https://learn.microsoft.com/microsoft-edge/webview2/)
[![License: MIT](https://img.shields.io/badge/license-MIT-9b8b6f.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/ausyeah/opencode-usage?label=Release)](https://github.com/ausyeah/opencode-usage/releases/latest)

</div>

---

## 为什么做这个工具？

OpenCode Zen 免费计划最需要的往往不是“再看一个聊天窗口”，而是回答这些问题：

- 今天到底用了多少 **Token**？其中多少是缓存读取？
- 哪些 **供应商**、哪些 **模型 / 变体**贡献了最多用量？
- 最近 30 分钟是否突然出现用量尖峰？
- 免费模型的成本显示为 `0`，是否真的等于“没有使用量”？
- 如果账单缺失，参考价格和实际账单到底差多少？

OpenCode Token Monitor 把 OpenCode 本地数据库中的用量记录整理成一个长期、可搜索、可导出的 Windows 桌面仪表盘，特别适合观察 **OpenCode Zen 免费计划**的 Token 消耗和缓存命中情况。

> **重要说明**：这是本地用量统计工具，不是 OpenCode 或 Zen 的官方配额面板。它不会读取账号剩余额度，也不会绕过任何服务端限制；它展示的是本机 OpenCode 已记录的请求用量。model.dev 价格仅用于缺失账单时的本地估算。

## 功能亮点

### 看见真实的使用量，而不只是 `$0.00`

- 统计 **请求次数**、输入、输出、推理、缓存读取、缓存写入
- 同时显示“含缓存读取”和“不含缓存读取”两种口径
- Token 构成使用多色堆叠条：输入、输出、推理、缓存读取、缓存写入一眼可区分
- 悬停查看每一类的数量与占比，图例同步显示数值
- 账单成本、参考估算、采用成本、未定价状态分开显示，绝不把估算伪装成账单

### 适合免费计划的长期观察

- 今日、最近 24 小时、7 天、30 天、90 天、全部、自定义日期
- 供应商 / 模型 / 变体级筛选
- 周期趋势、供应商排行、模型排行、项目统计
- 今日每跨过一个 10M Token 档位提醒
- 最近 30 分钟达到突增阈值时提醒
- 托盘颜色、声音通知、开机启动和每日告警记录

### 为本地数据工作而设计

- OpenCode 未运行时**不会查询源数据库**
- 只读访问本机 `opencode.db`，工具自己的统计数据保存在独立数据库
- 后台同步与 Dashboard 刷新分离，退出 OpenCode 后自动停止后续源库查询
- 请求日志单次返回最近 100 条，避免 WebView2 Bridge 数据长度超限
- 永久历史、CSV 流水、价格来源和匹配状态均可导出

### 成本与显示方式可配置

- 官方价格：使用 [model.dev](https://models.dev/) 供应商价格与上下文阶梯
- 官方价格 × 全局倍率：适合按组织规则调整参考估算
- 完全自定义价格：按供应商 / 模型 / 变体设置五类 Token 单价
- USD / CNY 一键切换；默认汇率 `1 USD = 7.20 CNY`，可编辑
- 内部历史和 CSV 始终保存 USD，汇率只影响显示

## 截图

> 截图使用脱敏的代表性数据生成，展示了产品界面和交互，不包含本机用户名、API Key 或完整数据库路径。

### 总览：免费计划也能看清真实用量

![深色总览](docs/screenshots/overview-dark.png)

总览同时给出请求次数、含缓存读取总 Token、不含缓存读取 Token、缓存命中率和成本状态。底部堆叠条把输入、输出、推理、缓存读取、缓存写入分别显示出来。

### 趋势与告警

![完整趋势](docs/screenshots/overview-full.png)

长时间范围自动切换为按天趋势，Dashboard 还会显示今日档位、最近 30 分钟突增和精确价格覆盖率。

### 紧凑窗口也不丢关键数据

![紧凑总览](docs/screenshots/overview-compact.png)

窗口较矮或较窄时自动进入紧凑布局，供应商和模型排行并排出现，核心数值仍然优先可见。

### 模型级别统计

![模型统计](docs/screenshots/model-statistics.png)

模型统计页可以快速比较不同模型 / 变体的请求次数、Token 构成、缓存读取和成本来源。

### 价格设置

![价格设置](docs/screenshots/pricing-settings.png)

官方价格、倍率价格、完全自定义价格三种模式在同一窗口切换；所有自定义价格单位均为 `USD / 1M tokens`。

## 快速开始

### 下载已打包版本

从 [Releases](https://github.com/ausyeah/opencode-usage/releases/latest) 下载 `OpenCodeTokenMonitor-*-windows-x64.exe`：

1. 双击 EXE 安装到 `%LOCALAPPDATA%\\Programs\\OpenCodeTokenMonitor`
2. 启动 Dashboard
3. 托盘模式会在后台同步；也可以使用桌面快捷方式直接打开 Dashboard

程序不会要求你把 `oc_sk_` 或其他 OpenCode API Key 填入配置。它只读取本机 OpenCode 已经产生的数据。

### 从源码构建

需要 Windows、Python 3.13+ 和 Microsoft Edge WebView2 Runtime：

```powershell
git clone https://github.com/ausyeah/opencode-usage.git
cd opencode-usage
python -m pip install -r requirements.txt
.\build.ps1
```

构建产物：

```text
dist\OpenCodeTokenMonitor.exe
```

安装并保留已有配置：

```powershell
.\install.ps1
```

只安装、不立即启动托盘：

```powershell
.\install.ps1 -NoStart
```

运行测试：

```powershell
python -m unittest discover -s tests -q
```

## 使用方法

### Dashboard

启动后可以查看：

- **总览**：总量、请求次数、成本、缓存构成、趋势、供应商和模型排行
- **周期明细**：按小时 / 按天查看每个时间桶
- **供应商统计**：按供应商聚合
- **模型统计**：按供应商、模型、变体聚合
- **项目统计**：按项目聚合
- **请求日志**：搜索、排序最近请求，查看请求详情抽屉
- **告警记录**：每日档位、突增、错误和恢复记录

### 命令行

```text
OpenCodeTokenMonitor.exe dashboard
OpenCodeTokenMonitor.exe tray
OpenCodeTokenMonitor.exe status
OpenCodeTokenMonitor.exe status --json
OpenCodeTokenMonitor.exe history --days 30
OpenCodeTokenMonitor.exe sync
OpenCodeTokenMonitor.exe export
OpenCodeTokenMonitor.exe paths
OpenCodeTokenMonitor.exe test-alert
OpenCodeTokenMonitor.exe install
OpenCodeTokenMonitor.exe uninstall
```

不传参数或传入 `tray` 时启动托盘模式。

## 数据与隐私

默认数据目录：

```text
%LOCALAPPDATA%\\OpenCodeTokenMonitor
```

主要文件：

| 文件 | 用途 |
| --- | --- |
| `monitor.db` | 工具自己的永久统计数据库 |
| `config.json` | 本地配置与价格设置 |
| `monitor.log` | 运行日志 |
| `models.dev.all.json` | model.dev 价格目录缓存 |
| `csv\\usage_events.csv` | 每次模型请求的最终用量 |
| `csv\\usage_ledger.csv` | Token 变化流水 |
| `csv\\daily_summary.csv` | 按天汇总 |
| `csv\\provider_summary.csv` | 按天 + 供应商汇总 |
| `csv\\model_summary.csv` | 按天 + 供应商 + 模型汇总 |
| `csv\\project_summary.csv` | 按天 + 项目汇总 |
| `csv\\pricing_estimates.csv` | 账单、估算、价格来源与匹配状态 |

源数据库只读访问：

```text
%USERPROFILE%\\.local\\share\\opencode\\opencode.db
```

程序会先检测 `OpenCode.exe`、`opencode.exe` 或 `opencode-cli.exe` 是否运行。未运行时不会查询源数据库，也不会调用 OpenCode CLI 去探测数据。

## 价格口径

| 显示项 | 含义 |
| --- | --- |
| 账单成本 | 供应商事件中已经写入的非零 `cost` |
| 参考估算 | 账单为零或缺失时，根据 model.dev 进行本地估算 |
| 采用成本 | 优先使用账单；账单缺失时才使用估算 |
| 未定价 | 没有可靠价格，不伪装成 `$0` |

内置价格模式：

- `official`：model.dev 官方价格
- `multiplier`：官方参考价格 × 全局倍率
- `custom`：完全自定义，不自动回退官方目录

配置示例见 [`config.example.json`](config.example.json)。

## 项目结构

```text
opencode_token_monitor.py       后端、SQLite 索引、同步、计价、托盘、CLI、WebView2 启动
dashboard.html                  响应式 Dashboard、价格窗口、图表和交互
assets/app.ico                  应用图标
config.example.json             配置示例
install.ps1                     Windows 安装脚本
build.ps1                       PyInstaller 构建入口
OpenCodeTokenMonitor.spec        单文件 EXE 打包配置
tests/test_monitor.py           单元测试
docs/screenshots/               README 截图
```

## 开发检查

提交前建议运行：

```powershell
python -m py_compile .\opencode_token_monitor.py
python -m unittest discover -s tests -q
```

当前测试覆盖：

- OpenCode V1 / V2 数据库读取
- 增量同步与去重
- Token 统计和 CSV
- model.dev 价格与上下文阶梯
- 官方、倍率、自定义价格
- 配置参数和进程检测

## License

[MIT License](LICENSE)

本项目与 OpenCode、Sentinel Labs 或 Zen 计划没有隶属或背书关系。OpenCode、模型名称和相关服务归各自权利人所有。
