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

当前仓库当前主要包含以下内容：

- PyFlink 框架耗时归属规范
- TPC-H SQL 到整体 Python UDF 工作负载与计时规范
- 热点函数、模式、根因与证据建模规范
- 四层抽象方案
- 报告数据 schema 规范
- 报告页面字段规范
- 前端 demo 骨架与示例数据包
- 一套可继续抽象到其他 Python 框架的软件分析流程基础

## 目录结构

- `docs/specs/`
  - PyFlink 专用规范
  - 四层抽象方案
  - 报告 schema 与页面规范
- `docs/plans/`
  - 前端实现计划、自动化流程路线图与阶段性设计记录
- `schemas/`
  - 四层输入和校验报告的 JSON Schema 草案
- `pipelines/`
  - 自动化分析流程 CLI、校验器、步骤接口和框架适配器
- `projects/`
  - 真实分析项目的配置、采集产物和运行记录
- `web/`
  - 前端 demo
- `web/public/examples/four-layer/`
  - 前端 demo 直接加载的四层示例输入
- `web/public/examples/four-layer/pyflink-reference/artifacts/`
  - 四层 `Source.artifactIndex` 引用的示例证据附件

## 后续方向

- 根据真实实机数据回看并收紧 JSON Schema
- 打通环境搭建、用例生成、采集、回填的最小自动化闭环
- 扩充更多 Python 框架的四层示例输入
- 继续完善正式汇报 demo 的页面表达与证据对比能力

## Pipeline CLI

当前 CLI 先提供四层输入的跨引用校验，用来保证 `Framework / Dataset / Source / Project` 之间的绑定关系没有断链。

```bash
PYTHONPATH=pipelines python3 -m pyframework_pipeline --help
PYTHONPATH=pipelines python3 -m pyframework_pipeline validate examples/four-layer/pyflink-reference
PYTHONPATH=pipelines python3 -m pyframework_pipeline validate projects/pyflink-tpch-reference/project.yaml
```

第一版不引入外部 Python 依赖，`project.yaml` 只支持当前项目配置需要的简单 `key: value` 格式。

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
