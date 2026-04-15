# 环境搭建执行手册

## 1. 目标

本手册描述第 3 步“环境搭建”的执行方式。当前阶段优先支持隔离环境，因此默认模式是生成计划、人工审查、人工执行、回填记录。

## 2. 推荐流程

1. 在项目目录准备 `project.yaml`。
2. 在项目目录准备 `environment.yaml`。
3. 运行环境计划生成命令。
4. 审查生成的 `environment-plan.json`。
5. 在目标机器上执行计划中的命令。
6. 把执行日志和环境事实回填到 `runs/<run-id>/`。
7. 运行环境记录校验。
8. readiness 通过后，才进入用例和采集阶段。

## 3. 目录约定

```text
projects/<project>/
  project.yaml
  environment.yaml
  runs/
    <run-id>/
      environment-plan.json
      environment-record.json
      readiness-report.json
      logs/
```

`run-id` 建议包含日期、平台和用途，例如：

- `2026-04-15-arm-env`
- `2026-04-15-x86-env`

## 4. 当前阶段命令形态

后续 CLI 应提供：

```bash
PYTHONPATH=pipelines python3 -m pyframework_pipeline environment plan projects/pyflink-tpch-reference/project.yaml
PYTHONPATH=pipelines python3 -m pyframework_pipeline environment validate projects/pyflink-tpch-reference/runs/2026-04-15-arm-env
```

第一条命令只生成计划，不连接远程机器。

第二条命令校验人工回填的执行记录和 readiness 报告。

## 5. 手工执行要求

隔离环境下手工执行计划时，需要保留以下信息：

- 执行的 `environment-plan.json` 及其 `planHash`。
- 每个步骤的状态：`passed`、`failed`、`skipped`。
- 会修改机器状态的步骤对应的日志路径或人工说明。
- 环境事实：arch、kernel、Python、Java、Docker 或替代运行时版本。
- readiness check 的明确结果。
- 操作来源：记录人、执行环境说明、是否允许拉取日志。

## 6. 验收标准

环境搭建阶段完成的最低标准：

- 两个平台都有独立的环境记录。
- 记录中包含 arch、kernel、Python、Docker 或替代运行时信息。
- 框架 readiness check 明确通过或失败。
- 环境记录中的 `planHash` 与环境计划一致。
- 所有影响性能可比性的版本差异都能在记录中看到。
- 没有敏感信息进入 git。
