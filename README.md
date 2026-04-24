# python-framework-analysis-pipeline

用于构建 Python 框架分析流程的仓库。

当前仓库内置的是一套面向 PyFlink 的参考实现，包括：

- 一组 PyFlink 专用分析规范
- 一套前端 demo
- 一份示例数据包
- 一套自动化分析流程的初始 CLI 骨架

仓库的目标不是长期停留在“单一 PyFlink 报告”，而是演进到一套可复用于不同 Python 框架的软件分析流程。

当前抽象方向已经明确为四层模型：

- `Framework`
- `Dataset`
- `Source`
- `Project`

其中：

- `Framework` 定义框架边界、分类体系和指标口径
- `Dataset` 定义用例、结果、热点、模式和根因
- `Source` 定义源码仓、revision、源码索引和附件索引
- `Project` 通过显式绑定表把前三者装配成一份可展示的具体分析项目

## 当前范围

当前仓库主要包含以下内容：

- PyFlink 框架耗时归属规范
- TPC-H SQL 到整体 Python UDF 工作负载与计时规范
- 热点函数、模式、根因与证据建模规范
- 四层抽象方案（Framework / Dataset / Source / Project）
- 报告数据 schema 规范与页面字段规范
- 完整的自动化分析 Pipeline CLI（环境部署→采集→回填→Issue 桥接）
- 前端报告应用（首页、Stack/Case drill-down、函数 diff view、Artifact 查看）

## 目录结构

- `docs/specs/`
  - PyFlink 专用规范
  - 四层抽象方案
  - 报告 schema 与页面规范
  - 环境搭建架构规范
- `docs/plans/`
  - 前端实现计划、自动化流程路线图与阶段性设计记录
- `docs/runbooks/`
  - 环境搭建执行手册
- `schemas/`
  - 四层输入和校验报告的 JSON Schema
- `pipelines/`
  - 自动化分析流程 CLI、校验器、步骤接口和框架适配器
- `projects/`
  - 真实分析项目的配置、采集产物和运行记录
- `workload/`
  - Benchmark 用例定义和框架专属 UDF 实现
  - `workload/tpch/sql/` — 22 条原始 TPC-H SQL
  - `workload/tpch/pyflink/` — 13 条 PyFlink 纯 Python UDF + 公共 runner
- `web/`
  - 前端 demo
- `web/public/examples/four-layer/`
  - 前端 demo 直接加载的四层示例输入
- `web/public/examples/four-layer/pyflink-reference/artifacts/`
  - 四层 `Source.artifactIndex` 引用的示例证据附件

## 后续方向

- 扩充更多 Python 框架的四层示例输入（PySpark / PyTorch）
- 强化首页与总览页的汇报表达（趋势对比、Top-N 排行）
- 补充更接近正式汇报场景的视觉层次与证据对比能力
- 根据更多实机数据收紧 JSON Schema

## Pipeline 流程总览

Pipeline 按 Step 1→7 串行执行。CLI 子命令均通过 `PYTHONPATH=pipelines python3 -m pyframework_pipeline <subcommand>` 调用，完整参数见 [`pipelines/pyframework_pipeline/README.md`](pipelines/pyframework_pipeline/README.md)。

### 流程步骤

| Step | 名称 | CLI 子命令 | 输入 | 输出 |
|------|------|-----------|------|------|
| **1** | 配置校验 | `config validate` | • `project.yaml`<br>• `environment.yaml`<br>• 四层目录、`workload/` | • 校验报告 (stdout) |
| **3** | 环境部署 | `environment plan`<br>`environment deploy` | • `project.yaml`<br>• `environment.yaml` | • `environment-plan.json`<br>• `environment-record.json` |
| **4** | Workload 部署 | `workload deploy` | • `workload/tpch/pyflink/` | • 容器内 `benchmark_runner`、UDF、JAR |
| **5a** | Benchmark | `benchmark run` | • 容器内 workload<br>• `project.yaml` (queries, rows) | • `timing-normalized.json`<br>• `tm-stdout-tm*.log` |
| **5b** | 数据采集 | `collect run` | • `perf-udf.data` | • `perf_records.csv`<br>• `perf-*.data`<br>• `*.s` (asm) |
| **5c** | Acquire 汇总 | `acquire all` | • S5a/S5b 全部产物 | • `acquisition-manifest.json` |
| **6** | Backfill 回填 | `backfill run` | • `timing-normalized.json`<br>• `perf_records.csv`<br>• `*.s`<br>• 四层 JSON | • `*.dataset.json`<br>• `*.source.json`<br>• `*.project.json` |
| **7** | Bridge 桥接 | `bridge publish`<br>`bridge fetch` | • 四层 JSON (functions, artifacts)<br>• `PYFRAMEWORK_BRIDGE_TOKEN` | • GitHub/GitCode Issues |

## 前端应用

前端工程位于 `web/`。

### 本地运行

```bash
cd web
npm install
npm run dev
```

### 构建

```bash
cd web
npm run build
```

### 数据来源

当前前端 demo 默认从 `web/public/examples/four-layer/pyflink-reference/` 读取四层示例输入。加载顺序是：

1. `Project`
2. `Framework`
3. `Dataset`
4. `Source`

`web/src/data/assembly.ts` 负责把四层输入组装成页面 view model。`web/src/data/loaders.ts` 只调用组装层，不再读取旧的 `summary/details` 数据包，也不再使用 mock fallback。

`web/public/examples/four-layer/pyflink-reference/artifacts/` 是示例附件目录；它必须通过 `Source.artifactIndex` 引用，页面不能直接硬编码附件路径。
