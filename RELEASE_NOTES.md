# Release Notes

## v3.0.1 — 2026-09-25

### 修复

- 修复 Windows 托盘提示标题超过 128 个字符时导致 `pystray` 抛出 `ValueError` 的问题
- 托盘标题改为紧凑格式，并强制限制在 Windows Shell 安全长度内
- 增加托盘标题长度回归测试，避免后台同步线程因标题过长反复报错

## v3.0.0 — 2026-09-25

首个公开版本，面向 OpenCode Zen 免费计划的本地 Token 用量观察与成本分析。

### Highlights

- Windows 原生托盘 + WebView2 Dashboard
- OpenCode V1 / V2 本地数据库只读同步与增量去重
- 永久历史、今日 / 24 小时 / 7 天 / 30 天 / 90 天 / 全部 / 自定义日期
- 供应商、模型、变体和项目维度统计
- 请求次数、输入、输出、推理、缓存读写和含 / 不含缓存读取口径
- 账单、model.dev 参考估算、采用成本和未定价状态分离
- 官方价格、全局倍率、完全自定义价格
- USD / CNY 显示切换，CSV 和内部历史继续保存 USD
- 10M 每日档位告警、30 分钟突增告警、托盘颜色和开机启动
- 深色 / 浅色主题、宽屏铺满、低高度紧凑布局和响应式滚动
- CSV 导出、请求日志搜索排序和请求详情抽屉
- Windows 单文件 PyInstaller 构建

### Windows asset

- `OpenCodeTokenMonitor-v3.0.0-windows-x64.exe`

### Upgrade notes

- 这是首个公开 Release，直接下载 Windows 资产即可。
- 程序数据默认位于 `%LOCALAPPDATA%\\OpenCodeTokenMonitor`。
- 从源码构建需要 Microsoft Edge WebView2 Runtime。
