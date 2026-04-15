# Python 框架自动化分析流程路线图

## 1. 目标

本项目的长期目标是搭建一套面向 Python 框架类软件的自动化分析流程，而不是只维护一个交互式报告 demo。

流程需要覆盖从报告框架、数据格式、环境准备、用例编写、数据获取、性能采集、数据回填，到机器码差异分析和优化机会分析的完整链路。

当前状态：

- 第 1 步“制定报告框架”暂时完成。
- 第 2 步“制定数据格式”暂时完成。
- 数据格式后续应在拿到真实实机数据后重新审视。
- 下一阶段重点进入第 3 步到第 7 步，先打通自动化流程骨架。

## 2. 总体流程

自动化分析流程按 9 个阶段组织：

1. 制定报告框架
2. 制定数据格式
3. 环境搭建
4. 测试用例编写
5. 用例数据获取
6. 性能采集数据获取
7. 数据回填框架
8. 机器码差异分析
9. 优化机会与验证路线分析

其中第 1 步和第 2 步输出的是规范和接口；第 3 步到第 7 步输出的是可执行流水线；第 8 步和第 9 步依赖真实数据质量，下一阶段只预留接口，不做重逻辑。

## 3. 当前已完成基础

当前仓库已经具备以下基础：

- 四层模型：`Framework / Dataset / Source / Project`
- PyFlink 参考四层示例输入
- 前端报告壳
- 页面 view model 组装层
- PyFlink 专用分析规范
- 报告数据 schema 规范
- 页面字段规范

当前前端已经完全从四层示例输入加载数据，不再依赖旧 `report-package` 数据入口。

## 4. 推荐目录结构

下一阶段建议把仓库整理成下面的结构：

```text
python-framework-analysis-pipeline/
  docs/
    specs/
    plans/
    runbooks/

  schemas/
    framework.schema.json
    dataset.schema.json
    source.schema.json
    project.schema.json
    validation-report.schema.json

  pipelines/
    pyframework_pipeline/
      __init__.py
      cli.py
      config.py
      models/
      validators/
      steps/
      adapters/

  projects/
    pyflink-tpch-reference/
      project.yaml
      framework/
      dataset/
      source/
      artifacts/
      runs/

  examples/
    four-layer/
      pyflink-reference/

  web/
```

## 5. 目录职责

### `docs/specs/`

放稳定规范，包括：

- 四层抽象
- 数据格式
- 页面字段
- 采集口径
- 机器码 diff 建模
- 优化机会建模

这些文档定义“应该是什么”。

### `docs/plans/`

放阶段计划和实施记录。

这些文档定义“下一阶段怎么做”。

### `docs/runbooks/`

放可执行操作手册，例如：

- 如何准备 PyFlink 分析环境
- 如何生成 TPC-H 数据
- 如何运行一次采集
- 如何回填四层数据
- 如何验收一份分析项目

这些文档定义“人或 agent 实际怎么跑”。

### `schemas/`

放机器可校验的数据契约。

第一批需要覆盖：

- `framework.schema.json`
- `dataset.schema.json`
- `source.schema.json`
- `project.schema.json`
- `validation-report.schema.json`

这些 schema 是后续 `validate` 命令和 CI 的基础。

### `pipelines/`

放自动化流程代码。

建议第一版使用 Python 实现 CLI，因为后续环境搭建、文件处理、采集结果解析和数据回填更适合放在 Python 工具链里。

目录建议：

- `cli.py`：命令入口
- `config.py`：项目配置加载
- `models/`：内部数据模型
- `validators/`：schema 与业务约束校验
- `steps/`：对应 9 个流程步骤
- `adapters/`：不同 Python 框架的差异化实现

### `pipelines/pyframework_pipeline/steps/`

每个文件对应一个流程阶段：

- `define_report.py`
- `validate_data_format.py`
- `setup_environment.py`
- `generate_testcases.py`
- `acquire_case_data.py`
- `collect_performance.py`
- `backfill_data.py`
- `analyze_asm_diff.py`
- `analyze_opportunities.py`

下一阶段优先实现：

- `validate_data_format.py`
- `setup_environment.py` 的接口骨架
- `generate_testcases.py` 的接口骨架
- `acquire_case_data.py` 的接口骨架
- `collect_performance.py` 的输出契约
- `backfill_data.py` 的最小闭环

### `pipelines/pyframework_pipeline/adapters/`

放不同框架的差异化逻辑。

建议结构：

```text
adapters/
  base.py
  pyflink/
    adapter.py
  pyspark/
    adapter.py
  pytorch/
    adapter.py
```

第一版只需要实现 `base.py` 和 `pyflink/adapter.py` 的最小接口。

### `projects/`

放真实分析项目，不放通用示例。

每个项目对应一次具体分析任务，例如：

```text
projects/
  pyflink-tpch-reference/
    project.yaml
    framework/
    dataset/
    source/
    artifacts/
    runs/
```

`projects/` 和 `examples/` 的区别：

- `examples/` 用于脱敏演示和测试。
- `projects/` 用于真实分析任务。

### `examples/`

保留公开、脱敏、可测试的四层示例输入。

当前 `examples/four-layer/pyflink-reference/` 是 PyFlink 参考示例，应继续作为测试 fixture 和前端 demo 的默认数据来源。

### `web/`

只做展示壳。

`web/` 不应承担：

- 环境搭建
- 用例生成
- 性能采集
- 原始数据解析
- 数据回填

