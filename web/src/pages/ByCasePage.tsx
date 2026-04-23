import { DataTable, EmptyState, MetricCard, PageHeader, SectionCard, SplitPanel } from "../components";
import { loadCaseIndex } from "../data/loaders";
import { useAsyncData } from "../hooks/useAsyncData";
import { Link } from "react-router-dom";

function parseSignedPercent(value: string) {
  return Number.parseFloat(value.replace("%", ""));
}

export default function ByCasePage() {
  const state = useAsyncData(loadCaseIndex);

  if (state.loading) {
    return <EmptyState title="正在加载用例总览" message="正在读取用例对比数据。" />;
  }

  if (state.error || !state.data) {
    return <EmptyState title="用例总览不可用" message={state.error ?? "当前还没有可展示的用例总览数据。"} />;
  }

  const rankedByDemo = [...state.data].sort(
    (left, right) => parseSignedPercent(right.demoDelta) - parseSignedPercent(left.demoDelta),
  );
  const rankedByFramework = [...state.data].sort(
    (left, right) =>
      parseSignedPercent(right.frameworkDelta) - parseSignedPercent(left.frameworkDelta),
  );
  const largestDemoGap = rankedByDemo[0];
  const largestFrameworkGap = rankedByFramework[0];

  return (
    <>
      <PageHeader
        title="按用例分析总览"
        description="从用例视角查看热点与差异。"
      />
      <section className="summary-band">
        <MetricCard
          title="总耗时差异最大"
          primaryValue={largestDemoGap.name}
          secondaryValue={largestDemoGap.demoDelta}
        />
        <MetricCard
          title="框架差异最大"
          primaryValue={largestFrameworkGap.name}
          secondaryValue={largestFrameworkGap.frameworkDelta}
        />
        <MetricCard
          title="覆盖用例数"
          primaryValue={String(state.data.length)}
          secondaryValue="当前 demo 示例包"
        />
      </section>
      <SplitPanel className="split-panel--balanced">
        <SectionCard title="排序快照">
          <ol className="ranking-list">
            {rankedByFramework.map((entry) => (
              <li key={entry.id}>
                <Link to={`/case/${entry.id}`}>{entry.name}</Link>
                <span>{entry.frameworkDelta}</span>
              </li>
            ))}
          </ol>
        </SectionCard>
        <SectionCard title="如何解读">
          <ul className="highlight-list">
            <li>先看 Demo 差异，判断对外可见影响大小。</li>
            <li>再看框架差异，定位最能暴露 PyFlink 开销的用例。</li>
            <li>同时具备高 Demo 差异和高框架差异的用例，是最适合汇报展开的锚点。</li>
          </ul>
        </SectionCard>
      </SplitPanel>
      <SectionCard title="用例差异条带">
        <div className="delta-bar-list">
          {rankedByFramework.map((entry) => {
            const width = Math.min(100, Math.max(10, parseSignedPercent(entry.frameworkDelta) * 6));
            return (
              <div key={entry.id} className="delta-bar-list__row">
                <div>
                  <strong><Link to={`/case/${entry.id}`}>{entry.name}</Link></strong>
                  <p>总耗时 {entry.demoArm ?? entry.demoDelta} · TM {entry.tmArm ?? entry.tmDelta} · 业务算子 {entry.operatorArm ?? entry.operatorDelta}</p>
                </div>
                <div className="delta-bar-list__bar-wrap">
                  <div className="delta-bar-list__bar" style={{ width: `${width}%` }} />
                  <span>{entry.frameworkDelta}</span>
                </div>
              </div>
            );
          })}
        </div>
      </SectionCard>
      <SectionCard title="用例明细">
        <DataTable
          columns={[
            { key: "name", header: "用例", render: (row) => <Link to={`/case/${row.id}`}>{row.name}</Link> },
            { key: "demo", header: "总耗时", render: (row) => row.demoArm ? `${row.demoArm} (${row.demoDelta})` : row.demoDelta },
            { key: "tm", header: "TM", render: (row) => row.tmArm ? `${row.tmArm} (${row.tmDelta})` : row.tmDelta },
            { key: "operator", header: "业务算子", render: (row) => row.operatorArm ? `${row.operatorArm} (${row.operatorDelta})` : row.operatorDelta },
            { key: "framework", header: "框架调用", render: (row) => row.frameworkArm ? `${row.frameworkArm} (${row.frameworkDelta})` : row.frameworkDelta },
          ]}
          rows={state.data}
          getRowKey={(row) => row.id}
        />
      </SectionCard>
    </>
  );
}
