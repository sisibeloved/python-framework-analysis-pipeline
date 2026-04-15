# 环境搭建架构规范

## 1. 定位

第 3 步“环境搭建”不是单个安装脚本，而是一套可复用的环境准备子系统。

它要解决的问题是：

- 不同 Python 框架软件的部署方式不同，例如 PyFlink、PySpark、PyTorch 不应共用同一套部署脚本。
- 不同框架的测试方式不同，例如分布式作业、批处理作业、单机模型推理的 readiness check 不同。
- 远程连接、文件同步、依赖安装、Docker 安装、日志收集、运行记录这些能力有共通性，不能在每个框架里重复实现。
- 分析环境可能运行在隔离网络或内网机器上，流程必须允许“自动生成计划、人工执行关键命令、手动回填执行结果”。

因此第 3 步的目标不是追求全自动，而是让环境准备过程可描述、可审计、可复用、可验证。

## 2. 分层模型

环境搭建按三层拆分。

### 2.1 通用执行底座

通用执行底座只处理与具体框架无关的能力：

- 主机清单解析
- SSH 连接
- 远程命令执行
- 文件上传与下载
- 目录创建和权限检查
- 包管理器探测
- Python 版本探测
- Docker / container runtime 探测与安装
- 系统依赖安装
- 日志收集
- 执行记录归档
- 环境 readiness 结果归档

通用执行底座不能包含：

- PyFlink JobManager / TaskManager 语义
- PySpark driver / executor 语义
- PyTorch CUDA / ROCm / CPU 推理语义
- TPC-H 用例语义
- 任何具体框架的 benchmark 命令

### 2.2 框架适配层

框架适配层描述某个框架软件如何被部署、检查和测试。

每个框架适配器负责：

- 声明需要的主机角色
- 声明需要的系统依赖
- 声明需要的 Python 依赖
- 声明是否需要 Docker
- 声明源码构建方式
- 声明服务启动方式
- 声明 readiness check
- 声明 smoke test
- 声明性能采集前置条件

框架适配层不能包含：

- 具体机器 IP
- 私钥路径
- 内网镜像地址的明文凭据
- 单次运行的临时目录
- 某次实验的采集结果

### 2.3 项目实例层

项目实例层描述一次具体分析任务的环境。

项目实例层负责：

- 选择框架适配器
- 绑定目标机器
- 绑定平台标签，例如 `arm` / `x86`
- 指定软件版本
- 指定源码 revision
- 指定 Python 版本
- 指定依赖版本锁定文件
- 指定内网镜像源或包源的引用名称
- 指定输出目录
- 保存每次环境搭建的执行记录

项目实例层可以包含环境差异，但不能把差异藏在脚本里。所有会影响性能可比性的配置都必须显式记录。

## 3. 隔离边界

### 3.1 必须复用的能力

这些能力应沉淀在 `pipelines/pyframework_pipeline/environment/` 或等价通用模块中：

- SSH 执行器
- 本地执行器
- 文件同步器
- 命令结果模型
- 包管理器探测
- Docker 探测与安装计划生成
- Python 环境探测
- 操作系统信息采集
- CPU / 内存 / kernel 信息采集
- 日志归档
- 执行计划渲染
- dry-run

复用标准：如果 PyFlink、PySpark、PyTorch 都可能用到，就不应该写进某个框架 adapter。

### 3.2 必须隔离的能力

这些能力必须放在框架 adapter 或项目目录下：

- 框架安装方式
- 框架源码构建方式
- 框架服务启动方式
- 框架 readiness check
- 框架 smoke test
- 框架 benchmark 启动命令
- 测试用例入口
- 采集工具插桩位置
- 框架特有日志解析

隔离标准：如果换成另一个 Python 框架就需要重写，就不能进入通用执行底座。

### 3.3 必须不入库的内容

以下内容不能进入 git：

- SSH 私钥
- 明文密码
- 内网 token
- 真实业务数据
- 未脱敏的性能采集原始数据
- 大体积构建产物
- 远程机器上的临时目录快照

仓库内只允许保存 secret 引用名，例如 `secretRef: arm-lab-ssh-key`，由实际执行环境解析。

## 4. 仓库体现方式

建议目录结构：