`web/` 只消费四层输入和组装后的页面 view model。

## 6. 下一阶段边界

下一阶段聚焦第 3 步到第 7 步：

- 环境搭建
- 测试用例编写
- 用例数据获取
- 性能采集数据获取
- 数据回填框架

第 3 步“环境搭建”应先独立建模，不应直接写成某个框架的安装脚本。具体方案见 `docs/specs/environment-setup-architecture-spec.md`。

环境搭建的阶段性原则：

- SSH、文件同步、包管理器探测、Docker 探测、Python 探测、日志归档属于通用执行底座。
- 框架安装、框架启动、框架 readiness check、框架 smoke test 属于框架适配层。
- 具体机器、平台、secret 引用、版本锁定、运行记录属于项目实例层。
- 隔离环境优先支持 `Plan Only` 和 `Manual Record`，不要把远程自动执行作为第一版阻塞项。

第 8 步和第 9 步暂时只做接口预留：

- 机器码差异分析先保留输入/输出契约，不做复杂自动判断。
- 优化机会与验证路线分析先保留数据结构，不做自动推理。

原因：

- 没有真实实机数据时，过早实现机器码差异分析容易固化错误假设。
- 优化机会分析需要依赖真实 pattern 和 root cause 的质量。
- 当前最重要的是让数据从采集结果稳定进入四层模型。

## 7. 第一批落地任务

第一批落地任务建议按以下顺序执行。

### 任务 1：建立 `schemas/`

输出：

- `schemas/framework.schema.json`
- `schemas/dataset.schema.json`
- `schemas/source.schema.json`
- `schemas/project.schema.json`
- `schemas/validation-report.schema.json`

验收：

- 当前 `examples/four-layer/pyflink-reference/` 能通过 schema 校验。

### 任务 2：建立 `pipelines/` CLI 骨架

输出：

- `pipelines/pyframework_pipeline/cli.py`
- `pipelines/pyframework_pipeline/config.py`
- `pipelines/pyframework_pipeline/models/`
- `pipelines/pyframework_pipeline/validators/`
- `pipelines/pyframework_pipeline/steps/`
- `pipelines/pyframework_pipeline/adapters/`

验收：

- 可以运行 `python -m pyframework_pipeline --help`
- 可以运行 `python -m pyframework_pipeline validate <path>`

### 任务 3：建立 `projects/pyflink-tpch-reference/`

输出：

- `projects/pyflink-tpch-reference/project.yaml`
- `projects/pyflink-tpch-reference/framework/`
- `projects/pyflink-tpch-reference/dataset/`
- `projects/pyflink-tpch-reference/source/`
- `projects/pyflink-tpch-reference/artifacts/`
- `projects/pyflink-tpch-reference/runs/`

验收：

- `project.yaml` 能描述当前 PyFlink 参考项目。
- 后续 CLI 能从该配置定位四层输入和输出目录。

### 任务 4：实现 `validate`

输出：

- schema 校验
- 基础业务约束校验
- validation report

第一版业务约束：

- `Project.frameworkRef` 必须能找到 `Framework`
- `Project.datasetRef` 必须能找到 `Dataset`
- `Project.sourceRef` 必须能找到 `Source`
- `functionBindings[*].functionId` 必须存在于 `Dataset.functions`
- `functionBindings[*].sourceAnchorIds` 必须存在于 `Source.sourceAnchors`
- `functionBindings[*].armArtifactIds` / `x86ArtifactIds` 必须存在于 `Source.artifactIndex`
- `Dataset.stackOverview.categories[*].topFunctionId` 必须存在于 `Dataset.functions`

验收：

- 当前 PyFlink 示例校验通过。
- 人为删掉一个 artifact 引用时，校验失败并指出缺失对象。

### 任务 5：定义采集输出中间格式

输出：

- timing raw 格式
- stack overview raw 格式
- hotspot raw 格式
- artifact manifest raw 格式

验收：

- 能说明 `collect_performance` 输出如何进入 `backfill_data`。

### 任务 6：实现最小 `backfill`

输出：

- 从一个输入目录读取 raw / manifest
- 生成四层 `Dataset / Source / Project` 的最小增量
- 保持 `Framework` 不被采集步骤污染

验收：

- 能从 PyFlink 示例输入重新生成一个可被 web 读取的四层输出目录。

## 8. 第一批暂不做

第一批不做以下内容：

- 自动下载或生成真实 TPC-H 数据
- 自动部署 PyFlink / PySpark / PyTorch 环境
- 自动运行 perf / flamegraph / objdump
- 自动判断机器码根因
- 自动生成优化建议
- 多框架对比 UI
- 权限管理和远程执行编排

这些内容都等最小流程骨架稳定后再进入。

## 9. 验收标准

第 0 步文档完成后，进入第一批落地任务前，应满足：

- 路线图清楚描述 9 步流程。
- 目录职责清楚区分 `docs / schemas / pipelines / projects / examples / web`。
- 第一批落地任务只覆盖第 3 步到第 7 步的骨架。
- 第 8 步和第 9 步只预留接口，不做虚假的自动分析。

第一批落地任务完成后，应满足：

- `schemas/` 存在并能校验当前四层示例。
- `pipelines/` 存在最小 CLI。
- `projects/pyflink-tpch-reference/project.yaml` 存在。
- `validate` 能发现跨层引用错误。
- `backfill` 有最小可执行闭环。
- `web` 仍能读取四层输出并通过测试构建。
