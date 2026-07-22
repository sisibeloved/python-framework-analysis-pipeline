# Volc Operator Sim workload

This reference keeps the target repository black-boxed at commit
`56d3b6856895427a0519cbaa437d55443fcb578b`. The adapter reads task definitions
from that checkout and writes all derived tasks under the Host-persistent
`/home/lxy/de_bench_full/operator-cache` mount; it never modifies the checkout.

The initial acceptance uses `core_dual_engine`, `smoke`, and one pipeline round.
The target's minimal text/image/audio/video fixtures are generated into
`/home/lxy/de_bench_full/fixtures`; the builder's temporary raw media is exposed
at `raw/min_fixtures` through a nested Host mount, so both canonical inputs and
their source files survive container replacement. For
fair or scale runs, first add every dataset/model object to `data-sources.json`
with a verified SHA-256; the deploy preparation step downloads and verifies
those objects on each Host before the image or container step begins.

Typical flow:

```bash
PYTHONPATH=pipelines python3 -m pyframework_pipeline config validate \
  projects/volc-operator-sim-reference/project.yaml --skip-bridge-token

PYTHONPATH=pipelines python3 -m pyframework_pipeline environment plan \
  projects/volc-operator-sim-reference/project.yaml --platform arm

PYTHONPATH=pipelines python3 -m pyframework_pipeline environment deploy \
  projects/volc-operator-sim-reference/project.yaml --platform arm

PYTHONPATH=pipelines python3 -m pyframework_pipeline environment deploy \
  projects/volc-operator-sim-reference/project.yaml --platform x86

PYTHONPATH=pipelines python3 -m pyframework_pipeline run \
  projects/volc-operator-sim-reference/project.yaml --stop-before 7
```

完整流程会在 `5c` 采集汇总后执行 `5c.1 operator readable reports`。也可以只用
已有本地证据重建单个平台的报告，不会重跑容器、数据集处理或 perf：

```bash
PYTHONPATH=pipelines python3 -m pyframework_pipeline operator report \
  projects/volc-operator-sim-reference/project.yaml \
  --platform arm \
  --run-dir projects/volc-operator-sim-reference/runs/<run-id>
```

入口为 `<run-dir>/arm/operators/operator-report.html`；每个 pipeline 的自包含
HTML 和 Markdown 位于 `operators/reports/`。报告将每个算子的 pipeline 内 wall
time、E2E 占比、隔离运行统计和 perf CPU period 分布放在同一页。perf 中显示的
估算 CPU 时间来自“隔离进程 CPU 总时间 × period 占比”，不是可与 E2E 相加的
额外 wall time；缺少逐算子边界时显示“不可归因”，不会显示为 0。

Smoke output is diagnostic only. Upstream currently exposes
`unblock_perf=false`, so even grade-A smoke evidence must not be presented as a
formal cross-architecture performance conclusion.

## Frozen 10K text-vectorize input

`project.text-vectorize-10k.yaml` runs the upstream
`pipeline_text_vectorize_full_min` task against a Host-persistent FineWeb
subset without replacing its canonical 32-row fixture.  The subset is the
first 10,000 non-empty `text` values encountered after lexicographically
sorting the Parquet shards under
`raw/fineweb_edu/sample/10BT`.  Its frozen authority is
`fixtures/text/scale/fineweb_edu_vectorize_10k_p4.manifest.json`; the JSONL
content SHA-256 is
`7475aaa2659f66ee4ca085c6178173db37f95c9f22d02e7b9c340bf71012d8ca`.

The Lance input and every derived operator snapshot are physically kept at
four fragments. Snapshot builder v4 reads the source manifest's
`partition_spec.fragments`, rewrites a collapsed intermediate result when
needed, records the observed fragment count, and rejects the cache unless the
input/output counts match. P0, P1 context, isolated timing, and perf all consume
task overlays derived from the same frozen input. This is a reproducible
diagnostic-scale run; the upstream formal scale threshold remains unset.