```text
pipelines/pyframework_pipeline/
  environment/
    executors/
      local.py
      ssh.py
    installers/
      docker.py
      python.py
      system_packages.py
    probes/
      os.py
      cpu.py
      python.py
      docker.py
    planning.py
    records.py

  adapters/
    base.py
    pyflink/
      environment.py
    pyspark/
      environment.py
    pytorch/
      environment.py

projects/
  pyflink-tpch-reference/
    project.yaml
    environment.yaml
    runs/
      2026-04-15-arm/
        environment-plan.json
        environment-record.json
        readiness-report.json
      2026-04-15-x86/
        environment-plan.json
        environment-record.json
        readiness-report.json
```

目录职责：

- `environment/executors/` 只负责“在哪里执行命令”。
- `environment/installers/` 只负责“如何安装通用能力”。
- `environment/probes/` 只负责“如何采集环境事实”。
- `environment/planning.py` 负责把项目配置和框架 adapter 组装成执行计划。
- `environment/records.py` 负责把执行结果写成稳定记录。
- `adapters/<framework>/environment.py` 负责给出框架自己的环境需求和检查逻辑。
- `projects/<project>/environment.yaml` 负责绑定具体机器、版本和平台。
- `projects/<project>/runs/` 负责保存每次环境搭建的事实结果。

## 5. 串联方式

第 3 步的执行链路应为：

1. 读取 `project.yaml`，确认分析项目和框架类型。
2. 读取 `environment.yaml`，确认目标平台、主机、版本、部署模式和 secret 引用。
3. 加载对应框架 adapter，例如 `pyflink.environment.PyFlinkEnvironmentAdapter`。
4. adapter 输出框架环境需求。
5. 通用 planning 层把环境需求转换为 `environment-plan.json`，并生成 `planHash`。
6. 用户或 agent 以 dry-run 模式审查计划。
7. 在允许自动执行的环境中，executor 逐步执行计划。
8. 在隔离环境中，CLI 只生成命令清单，由用户手动执行后回填结果。
9. probes 采集环境事实，生成 `environment-record.json`。
10. adapter 运行 readiness check，生成 `readiness-report.json`。
11. 第 4 步“测试用例编写”和第 6 步“性能采集”只消费 readiness 通过的环境。

这条链路的关键是：自动化入口可以相同，但执行模式可以不同。

## 6. 执行模式

### 6.1 Full Auto

CLI 直接 SSH 到远程机器并执行安装、部署、检查。

适用场景：

- 可访问测试服务器
- 可使用 SSH key
- 包源和镜像源可访问
- 没有审批型命令

### 6.2 Plan Only

CLI 只生成环境计划和命令清单，不执行。

适用场景：

- 环境隔离
- 需要人工审批
- 目标机器只能由用户登录
- 需要先审查安装动作

### 6.3 Manual Record

用户手动执行命令后，把执行结果、版本信息、日志路径回填为环境记录。

适用场景：

- 内网机器不可直连
- 无法从本机拉取日志
- 有保密数据
- 执行过程需要人工干预

第一版必须优先支持 `Plan Only` 和 `Manual Record`，因为它们对隔离环境最友好，也能避免把部署权限问题变成工具阻塞。

## 7. 操作类型与权限边界

环境计划中的每一步都必须显式标注它是否会修改机器状态。

操作类型：

- `probe`：只读探测，例如 `uname -a`、`python --version`。
- `check`：只读校验，例如 `docker --version`、`python -c "import pyflink"`。
- `prepare`：创建目录、检查权限、下载只读资源。
- `install`：安装或升级依赖，会修改系统或用户环境。
- `configure`：写配置文件，会修改框架或服务配置。
- `start`：启动服务或容器。
- `stop`：停止服务或容器。
- `cleanup`：清理临时目录或进程。

计划步骤必须包含：

- `mutatesHost`：是否修改机器状态。
- `requiresPrivilege`：是否需要 sudo 或等价权限。
- `requiresApproval`：是否需要人工确认。
- `rollbackHint`：如果步骤失败或需要撤销，应如何处理。

第一版 CLI 在 `plan-only` 模式下不执行任何步骤；在未来的 `full-auto` 模式下，也必须默认拒绝执行 `requiresApproval: true` 的步骤，除非用户显式传入允许参数。

## 8. 主机能力模型

不要假设所有远程机器都具备同样能力。`environment.yaml` 应允许记录主机能力：

- `ssh`：是否允许 SSH 连接。
- `sudo`：是否允许提权。
- `docker`：是否允许 Docker 或兼容容器运行时。
- `internet`：是否能访问公网。
- `internalMirror`：是否能访问内网包源或镜像源。
- `upload`：是否允许从本机上传文件。
- `download`：是否允许从远程拉取日志或产物。

planning 层应根据能力生成不同计划。

例子：

