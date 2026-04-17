# TPC-H PyFlink UDF 用例执行手册

## 1. 目标

本手册描述如何使用第 4 步"测试用例编写"产出的 TPC-H PyFlink UDF 用例。

## 2. 用例清单

当前已实现 13 条 UDF，覆盖 TPC-H 22 条 SQL 中的核心 8 条 + 可行扩展 5 条。

### 核心用例（8 条）

| Query | UDF 文件 | 表 | 返回类型 | 业务逻辑 |
|-------|---------|-----|---------|---------|
| Q1 | `q01.py` | lineitem | ROW<5 FLOAT> | 日期过滤 + 5 指标累加 |
| Q3 | `q03.py` | customer, orders, lineitem | FLOAT | 客户段过滤 + 折扣收入 |
| Q5 | `q05.py` | 6 表 JOIN | FLOAT | 区域过滤 + 折扣收入 |
| Q6 | `q06.py` | lineitem | FLOAT | 日期+折扣+数量过滤 |
| Q10 | `q10.py` | 4 表 JOIN | FLOAT | 退货标记 + 折扣收入 |
| Q12 | `q12.py` | orders, lineitem | ROW<2 INT> | 优先级分类 |
| Q14 | `q14.py` | lineitem, part | ROW<2 FLOAT> | 促销百分比 |
| Q19 | `q19.py` | lineitem, part | FLOAT | 3 分支 OR 匹配 |

### 扩展用例（5 条）

| Query | UDF 文件 | 表 | 返回类型 | 业务逻辑 |
|-------|---------|-----|---------|---------|
| Q4 | `q04.py` | orders, lineitem | INT | 日期过滤 + EXISTS 子查询 |
| Q9 | `q09.py` | 6 表 JOIN | FLOAT | LIKE 过滤 + 利润计算 |
| Q13 | `q13.py` | customer, orders | INT | NOT LIKE 过滤 + LEFT JOIN |
| Q18 | `q18.py` | customer, orders, lineitem | FLOAT | 数量透传 + HAVING 子查询 |
| Q22 | `q22.py` | customer, orders | ROW<STRING, FLOAT> | 国家码提取 + NOT EXISTS |

### 未实施（9 条）

Q2, Q7, Q8, Q11, Q15, Q16, Q17, Q20, Q21 — 需要多阶段执行或关联子查询，暂不适合单 UDF 模式。

## 3. 目录结构

```text
workload/tpch/
  sql/                    # 22 条原始 TPC-H SQL（框架共享）
    q01.sql ~ q22.sql
  pyflink/                # PyFlink 实现
    runner.py             # 公共 runner
    udf/                  # 纯 Python UDF
      __init__.py
      q01.py, q03.py ~ q22.py
```

## 4. 前置条件

- Flink 集群已部署并运行（或使用本地 mini-cluster）
- TPC-H 数据已生成（dbgen 输出的 pipe-delimited .tbl 文件）
- Python 3.11+ 环境中已安装 pyflink

## 5. 执行方式

### 远程集群模式

```bash
cd workload/tpch/pyflink
python runner.py --query q06 --data /path/to/tpch/sf10 --cluster jobmanager:6123
```

### 本地 mini-cluster 模式

```bash
cd workload/tpch/pyflink
python runner.py --query q06 --data /path/to/tpch/sf10
```

## 6. UDF 接口约定

每个 UDF 文件导出：

- `udf_q{NN}(*args)` — 纯 Python 函数，不 import PyFlink
- `UDF_INPUTS` — 输入列名列表
- `UDF_RESULT_TYPE` — Flink SQL 返回类型字符串
- `SQL` — 使用该 UDF 的 Flink SQL 查询

示例：

```python
def udf_q06(shipdate, discount, quantity, extendedprice):
    if shipdate < '1994-01-01' or shipdate >= '1995-01-01':
        return None
    if discount < 0.05 or discount > 0.07:
        return None
    if quantity >= 24:
        return None
    return float(extendedprice * discount)

UDF_INPUTS = ['l_shipdate', 'l_discount', 'l_quantity', 'l_extendedprice']
UDF_RESULT_TYPE = 'FLOAT'
SQL = "SELECT SUM(udf_q06(...)) AS revenue FROM lineitem"
```

## 7. Runner 行为

1. 解析 `--query`、`--data`、`--cluster` 参数
2. 创建远程或本地执行环境
3. 只注册该 query 需要的表（`QUERY_TABLES` 映射）
4. 动态加载 `udf/{query}.py`，注册 UDF
5. 执行 SQL，打印结果

## 8. 验证

运行前应确认：

- UDF 函数可被 Python 直接调用（不依赖 PyFlink）
- UDF 结果与原始 SQL 语义一致（结果行数、关键字段值、聚合结果）
- Runner 能成功提交任务并返回结果

## 9. 后续扩展

- 在 `workload/tpch/` 下增加 `pyspark/` 目录，实现 PySpark 版 UDF
- 在 `workload/` 下增加其他 benchmark（例如 TPC-DS）
- 增加 timing 采集包装，对接第 5 步"数据采集"
