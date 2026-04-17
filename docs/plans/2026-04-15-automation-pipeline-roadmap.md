# Python 框架自动化分析流程路线图

## 1. 目标

本项目的长期目标是搭建一套面向 Python 框架类软件的自动化分析流程，而不是只维护一个交互式报告 demo。

流程需要覆盖从报告框架、数据格式、环境准备、用例编写、数据获取、性能采集、数据回填，到机器码差异分析和优化机会分析的完整链路。

当前状态：

- 第 1 步”制定报告框架”暂时完成。
- 第 2 步”制定数据格式”暂时完成。
- 数据格式后续应在拿到真实实机数据后重新审视。
- 第 3 步”环境搭建”PyFlink 真实部署已完成（2026-04-16，kunpeng ARM + zen5 x86 双集群验证通过）。E1-E9 全部实现，16 条 UT 全部通过。
- 第 4 步”测试用例编写”PyFlink 第一批已完成（13 条 UDF + 公共 runner + benchmark runner）。Benchmark 三段式计时已在 zen5 集群端到端验证。
- 第 5 步”数据采集”已完成。三个子步骤：timing/perf/asm，集成 python-performance-kits（git submodule），可选远程执行。
- 第 6 步”数据回填”已完成。从采集产物填充四层模型（Dataset.cases.metrics / stackOverview / functions / Source.artifactIndex / Project bindings）。
- 环境搭建文档已落地：部署手册 + Python 3.14 FAQ + benchmark 验证记录。

## 2. 总体流程

自动化分析流程按 7 个阶段组织：

1. 制定报告框架
2. 制定数据格式
3. 环境搭建
4. 测试用例编写
5. 数据采集（合并原"用例数据获取"和"性能采集数据获取"，含 timing/perf/asm 三个子步骤）
6. 数据回填框架
7. 差异分析 + 优化机会 + 验证路线（通过 Issue 桥接外网 LLM，合并原步骤 7 和 8）

其中第 1 步和第 2 步输出的是规范和接口；第 3 步到第 6 步输出的是可执行流水线；第 7 步通过 GitCode/GitHub Issue 桥接外网 LLM 服务进行分析。

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

  workload/
    tpch/
      sql/
      pyflink/
        runner.py
        udf/

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
- `acquire_data.py`
- `backfill_data.py`
- `analyze_asm_diff.py`
- `analyze_opportunities.py`

下一阶段优先实现：

- `validate_data_format.py`
- `setup_environment.py` 的接口骨架
- `generate_testcases.py` 的接口骨架
- `acquire_data.py` 的接口骨架
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

### `workload/`

放框架通用的 benchmark 用例定义和框架专属的 UDF 实现。

目录按 benchmark 和框架二级组织：

```text
workload/
  tpch/                  # TPC-H benchmark
    sql/                 # 原始 SQL（22 条，框架共享）
    pyflink/             # PyFlink 实现
      runner.py          # 公共 runner（attach 到集群）
      udf/               # 纯 Python UDF（13 条）
    # 后续可增加 pyspark/ 等其他框架
```

职责边界：

- `workload/` 只放用例本身，不放采集、回填、环境搭建逻辑。
- `sql/` 是框架共享的原始 benchmark 定义。
- 各框架目录互不依赖，可以独立运行。
- UDF 必须是纯 Python 实现，不 import 框架 API。

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

下一阶段聚焦第 3 步到第 6 步：

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

第 7 步和第 8 步暂时只做接口预留：

- 机器码差异分析先保留输入/输出契约，不做复杂自动判断。
- 优化机会与验证路线分析先保留数据结构，不做自动推理。

原因：

- 没有真实实机数据时，过早实现机器码差异分析容易固化错误假设。
- 优化机会分析需要依赖真实 pattern 和 root cause 的质量。
- 当前最重要的是让数据从采集结果稳定进入四层模型。

## 7. 第一批落地任务

第一批落地任务建议按以下顺序执行。

### 任务 1：建立 `schemas/` — ✅ 已完成

输出：

- `schemas/framework.schema.json`
- `schemas/dataset.schema.json`
- `schemas/source.schema.json`
- `schemas/project.schema.json`
- `schemas/validation-report.schema.json`

验收：

- 当前 `examples/four-layer/pyflink-reference/` 能通过 schema 校验。

### 任务 2：建立 `pipelines/` CLI 骨架 — ✅ 已完成

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

### 任务 3：建立 `projects/pyflink-tpch-reference/` — ✅ 已完成

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

### 任务 4：实现 `validate` — ✅ 已完成

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

### 第 4 步"测试用例编写" PyFlink 第一批 — ✅ 已完成

产出：