- 没有 `internet` 但有 `internalMirror`，计划应使用内网源。
- 没有 `sudo`，计划不能生成系统级安装命令，只能生成用户级安装或人工前置要求。
- 没有 `download`，计划应要求用户手动把日志摘要回填到记录文件。

这部分属于项目实例层，因为不同实验室、不同机器、不同平台可能完全不同。

## 9. 项目环境配置草案

`projects/<project>/environment.yaml` 第一版建议只描述必要信息：

```yaml
schemaVersion: 1
framework: pyflink
mode: plan-only

platforms:
  - id: arm
    arch: aarch64
    hosts:
      - role: client
        hostRef: arm-client
      - role: taskmanager
        hostRef: arm-tm-01

  - id: x86
    arch: x86_64
    hosts:
      - role: client
        hostRef: x86-client
      - role: taskmanager
        hostRef: x86-tm-01

software:
  pythonVersion: "3.11"
  frameworkVersion: "pyflink-source-revision"
  dockerRequired: true
  dependencyLock: requirements.lock

hostRefs:
  arm-client:
    connect: ssh
    user: benchmark
    addressRef: arm-client-address
    secretRef: arm-lab-ssh-key
    capabilities:
      ssh: true
      sudo: false
      docker: true
      internet: false
      internalMirror: true
      upload: true
      download: false
  arm-tm-01:
    connect: ssh
    user: benchmark
    addressRef: arm-tm-01-address
    secretRef: arm-lab-ssh-key
    capabilities:
      ssh: true
      sudo: false
      docker: true
      internet: false
      internalMirror: true
      upload: true
      download: false
  x86-client:
    connect: ssh
    user: benchmark
    addressRef: x86-client-address
    secretRef: x86-lab-ssh-key
  x86-tm-01:
    connect: ssh
    user: benchmark
    addressRef: x86-tm-01-address
    secretRef: x86-lab-ssh-key
```

字段原则：

- `framework` 决定加载哪个 adapter。
- `mode` 决定是否真实执行远程命令。
- `platforms` 是性能对比的环境维度，必须显式。
- `hosts.role` 使用框架语义，但 host 连接细节在 `hostRefs`。
- `addressRef` 和 `secretRef` 只保存引用名，不保存敏感值。
- `software` 记录影响可比性的版本条件。
- `capabilities` 决定 planning 层生成自动执行、手动执行还是前置要求。

## 10. 环境计划输出草案

`environment-plan.json` 应是机器可读的执行计划：

```json
{
  "schemaVersion": 1,
  "projectId": "pyflink-tpch-reference",
  "framework": "pyflink",
  "platform": "arm",
  "mode": "plan-only",
  "planHash": "sha256:example",
  "steps": [
    {
      "id": "probe-os",
      "kind": "probe",
      "hostRef": "arm-client",
      "command": "uname -a && cat /etc/os-release",
      "required": true,
      "mutatesHost": false,
      "requiresPrivilege": false,
      "requiresApproval": false,
      "rollbackHint": "No rollback required."
    },
    {
      "id": "check-docker",
      "kind": "check",
      "hostRef": "arm-client",
      "command": "docker --version",
      "required": true,
      "mutatesHost": false,
      "requiresPrivilege": false,
      "requiresApproval": false,
      "rollbackHint": "No rollback required."
    },
    {
      "id": "install-framework",
      "kind": "framework-install",
      "hostRef": "arm-client",
      "command": "bash scripts/setup-pyflink.sh",
      "required": true,
      "mutatesHost": true,
      "requiresPrivilege": false,
      "requiresApproval": true,
      "rollbackHint": "Remove the configured PyFlink workspace and restore dependency lock."
    },
    {
      "id": "readiness",
      "kind": "framework-readiness",
      "hostRef": "arm-client",
      "command": "python scripts/check_pyflink_ready.py",
      "required": true,
      "mutatesHost": false,
      "requiresPrivilege": false,
      "requiresApproval": false,
      "rollbackHint": "No rollback required."
    }
  ]
}
```

计划中的 `kind` 用于区分通用步骤和框架步骤：

- `probe`：通用环境事实采集
- `system-install`：通用系统依赖安装
- `docker-install`：通用 Docker 安装或检查
- `python-install`：通用 Python 环境安装或检查
- `framework-install`：框架 adapter 输出
- `framework-start`：框架 adapter 输出
- `framework-readiness`：框架 adapter 输出
- `framework-smoke-test`：框架 adapter 输出

## 11. 环境记录输出草案

`environment-record.json` 记录实际发生了什么，不记录“应该发生什么”：

