# pyframework_pipeline

Pipeline 自动化分析流程的实现代码，包括配置校验、环境部署、benchmark、采集、回填和 Issue 桥接。

流程步骤总览见项目根目录 [README.md](../../README.md#pipeline-流程总览)。

## 前置准备

- Python ≥ 3.10（无第三方依赖，全部使用标准库）
- 确保当前工作目录为仓库根目录 `python-framework-analysis-pipeline/`
- 远程主机需通过 SSH 可达，并在 `~/.ssh/config` 中配置好 Host 别名

## 配置远程环境

每个项目目录下有一个 `environment.yaml.example` 模板文件，定义了平台架构、Docker 集群拓扑、软件版本和主机引用。

```bash
# 1. 复制模板
cp projects/pyflink-tpch-reference/environment.yaml.example \
   projects/pyflink-tpch-reference/environment.yaml

# 2. 编辑 environment.yaml，将占位符替换为实际主机
#    - alias: 填 IP 地址（如 192.168.1.100）或 ~/.ssh/config 中的 Host 别名
#    - user / key / port: 可选，不填则使用 SSH config 默认值
```

`environment.yaml` 被 `.gitignore` 排除，不会提交到仓库。

配置完成后用 `config validate` 检查：

```bash
PYTHONPATH=pipelines python3 -m pyframework_pipeline config validate projects/pyflink-tpch-reference/project.yaml
```

## 运行方式

所有命令从仓库根目录执行：

```bash
# 查看帮助
PYTHONPATH=pipelines python3 -m pyframework_pipeline --help

# 校验项目配置
PYTHONPATH=pipelines python3 -m pyframework_pipeline config validate projects/pyflink-tpch-reference/project.yaml
```

> 如果遇到 `No module named pyframework_pipeline`，说明未在仓库根目录执行，或 `PYTHONPATH` 指向错误。可以改用绝对路径：
>
> ```bash
> PYTHONPATH=/path/to/python-framework-analysis-pipeline/pipelines python3 -m pyframework_pipeline --help
> ```

## 关键文件路径速查

**四层输入/输出** (Step 6 读写)：

```
examples/four-layer/pyflink-reference/
  datasets/tpch-on-pyflink-2026q2.dataset.json   ← cases, functions, stackOverview, componentDetails, categoryDetails
  sources/pyflink-reference-source.source.json    ← artifactIndex (含 inline ASM content), sourceAnchors
  projects/tpch-pyflink-reference.project.json    ← caseBindings, functionBindings
  frameworks/pyflink.framework.json               ← 分类体系、指标定义 (只读)
```

**运行产物目录** (Step 5→6 产生)：

```
projects/<project>/runs/<run-id>/
  pipeline-run.json                               ← 运行状态追踪
  <platform>/
    timing/timing-normalized.json                  ← Step 5a: wallClock, operator, framework per query
    perf/data/perf_records.csv                     ← Step 5b: 符号/分类/占比的 perf CSV
    perf/tables/category_summary.csv               ← CPython 分类汇总
    perf/tables/symbol_hotspots.csv                ← 热点符号
    asm/<arm64|x86_64>/<symbol>.s                  ← objdump 反汇编
    tm-stdout-tm1.log                              ← BENCHMARK_SUMMARY 原始输出
```

**前端展示数据** (Step 6 输出后需同步)：

```
web/public/examples/four-layer/pyflink-reference/
  datasets/tpch-on-pyflink-2026q2.dataset.json    ← 从四层目录 cp
  sources/pyflink-reference-source.source.json     ← 从四层目录 cp
  projects/tpch-pyflink-reference.project.json     ← 从四层目录 cp
```

## 运行前检查清单

每次运行前按此清单核对，避免重复踩坑：

| 检查项 | 确认方式 | 踩坑记录 |
|--------|---------|---------|
| `project.yaml` 中 `rows` 值 | `grep rows project.yaml` | §八: 默认 10M 但 yaml 里写的 1M，搞错导致结论全错 |
| ARM/x86 workload 代码一致 | `diff` 两端 `benchmark_runner.py` | §八: x86 多了 JSON I/O，慢 6.4x |
| ARM 用本地模式（不加 `--cluster`） | 检查运行脚本无 `--cluster` | 十一: ARM TM classpath 缺 flink-python JAR |
| x86 perf 二进制路径 | 用完整路径 `/usr/lib/linux-tools/.../perf` | 九: `/usr/bin/perf` wrapper 找不到内核对应工具 |
| 容器内 python 路径 | ARM: pyenv 全路径；x86: `/usr/local/bin/python3` | 14.4: 两平台 Python 安装方式不同 |
| `docker cp` 后文件权限 | `docker exec -u root ... chmod 644` | 十二: cp 后 root:root -rw-------，容器用户无法读 |
| perf 采样量 ≥ 5000 | 检查 `perf_records.csv` 行数 | 十: 1M 行仅 ~1000 样本分布不可信，用 10M |
| timing-normalized.json 用 `per_invocation_ns` | 检查 JSON 含该字段 | 15.2: 只有 `total_ns` 时回填会用累计值，框架耗时 111 万秒 |
| Backfill 后同步到 `web/public/` | `cp` 四层 JSON → `web/public/...` | 前端从 `web/public/` 加载，不从 `examples/` 读取 |
| 前端 dev server 端口 5173 被占 | `lsof -ti:5173 \| xargs kill -9` | 默认端口 5173，被占时 Vite 用 5174 导致看旧版本 |
| 改 `_format_*` 后同步改 `_parse_*` | 检查所有消费格式化字符串的代码 | 15.1: 改了输出为秒但解析还假定毫秒，totals 从 23136s 变 6.5s |
| perf self% 归一化用 `/ share_total` | 检查 `_build_*` 函数不做 `/ 100` | 16.2: Python worker 仅 0.42% CPU，`/100` 得到 6s（实际 775s） |
| 平台总耗时只用 `demo` 指标 | 检查 `_estimate_total_ms` 不 sum 重叠指标 | 16.1: demo+tm 双倍计算 |
| 修 `_build_*` 归一化时检查全部四函数 | components/categories/functions/component_details 全检查 | 16.3: 只改了前两个，componentDetails 遗漏导致详情页 2.25s |

## Pipeline CLI

当前 CLI 的第一步是配置获取和完整性校验。真实项目必须先通过 `config validate`，再进入远程环境、workload、benchmark、采集、回填和 Issue 桥接；配置不完整时 `run` 会在本地直接失败，不会连接 SSH 或修改远程 Docker 状态。

```bash
PYTHONPATH=pipelines python3 -m pyframework_pipeline --help
PYTHONPATH=pipelines python3 -m pyframework_pipeline config validate projects/pyflink-tpch-reference/project.yaml
PYTHONPATH=pipelines python3 -m pyframework_pipeline validate examples/four-layer/pyflink-reference
PYTHONPATH=pipelines python3 -m pyframework_pipeline validate projects/pyflink-tpch-reference/project.yaml
```

`config validate` 会检查 `project.yaml`、同目录 `environment.yaml`、四层输入目录、`workload.localDir`、`run.platforms`、平台 `hostRef`、`software.flinkPyflinkImages` 以及 `bridge` 配置。默认还会检查 `bridge.tokenEnvVar` 指向的环境变量是否存在，并拒绝明显占位 token；只验证桥接前流程时可加 `--skip-bridge-token`。

真实一键流程示例：

```bash
export PYFRAMEWORK_BRIDGE_TOKEN=<real-github-or-gitcode-token>
PYTHONPATH=pipelines python3 -m pyframework_pipeline run projects/pyflink-tpch-reference/project.yaml --yes
```

`run` 会按 Step 3→7 串起环境部署、workload 上传、benchmark、远程采集、本地解析、回填和 issue 发布。常用控制参数：

```bash
# 指定运行目录
PYTHONPATH=pipelines python3 -m pyframework_pipeline run projects/pyflink-tpch-reference/project.yaml --run-dir projects/pyflink-tpch-reference/runs/2026-04-20-e2e --yes

# 从失败步骤恢复，例如重新从采集阶段开始
PYTHONPATH=pipelines python3 -m pyframework_pipeline run projects/pyflink-tpch-reference/project.yaml --run-dir projects/pyflink-tpch-reference/runs/2026-04-20-e2e --resume-from 5b --yes

# 只跑到某一步之前，用于本地预检
PYTHONPATH=pipelines python3 -m pyframework_pipeline run projects/pyflink-tpch-reference/project.yaml --stop-before 3
```

如果 `--stop-before 7` 或更早，本次运行不会进入 Issue 桥接，因此 `run` 不要求 `PYFRAMEWORK_BRIDGE_TOKEN`。完整运行到 Step 7 时仍必须提供 token。

也可以逐阶段执行：

```bash
# Step 3: 环境
PYTHONPATH=pipelines python3 -m pyframework_pipeline environment plan projects/pyflink-tpch-reference/project.yaml --platform arm --output projects/pyflink-tpch-reference/runs/arm-env
PYTHONPATH=pipelines python3 -m pyframework_pipeline environment deploy projects/pyflink-tpch-reference/project.yaml --platform arm --plan projects/pyflink-tpch-reference/runs/arm-env/environment-plan.json --yes
PYTHONPATH=pipelines python3 -m pyframework_pipeline environment validate projects/pyflink-tpch-reference/runs/arm-env

# Step 4-5: workload、benchmark、远程采集和本地解析
PYTHONPATH=pipelines python3 -m pyframework_pipeline workload deploy projects/pyflink-tpch-reference/project.yaml --platform arm
PYTHONPATH=pipelines python3 -m pyframework_pipeline benchmark run projects/pyflink-tpch-reference/project.yaml --platform arm --run-dir projects/pyflink-tpch-reference/runs/2026-04-20-e2e
PYTHONPATH=pipelines python3 -m pyframework_pipeline collect run projects/pyflink-tpch-reference/project.yaml --platform arm --run-dir projects/pyflink-tpch-reference/runs/2026-04-20-e2e
PYTHONPATH=pipelines python3 -m pyframework_pipeline acquire all projects/pyflink-tpch-reference/project.yaml --platform arm --run-dir projects/pyflink-tpch-reference/runs/2026-04-20-e2e/arm

# Step 6-7: 回填和 Issue 桥接
PYTHONPATH=pipelines python3 -m pyframework_pipeline backfill run projects/pyflink-tpch-reference/project.yaml --arm-run-dir projects/pyflink-tpch-reference/runs/2026-04-20-e2e/arm --x86-run-dir projects/pyflink-tpch-reference/runs/2026-04-20-e2e/x86
PYTHONPATH=pipelines python3 -m pyframework_pipeline bridge publish projects/pyflink-tpch-reference/project.yaml --dry-run
PYTHONPATH=pipelines python3 -m pyframework_pipeline bridge fetch projects/pyflink-tpch-reference/project.yaml
```

环境计划会优先使用 `environment.yaml` 中的 `software.flinkPyflinkImages.<platform>`，例如 arm 使用 `flink-pyflink:2.2.0-py314-arm-final`，x86 使用 `flink-pyflink:2.2.0-py314-x86-final`。`software.flinkImage` 只作为未配置平台专属镜像时的 fallback。

环境部署命令是幂等的：镜像已存在时跳过 `docker pull`；容器已存在且镜像匹配时复用/启动；容器已存在但镜像不匹配时删除并按当前配置重建。
