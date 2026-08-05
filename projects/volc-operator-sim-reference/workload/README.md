# Volc Operator Sim workload

This reference keeps the target repository black-boxed at commit
`c0c52fd514510bc223d76767e55bcefbc190033c`. The adapter reads task definitions
from that checkout and writes all derived tasks under the Host-persistent
`/home/lxy/de_bench_full/operator-cache` mount; it never modifies the checkout.

`project.yaml` is the default six-modality test set. It selects one business
pipeline for each of text, video, image, audio, PDF, and AD from the pinned
upstream `target_14` group. `project.extended-14.yaml` preserves the complete
original 14-pipeline matrix as the extended test set. Explicit task selection
applies to Pipeline E2E as well as every operator-analysis scope, so the default
entry point does not run the other eight pipelines in the background.

The default video and audio inputs are Panda-70M and Common Voice respectively:

- `fixtures/canonical/panda_70m_video.lance`
- `fixtures/canonical/common_voice_audio.lance`

Their JSONL mirrors and metadata files use the same basename. These are
Host-persistent frozen inputs and must be prepared before the default test set
runs; the adapter does not silently fall back to the upstream MSR-VTT or
LibriSpeech fixtures. The extended test set has no input overrides and retains
the original 14-pipeline input contracts from the pinned target checkout.

For fair or scale runs, first add every downloadable dataset/model object to
`data-sources.json` with a verified SHA-256. The deploy preparation step
downloads and verifies those objects on each Host before the image or container
step begins. Large or license-gated datasets that cannot be represented by one
direct URL must be materialized and frozen under the paths above on the Host.

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

Run the extended 14-pipeline set explicitly with:

```bash
PYTHONPATH=pipelines python3 -m pyframework_pipeline run \
  projects/volc-operator-sim-reference/project.extended-14.yaml --stop-before 7
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