```json
{
  "schemaVersion": 1,
  "projectId": "pyflink-tpch-reference",
  "platform": "arm",
  "planHash": "sha256:example",
  "startedAt": "2026-04-15T10:00:00Z",
  "finishedAt": "2026-04-15T10:12:00Z",
  "mode": "manual-record",
  "provenance": {
    "recordedBy": "manual",
    "operatorRef": "benchmark-operator",
    "source": "isolated-lab"
  },
  "facts": {
    "arch": "aarch64",
    "kernel": "6.6.0",
    "python": "3.11.6",
    "docker": "25.0.0"
  },
  "steps": [
    {
      "id": "probe-os",
      "status": "passed",
      "exitCode": 0,
      "logPath": "runs/2026-04-15-arm/logs/probe-os.log"
    }
  ]
}
```

`readiness-report.json` 只回答一个问题：这个环境是否可以进入用例和采集阶段。

```json
{
  "schemaVersion": 1,
  "projectId": "pyflink-tpch-reference",
  "platform": "arm",
  "status": "ready",
  "checks": [
    {
      "id": "pyflink-import",
      "status": "passed",
      "message": "PyFlink can be imported by target Python."
    },
    {
      "id": "mini-job",
      "status": "passed",
      "message": "Minimal PyFlink batch job completed."
    }
  ]
}
```

记录校验必须检查：

- `environment-record.json.planHash` 是否等于 `environment-plan.json.planHash`。
- `steps[*].id` 是否都来自计划。
- `mutatesHost: true` 的步骤是否有执行状态、日志路径或人工说明。
- `readiness-report.json.status` 是否明确为 `ready`、`not-ready` 或 `unknown`。

如果是 `manual-record`，记录可以没有完整 stdout/stderr，但必须有 `provenance` 和人工说明，否则不能作为后续性能分析的可信环境依据。

## 12. PyFlink 的第一版落点

PyFlink adapter 第一版只需要声明：

- 角色：`client`、`jobmanager`、`taskmanager`
- 必需探测：OS、arch、CPU、kernel、Python、Java、Docker
- 必需依赖：Python、JDK、Flink/PyFlink 运行包
- readiness check：能 import PyFlink、能提交最小 batch job、能确认 TaskManager 执行成功
- smoke test：一个不含业务 UDF 的最小作业

不要在第一版实现：

- 自动部署完整 Flink 集群
- 自动生成 TPC-H 数据
- 自动提交正式性能用例
- 自动 perf 采集
- 自动解释失败原因

原因是这些动作都高度依赖真实环境，过早自动化会制造错误抽象。

## 13. 和后续步骤的关系

第 4 步“测试用例编写”依赖：

- `readiness-report.json.status == ready`
- 项目环境中的框架版本和 Python 版本

第 5 步“用例数据获取”依赖：

- 环境记录中的磁盘路径
- 数据生成工具是否可用
- 平台是否需要分别生成数据

第 6 步“性能采集数据获取”依赖：

- 环境记录中的主机角色
- readiness 通过的执行入口
- 采集工具是否已安装

第 7 步“数据回填框架”不应自动触发环境搭建，只消费已经归档的运行记录和采集产物。

## 14. 第一批实施任务

第一批只做可验证的基础能力：

1. 增加 `projects/pyflink-tpch-reference/environment.yaml` 示例。
2. 增加 `schemas/environment.schema.json`。
3. 增加 `schemas/environment-plan.schema.json`。
4. 增加 `schemas/environment-record.schema.json`。
5. 增加 `schemas/readiness-report.schema.json`。
6. 在 CLI 增加 `environment plan <project.yaml>`，只生成计划，不执行远程命令。
7. 在 CLI 增加 `environment validate <run-dir>`，校验人工回填记录。
8. 增加 PyFlink environment adapter，只输出最小 plan steps。
9. 增加测试，覆盖 plan-only 模式和 manual-record 模式。

这批任务完成后，才考虑 SSH 自动执行器。

## 15. 需要刻意避免的错误抽象

- 不要把 `ssh user@host command` 字符串散落在框架 adapter 中。
- 不要把 Docker 安装写成 PyFlink 专用能力。
- 不要假设所有框架都需要 Docker。
- 不要假设所有框架都有 client / worker 结构。
- 不要假设所有平台都能从当前机器 SSH 直连。
- 不要把环境搭建成功等同于框架 readiness 成功。
- 不要把用例执行和环境搭建揉在一起。
- 不要让 web 读取环境搭建中间态。