- `workload/tpch/sql/q01.sql ~ q22.sql` — 22 条原始 TPC-H SQL
- `workload/tpch/pyflink/udf/` — 13 条纯 Python UDF（Q1/Q3-Q6/Q9/Q10/Q12-q14/Q18/Q19/Q22）
- `workload/tpch/pyflink/runner.py` — 公共 runner，支持 attach 到远程 Flink 集群或本地 mini-cluster
- `workload/tpch/pyflink/benchmark_runner.py` — 框架开销 benchmark runner（三段式计时算子链 + datagen/blackhole + UDF 零改动 wrapper）

UDF 接口：每个文件导出 `udf_q{NN}` 纯 Python 函数 + `UDF_INPUTS` + `UDF_RESULT_TYPE` + `SQL` 元数据。

覆盖率：核心 8 条 + 可行扩展 5 条，不实施 9 条（Q2/Q7/Q8/Q11/Q15/Q16/Q17/Q20/Q21，需多阶段或关联子查询）。

Benchmark 验证：13 条全部通过 `--dry-run`；q06/q01 在 zen5 远程集群端到端通过（详见 `docs/runbooks/pyflink-python314-deployment.md` 第 7 节）。

### 任务 5：数据采集 — ✅ 已完成

产出：

- `pipelines/pyframework_pipeline/acquisition/` — 数据采集模块（timing/perf_profile/machine_code/manifest/ssh_executor/collectors）
- `schemas/acquisition-manifest.schema.json` — 采集清单 JSON Schema
- `vendor/python-performance-kits/` — git submodule，perf 数据分析流水线
- CLI 子命令：`acquire timing`、`acquire perf`、`acquire asm`、`acquire validate`、`acquire all`
- `environment.yaml` 增加 `profilingTools` 配置（perf/strace/objdump/gdb/readelf）
- PyFlink adapter 增加采集工具安装/校验 plan steps

三类数据采集：
1. **用例数据 (Timing)** — 解析 TM stdout 中的 `[BENCHMARK_SUMMARY]` JSON，计算 4 指标 × 3 归一化
2. **性能采集数据 (perf Profile)** — 调用 python-performance-kits 处理 perf.data，生成分类/热点 CSV
3. **机器码 (Machine Code)** — perf annotate 热点函数 + objdump 二进制反汇编

采集边界：含远程执行（可选），数据已存在时自动跳过。

测试：26 条 UT 全部通过（17 环境 + 9 采集）。

### 任务 6：实现 `backfill` — ✅ 已完成

产出：

- `pipelines/pyframework_pipeline/backfill/` 模块（5 个子模块）
- `backfill_timing` — 读取 arm+x86 timing-normalized.json，匹配 caseId，计算 delta
- `backfill_perf` — 读取 arm+x86 perf CSV，CATEGORY_MAP 映射，聚合 component/category/function
- `backfill_asm` — glob .s 文件，生成 Source.artifactIndex，diffView 骨架（含单平台降级）
- `binding_generator` — 从 Dataset+Source 生成 Project.caseBindings / functionBindings
- `pipeline.py` 编排器 — 加载四层 JSON → 子模块 → 写回
- CLI：`backfill run` / `backfill status` 子命令

验收：

- `backfill status` 能正确显示四层数据状态
- 从采集产物填充 Dataset.cases[].metrics（4 指标 × arm/x86/delta）
- Dataset.stackOverview（component/category 分解）和 Dataset.functions[]（热点函数）
- Source.artifactIndex[]（汇编 artifacts，支持单平台场景）
- Framework 层不被修改

### 任务 7：差异分析 + 优化机会 + 验证路线（Issue 桥接）— ✅ 已完成

通过 GitCode/GitHub Issue 桥接外网 LLM 服务，每个热点函数一个 issue。

产出：

- `pipelines/pyframework_pipeline/bridge/` 模块（7 个子模块）
- `issue_client.py` — IssueClient Protocol + factory
- `github_client.py` — GitHub REST API（Bearer auth、Link-header 分页）
- `gitcode_client.py` — GitCode API v5（PRIVATE-TOKEN auth）
- `issue_template.py` — issue body 生成（双平台/单平台降级/截断/分块检测）
- `comment_parser.py` — 结构化 Markdown 评论解析（总览→逐行分析→根因汇总→优化策略）
- `manifest.py` — BridgeManifest 持久化（issue 状态追踪）
- `analysis.py` — publish/fetch 编排（issue 发布 + 评论拉取 + Dataset 回填）
- `schemas/bridge-manifest.schema.json`
- CLI：`bridge publish`（含 `--dry-run`）/ `bridge fetch` / `bridge status` 子命令
- 28 条 UT 全部通过

验收：

- `bridge publish --dry-run` 生成符合格式的 issue body（提示词 + 源码 + objdump -S 机器码）
- `bridge publish` 在 GitCode/GitHub 创建 issue（含 label）
- `bridge fetch` 拉取评论并解析为 Dataset.diffView / patterns / rootCauses
- `bridge status` 显示 issue 创建和解析状态
- 单平台函数自动降级为单平台分析模式
- 超长汇编自动截断，分块检测就绪

