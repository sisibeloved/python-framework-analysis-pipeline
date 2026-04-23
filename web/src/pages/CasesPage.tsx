import { DataTable, EmptyState, MetricCard, PageHeader, SectionCard } from "../components";
import { loadCaseIndex } from "../data/loaders";
import { useAsyncData } from "../hooks/useAsyncData";
import { Link } from "react-router-dom";

function parseSignedPercent(value: string) {
  return Number.parseFloat(value.replace("%", ""));
}

function renderAssetLink(id?: string) {
  return id ? <Link to={`/artifact/${id}`}>{id}</Link> : "—";
}

function renderAssetPairCard(entry: {
  id: string;
  name: string;
  scale?: string;
  semanticStatus?: string;
  pythonUdfMode?: string;
  sourceSqlArtifactId?: string;
  pythonUdfArtifactId?: string;
  notes?: string;
}) {
  return (
    <article key={entry.id} className="case-asset-pair-card">
      <div className="case-asset-pair-card__header">
        <div>
          <strong>
            <Link to={`/case/${entry.id}`}>{entry.name}</Link>
          </strong>
          <span>
            {entry.scale ?? "—"} · {entry.semanticStatus ?? "未标注"} · {entry.pythonUdfMode ?? "未标注"}
          </span>
        </div>
      </div>
      <div className="case-asset-pair-card__grid">
        <div className="case-asset-tile">
          <span className="case-asset-tile__label">SQL 基准</span>
          <strong>{renderAssetLink(entry.sourceSqlArtifactId)}</strong>
          <p>作为语义基准的原始 SQL 资产，供用例语义校验与比对使用。</p>
        </div>
        <div className="case-asset-tile">
          <span className="case-asset-tile__label">Python UDF 实现</span>
          <strong>{renderAssetLink(entry.pythonUdfArtifactId)}</strong>
          <p>将 SQL 语义整体改写到单个 Python UDF 后的实现入口。</p>
        </div>
      </div>
      <p className="case-asset-pair-card__note">{entry.notes ?? "—"}</p>
    </article>
  );
}

export default function CasesPage() {
  const state = useAsyncData(loadCaseIndex);

  if (state.loading) {
    return <EmptyState title="正在加载用例资产" message="正在从报告数据包读取用例清单。" />;
  }

  if (state.error || !state.data) {
    return <EmptyState title="用例资产不可用" message={state.error ?? "当前还没有可展示的用例资产。"} />;
  }

  const rankedByDemo = [...state.data].sort(
    (left, right) => parseSignedPercent(right.demoDelta) - parseSignedPercent(left.demoDelta),
  );
  const rankedByFramework = [...state.data].sort(
    (left, right) => parseSignedPercent(right.frameworkDelta) - parseSignedPercent(left.frameworkDelta),
  );
  const largestDemoGap = rankedByDemo[0];
  const largestFrameworkGap = rankedByFramework[0];

  return (
    <>
      <PageHeader
        title="用例资产中心"
        description="按用例编排 SQL 基准、Python UDF 实现和四类性能差异，作为后续下钻的资产入口。"
      />
      <section className="summary-band">
        <MetricCard title="覆盖用例数" primaryValue={String(state.data.length)} secondaryValue="当前 demo 示例包" />
        <MetricCard title="总耗时差异最大" primaryValue={largestDemoGap.name} secondaryValue={largestDemoGap.demoDelta} />
        <MetricCard title="框架差异最大" primaryValue={largestFrameworkGap.name} secondaryValue={largestFrameworkGap.frameworkDelta} />
        <MetricCard title="默认工作负载形态" primaryValue={state.data[0]?.workloadForm ?? "—"} secondaryValue="SQL 语义整体改写到单个 Python UDF" />
      </section>
      <SectionCard title="四类指标差异全景">
        <div className="case-metric-matrix">
          {state.data.map((entry) => {
            const metrics = [
              { label: "Demo", value: entry.demoDelta },
              { label: "TM", value: entry.tmDelta },
              { label: "业务算子", value: entry.operatorDelta },
              { label: "框架调用", value: entry.frameworkDelta },
            ];

            return (
              <article key={entry.id} className="case-metric-matrix__row">
                <div className="case-metric-matrix__meta">
                  <strong><Link to={`/case/${entry.id}`}>{entry.name}</Link></strong>
                  <span>{entry.scale ?? "—"} · {entry.semanticStatus ?? "未标注"} · {entry.pythonUdfMode ?? "未标注"}</span>
                </div>
                <div className="case-metric-matrix__bars">
                  {metrics.map((metric) => (
                    <div key={metric.label} className="case-metric-matrix__bar-row">
                      <span>{metric.label}</span>
                      <div className="case-metric-matrix__bar-wrap">
                        <div
                          className="case-metric-matrix__bar"
                          style={{ width: `${Math.min(100, Math.max(12, parseSignedPercent(metric.value) * 6))}%` }}
                        />
                        <strong>{metric.value}</strong>
                      </div>
                    </div>
                  ))}
                </div>
              </article>
            );
          })}
        </div>
      </SectionCard>
      <SectionCard title="SQL 与 Python UDF 成对资产入口">
        <div className="case-asset-pair-list">
          {state.data.map((entry) => renderAssetPairCard(entry))}
        </div>
      </SectionCard>
      <SectionCard title="用例差异明细">
        <DataTable
          columns={[
            { key: "id", header: "用例 ID", render: (row) => <Link to={`/case/${row.id}`}>{row.id}</Link> },
            { key: "name", header: "用例名称", render: (row) => <Link to={`/case/${row.id}`}>{row.name}</Link> },
            { key: "demo", header: "Demo 耗时", render: (row) => row.demoArm ? `${row.demoArm} (${row.demoDelta})` : row.demoDelta },
            { key: "tm", header: "TM 耗时", render: (row) => row.tmArm ? `${row.tmArm} (${row.tmDelta})` : row.tmDelta },
            { key: "operator", header: "业务算子", render: (row) => row.operatorArm ? `${row.operatorArm} (${row.operatorDelta})` : row.operatorDelta },
            { key: "framework", header: "框架调用", render: (row) => row.frameworkArm ? `${row.frameworkArm} (${row.frameworkDelta})` : row.frameworkDelta },
          ]}
          rows={state.data}
          getRowKey={(row) => row.id}
        />
      </SectionCard>
      <SectionCard title="用例资产编目">
        <DataTable
          columns={[
            { key: "name", header: "用例", render: (row) => <Link to={`/case/${row.id}`}>{row.name}</Link> },
            { key: "scale", header: "规模", render: (row) => row.scale ?? "—" },
            { key: "workloadForm", header: "工作负载形态", render: (row) => row.workloadForm ?? "—" },
            { key: "semanticStatus", header: "语义状态", render: (row) => row.semanticStatus ?? "—" },
            { key: "pythonUdfMode", header: "Python UDF 形态", render: (row) => row.pythonUdfMode ?? "—" },
            { key: "sourceSqlArtifactId", header: "SQL 资产", render: (row) => renderAssetLink(row.sourceSqlArtifactId) },
            { key: "pythonUdfArtifactId", header: "实现资产", render: (row) => renderAssetLink(row.pythonUdfArtifactId) },
            { key: "notes", header: "说明", render: (row) => row.notes ?? "—" },
          ]}
          rows={state.data}
          getRowKey={(row) => row.id}
        />
      </SectionCard>
    </>
  );
}
