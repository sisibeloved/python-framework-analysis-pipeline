# 前端骨架实现计划 — ✅ 已完成

> 状态：全部 7 个任务已完成（2026-04-24）。本文档保留作为历史记录。

## 目标

构建一个基于 `React + Vite + TypeScript` 的前端工作台骨架，用于承载 PyFlink 跨平台性能差异分析报告。

## 当前实现方向

应用采用客户端 SPA 形态，运行在 `web/` 目录下。页面通过 React Router 组织，数据从四层示例输入读取，再由组装层输出页面 view model。

首版前端聚焦以下能力：

- 路由与应用壳
- 基础设计系统
- 四层模型数据加载层
- 示例四层输入
- 首页、总览页、详情页的最小闭环

## 技术栈

- React
- Vite
- TypeScript
- React Router
- Vitest
- Testing Library

## 实施任务

### 任务 1：初始化前端工作区

输出：

- `web/package.json`
- `web/tsconfig*.json`
- `web/vite.config.ts`
- `web/index.html`
- `web/src/main.tsx`
- `web/src/App.tsx`
- 样式基础文件

目标：

- 让前端工程可安装、可运行、可构建

### 任务 2：建立路由与应用壳

输出：

- 侧边导航
- 顶部导航
- 主内容区
- 全局过滤栏
- 路由定义

目标：

- 先让所有主要路由具备稳定入口

### 任务 3：建立基础设计系统

输出：

- `PageHeader`
- `MetricCard`
- `SectionCard`
- `DataTable`
- `SplitPanel`
- `Tag`
- `EmptyState`

目标：

- 为后续页面提供统一表达方式

### 任务 4：建立数据加载层

输出：

- Project / Framework / Dataset / Source loader
- view model assembler
- artifact loader

目标：

- 保证前端只通过四层模型消费示例 JSON 数据

### 任务 5：准备示例数据包

输出：

- `frameworks/*.framework.json`
- `datasets/*.dataset.json`
- `sources/*.source.json`
- `projects/*.project.json`
- artifacts 示例

目标：

- 让前端不依赖真实内网数据也能演示

### 任务 6：接线首页与主要详情页

优先页面：

- 首页
- By Case
- By Stack
- Case Detail
- Function Detail
- Pattern Detail
- Root Cause Detail
- Artifact Detail
- Insights

目标：

- 建立从摘要到证据的完整闭环

### 任务 7：验证与说明

输出：

- README 运行说明
- 测试
- 构建验证

目标：

- 确保前端工程可交付、可继续扩展

## 验收标准

- `npm run test` 通过
- `npm run build` 通过
- 首页可进入主要 drill-down 页面
- 至少存在一条完整的：
  - 首页 -> case -> function -> pattern -> root cause -> artifact

## 后续增量方向

已完成：

- 强化首页与总览页的汇报表达
- 增加 Arm / x86 并排 artifact compare（函数详情 diff view）
- 扩充示例数据包（真实 ARM/x86 采集数据）

待推进：

- 补充更接近正式汇报场景的视觉层次