## 8. 第一批暂不做

第一批不做以下内容：

- 自动下载或生成真实 TPC-H 数据
- 自动部署 PyFlink / PySpark / PyTorch 环境
- 多框架对比 UI
- 权限管理和远程执行编排

这些内容都等最小流程骨架稳定后再进入。

## 9. 第 3 步"环境搭建"实施计划 — ✅ 已完成

第 3 步"环境搭建"的架构设计已完成（见 `docs/specs/environment-setup-architecture-spec.md`），执行手册已完成（见 `docs/runbooks/environment-setup-workflow.md`）。

实施顺序遵循环境搭建规范中的"第一批实施任务"：

### 9.1 任务列表

| # | 任务 | 输出 | 依赖 | 状态 |
|---|------|------|------|------|
| E1 | 新增 `schemas/environment.schema.json` | environment 配置的 JSON Schema | 无 | ✅ |
| E2 | 新增 `schemas/environment-plan.schema.json` | 环境计划的 JSON Schema | 无 | ✅ |
| E3 | 新增 `schemas/environment-record.schema.json` | 环境记录的 JSON Schema | 无 | ✅ |
| E4 | 新增 `schemas/readiness-report.schema.json` | readiness 报告的 JSON Schema | 无 | ✅ |
| E5 | 新增 `projects/pyflink-tpch-reference/environment.yaml` | PyFlink 项目的环境配置 | E1 | ✅ |
| E6 | CLI 增加 `environment plan` 子命令 | 生成 `environment-plan.json`，不执行远程命令 | E1, E2 | ✅ |
| E7 | CLI 增加 `environment validate` 子命令 | 校验人工回填记录 | E3, E4 | ✅ |
| E8 | 增加 PyFlink environment adapter | 输出最小 plan steps | E6 | ✅ |
| E9 | 测试覆盖 | plan-only 模式 + manual-record 模式，16 条 UT 全部通过 | E6, E7, E8 | ✅ |

### 9.2 第一版边界

第一版只实现：

- `plan-only` 模式：生成计划，不执行远程命令
- `manual-record` 模式：校验人工回填的执行记录
- PyFlink adapter 的最小 plan steps

第一版不实现：

- SSH 自动执行器
- 自动部署 Flink 集群
- 自动生成 TPC-H 数据
- 自动 perf 采集

### 9.3 新增目录结构

```text
pipelines/pyframework_pipeline/
  environment/
    __init__.py
    planning.py            # 组装 project.yaml + adapter → plan
    records.py             # 校验 manual record
    probes/
      __init__.py
      os.py                # OS 信息采集模板
      cpu.py               # CPU 信息采集模板
    pyflink/
      environment.py       # PyFlink 环境 adapter（声明需求 + readiness steps）

schemas/
  environment.schema.json
  environment-plan.schema.json
  environment-record.schema.json
  readiness-report.schema.json

projects/pyflink-tpch-reference/
  environment.yaml          # 项目环境配置
```

### 9.4 CLI 命令形态

```bash
# 生成环境计划（plan-only，不执行远程命令）
PYTHONPATH=pipelines python3 -m pyframework_pipeline environment plan \
  projects/pyflink-tpch-reference/project.yaml \
  --platform arm \
  --output runs/2026-04-15-arm-env/

# 校验人工回填的环境记录
PYTHONPATH=pipelines python3 -m pyframework_pipeline environment validate \
  projects/pyflink-tpch-reference/runs/2026-04-15-arm-env/
```

## 10. 验收标准

第 0 步文档完成后，进入第一批落地任务前，应满足：

- 路线图清楚描述 7 步流程。
- 目录职责清楚区分 `docs / schemas / pipelines / projects / examples / web`。
- 第一批落地任务只覆盖第 3 步到第 6 步的骨架。
- 第 7 步通过 Issue 桥接外网 LLM，内网只负责 issue 创建和评论拉取。

第一批落地任务完成后，应满足：

- `schemas/` 存在并能校验当前四层示例。
- `pipelines/` 存在最小 CLI。
- `projects/pyflink-tpch-reference/project.yaml` 存在。
- `validate` 能发现跨层引用错误。
- `environment plan` 能为指定平台生成执行计划（含 profiling tools 安装步骤）。
- `environment validate` 能校验人工回填记录和 hash 一致性。
- 第 4 步产出 13 条纯 Python UDF 和公共 runner。
- 第 5 步 `acquire timing/perf/asm` 能采集三类数据，`acquire validate` 校验清单完整性。
- `backfill run` 能从 arm/x86 采集产物填充四层模型（Dataset.cases.metrics / stackOverview / functions / Source.artifactIndex / Project bindings）。
- `backfill status` 能显示四层数据当前状态。
- `web` 仍能读取四层输出并通过测试构建。
