import type {
  ArtifactDetail,
  ExecutiveSummary,
  OpportunityRankingEntry,
  PatternDetail,
  PatternIndexEntry,
  RootCauseDetail,
  RootCauseIndexEntry,
  ScopeSummary,
  CaseIndexEntry,
  ComponentDetail,
  CategoryDetail,
  CaseDetail,
  CaseHotspot,
  FunctionDetail,
  StackOverview,
} from "../types/report";

type ProjectCaseBinding = {
  caseId: string;
  primaryArtifactIds?: string[];
  notes?: string;
};

type ProjectFunctionBinding = {
  functionId: string;
  sourceAnchorIds?: string[];
  armArtifactIds?: string[];
  x86ArtifactIds?: string[];
};

type ProjectDefinition = {
  id: string;
  frameworkRef?: string;
  datasetRef?: string;
  sourceRef?: string;
  caseBindings?: ProjectCaseBinding[];
  functionBindings?: ProjectFunctionBinding[];
};

type FrameworkDefinition = {
  id: string;
  name?: string;
  analysisScope?: string[];
  excludedScope?: string[];
  metricDefinitions?: string[];
  taxonomy?: {
    components?: string[];
    categoriesL1?: string[];
  };
  metricGuardrails?: {
    unknownWarnThreshold?: string;
  };
};

type DatasetCase = {
  id: string;
  legacyCaseId?: string;
  name: string;
  implementationForm?: string;
  semanticStatus?: string;
  semanticNotes?: string;
  knownDeviations?: string[];
  artifactIds?: string[];
  hotspots?: CaseHotspot[];
  rootCauses?: string[];
  patterns?: string[];
  metrics?: {
    demoDelta?: string;
    tmDelta?: string;
    operatorDelta?: string;
    frameworkDelta?: string;
    demo?: { arm?: string; x86?: string; delta?: string };
    tm?: { arm?: string; x86?: string; delta?: string };
    operator?: { arm?: string; x86?: string; delta?: string };
    framework?: { arm?: string; x86?: string; delta?: string };
  };
};

type DatasetFunction = {
  id: string;
  symbol: string;
  component?: string;
  categoryL1?: string;
  categoryL2?: string;
  caseIds?: string[];
  patternIds?: string[];
  artifactIds?: string[];
  metrics?: {
    selfArm?: string;
    selfX86?: string;
    totalArm?: string;
    totalX86?: string;
    armShare?: string;
    x86Share?: string;
    delta?: string;
    deltaContribution?: string;
  };
  callPath?: string[];
  diffView?: FunctionDetail["diffView"];
};

type DatasetStackOverview = Omit<StackOverview, "categories"> & {
  categories: Array<Omit<StackOverview["categories"][number], "topFunction"> & { topFunctionId?: string }>;
};

type DatasetCategoryDetail = {
  id: string;
  name: string;
  level: string;
  parentCategoryId?: string;
  componentIds: string[];
  armTime: string;
  x86Time: string;
  armShare?: string;
  x86Share?: string;
  delta: string;
  deltaContribution?: string;
  caseIds: string[];
  hotspotIds: string[];
  patternIds: string[];
  artifactIds: string[];
};

type DatasetComponentDetail = {
  id: string;
  name: string;
  armTime: string;
  x86Time: string;
  armShare?: string;
  x86Share?: string;
  delta: string;
  deltaContribution?: string;
  categories: Array<{
    id: string;
    name: string;
    delta: string;
  }>;
  hotspotIds: string[];
  patternIds: string[];
  rootCauseIds: string[];
  artifactIds: string[];
};

type DatasetDefinition = {
  id: string;
  cases?: DatasetCase[];
  functions?: DatasetFunction[];
  stackOverview?: DatasetStackOverview;
  categoryDetails?: DatasetCategoryDetail[];
  componentDetails?: DatasetComponentDetail[];
  patterns?: PatternDetail[];
  rootCauses?: RootCauseDetail[];
  opportunities?: OpportunityRankingEntry[];
};

type SourceAnchor = {
  id: string;
  label: string;
  location: string;
  role?: string;
  snippet?: string;
};

type SourceDefinition = {
  id: string;
  sourceAnchors?: SourceAnchor[];
  artifactIndex?: Array<Omit<ArtifactDetail, "content">>;
};

export type AssemblyContext = {
  project: ProjectDefinition;
  framework: FrameworkDefinition;
  dataset: DatasetDefinition;
  source: SourceDefinition;
};

function getFourLayerBasePath(kind: "frameworks" | "datasets" | "sources" | "projects", id: string) {
  const encoded = encodeURIComponent(id);
  const suffix =
    kind === "frameworks"
      ? ".framework.json"
      : kind === "datasets"
        ? ".dataset.json"
        : kind === "sources"
          ? ".source.json"
          : ".project.json";

  return `/examples/four-layer/pyflink-reference/${kind}/${encoded}${suffix}`;
}

async function loadJson<T>(path: string): Promise<T> {
  const response = await fetch(resolvePublicPath(path));
  if (!response.ok) {
    throw new Error(`Failed to load JSON from ${path}`);
  }

  return (await response.json()) as T;
}

function resolvePublicPath(path: string) {
  if (!path.startsWith("/")) {
    return path;
  }

  return new URL(path, globalThis.location?.origin ?? "http://localhost").toString();
}

export async function loadProject(projectId: string): Promise<ProjectDefinition> {
  return loadJson<ProjectDefinition>(getFourLayerBasePath("projects", projectId));
}

export async function loadFramework(frameworkId: string): Promise<FrameworkDefinition> {
  return loadJson<FrameworkDefinition>(getFourLayerBasePath("frameworks", frameworkId));
}

export async function loadDataset(datasetId: string): Promise<DatasetDefinition> {
  return loadJson<DatasetDefinition>(getFourLayerBasePath("datasets", datasetId));
}

export async function loadSource(sourceId: string): Promise<SourceDefinition> {
  return loadJson<SourceDefinition>(getFourLayerBasePath("sources", sourceId));
}

export async function loadAssemblyContext(projectId: string): Promise<AssemblyContext> {
  const project = await loadProject(projectId);
  if (!project.frameworkRef || !project.datasetRef || !project.sourceRef) {
    throw new Error(`Project ${projectId} is missing frameworkRef/datasetRef/sourceRef`);
  }
  const [framework, dataset, source] = await Promise.all([
    loadFramework(project.frameworkRef),
    loadDataset(project.datasetRef),
    loadSource(project.sourceRef),
  ]);

  return {
    project,
    framework,
    dataset,
    source,
  };
}

export function assembleCaseIndex(ctx: AssemblyContext): CaseIndexEntry[] {
  const bindingsByCaseId = new Map(
    (ctx.project.caseBindings ?? []).map((binding) => [binding.caseId, binding]),
  );

  return (ctx.dataset.cases ?? []).map((entry) => {
    const binding = bindingsByCaseId.get(entry.id);
    const [sourceSqlArtifactId, pythonUdfArtifactId] = binding?.primaryArtifactIds ?? [];

    return {
      id: entry.id,
      name: entry.name,
      demoDelta: entry.metrics?.demoDelta ?? "-",
      tmDelta: entry.metrics?.tmDelta ?? "-",
      operatorDelta: entry.metrics?.operatorDelta ?? "-",
      frameworkDelta: entry.metrics?.frameworkDelta ?? "-",
      demoArm: entry.metrics?.demo?.arm,
      demoX86: entry.metrics?.demo?.x86,
      tmArm: entry.metrics?.tm?.arm,
      tmX86: entry.metrics?.tm?.x86,
      operatorArm: entry.metrics?.operator?.arm,
      operatorX86: entry.metrics?.operator?.x86,
      frameworkArm: entry.metrics?.framework?.arm,
      frameworkX86: entry.metrics?.framework?.x86,
      workloadForm: entry.implementationForm,
      semanticStatus: entry.semanticStatus,
      sourceSqlArtifactId,
      pythonUdfArtifactId,
    };
  });
}

function parseMs(value: string | undefined): number {
  if (!value) return 0;
  return Number.parseFloat(value.replace("ms", "").replace("s", "").replace("%", "").trim()) || 0;
}

function formatMsDelta(arm: string, x86: string): string {
  const delta = parseMs(arm) - parseMs(x86);
  const sign = delta >= 0 ? "+" : "";
  return `${sign}${delta.toFixed(1)} ms`;
}

function getMetricDefinition(id: string) {
  switch (id) {
    case "demo_total_time":
      return {
        name: "Demo 总耗时",
        definition: "从客户端提交作业到作业完成的总时间。",
        boundary: "Client -> Job 完成",
        normalization: "批量总量",
      };
    case "tm_end_to_end_time":
      return {
        name: "TM 端到端耗时",
        definition: "TaskManager 侧 SubTask 开始到结束的时间。",
        boundary: "SubTask 开始 -> 结束",
        normalization: "批量总量",
      };
    case "business_operator_time":
      return {
        name: "业务算子耗时",
        definition: "Python UDF 内部执行时间。",
        boundary: "Python UDF start -> Python UDF end",
        normalization: "按调用、按记录",
      };
    case "framework_call_time":
      return {
        name: "框架调用耗时",
        definition: "围绕 Python UDF 边界的包装与桥接开销。",
        boundary: "Java PreUDF -> Java PostUDF 减去 Python UDF 时间",
        normalization: "按调用、按记录",
      };
    default:
      return {
        name: id,
        definition: "由四层示例组装得到的指标定义。",
        boundary: "-",
        normalization: "-",
      };
  }
}

export function assembleExecutiveSummary(ctx: AssemblyContext): ExecutiveSummary {
  const totals = ctx.dataset.stackOverview?.platformTotals ?? { arm: "-", x86: "-" };
  const topComponent = ctx.dataset.stackOverview?.components?.[0];
  const topCategory = ctx.dataset.stackOverview?.categories?.[0];
  const topPattern = ctx.dataset.patterns?.[0];
  const topRootCause = ctx.dataset.rootCauses?.[0];

  return {
    title: "执行摘要",
    subtitle: `${ctx.framework.name ?? ctx.framework.id} 参考项目的四层组装摘要`,
    metrics: [
      {
        label: "总体栈耗时",
        armValue: totals.arm,
        x86Value: totals.x86,
        delta: formatMsDelta(totals.arm, totals.x86),
        target: "/analysis/by-stack",
      },
      {
        label: "主导组件差异",
        armValue: topComponent ? `${topComponent.name} ${topComponent.armTime}` : "-",
        x86Value: topComponent ? `${topComponent.name} ${topComponent.x86Time}` : "-",
        delta: topComponent?.delta ?? "-",
        target: "/analysis/by-stack",
      },
      {
        label: "主导分类差异",
        armValue: topCategory ? `${topCategory.name} ${topCategory.armTime}` : "-",
        x86Value: topCategory ? `${topCategory.name} ${topCategory.x86Time}` : "-",
        delta: topCategory?.delta ?? "-",
        target: "/analysis/by-stack",
      },
      {
        label: "分析对象",
        armValue: topPattern?.title ?? "-",
        x86Value: topRootCause?.title ?? "-",
        delta: `${(ctx.dataset.cases ?? []).length} 个用例`,
        target: "/insights",
      },
    ],
    topPattern: topPattern?.title ?? "-",
    topRootCause: topRootCause?.title ?? "-",
  };
}

export function assembleScopeSummary(ctx: AssemblyContext): ScopeSummary {
  const metricDefinitions = (ctx.framework.metricDefinitions ?? []).map(getMetricDefinition);

  return {
    includedScope: ctx.framework.analysisScope ?? [],
    excludedScope: ctx.framework.excludedScope ?? [],
    metrics: metricDefinitions,
    taxonomy: {
      level1Categories: ctx.framework.taxonomy?.categoriesL1 ?? [],
      componentAxis: ctx.framework.taxonomy?.components ?? [],
      unknownWarningThreshold: ctx.framework.metricGuardrails?.unknownWarnThreshold ?? "-",
    },
    pageHighlights: [
      {
        label: "纳入项",
        value: String((ctx.framework.analysisScope ?? []).length),
        detail: "基于 Framework.analysisScope 组装。",
      },
      {
        label: "排除项",
        value: String((ctx.framework.excludedScope ?? []).length),
        detail: "基于 Framework.excludedScope 组装。",
      },
      {
        label: "一级分类",
        value: String((ctx.framework.taxonomy?.categoriesL1 ?? []).length),
        detail: "基于 Framework.taxonomy.categoriesL1 组装。",
      },
      {
        label: "组件轴",
        value: String((ctx.framework.taxonomy?.components ?? []).length),
        detail: "基于 Framework.taxonomy.components 组装。",
      },
    ],
  };
}

export function assembleStackOverview(ctx: AssemblyContext): StackOverview {
  const stackOverview = ctx.dataset.stackOverview;
  if (!stackOverview) {
    return {
      platformTotals: {
        arm: "-",
        x86: "-",
      },
      components: [],
      categories: [],
    };
  }

  const functionNameById = new Map(
    (ctx.dataset.functions ?? []).map((entry) => [entry.id, entry.symbol]),
  );

  return {
    platformTotals: stackOverview.platformTotals,
    components: stackOverview.components,
    categories: stackOverview.categories.map((category) => ({
      ...category,
      topFunction: category.topFunctionId
        ? (functionNameById.get(category.topFunctionId) ?? category.topFunctionId)
        : "",
    })),
  };
}

export function assembleOpportunityRanking(ctx: AssemblyContext): OpportunityRankingEntry[] {
  return ctx.dataset.opportunities ?? [];
}

export function assemblePatternIndex(ctx: AssemblyContext): PatternIndexEntry[] {
  return (ctx.dataset.patterns ?? []).map((entry) => ({
    id: entry.id,
    title: entry.title,
    confidence: entry.confidence,
  }));
}

export function assembleRootCauseIndex(ctx: AssemblyContext): RootCauseIndexEntry[] {
  return (ctx.dataset.rootCauses ?? []).map((entry) => ({
    id: entry.id,
    title: entry.title,
    confidence: entry.confidence,
  }));
}

export function assembleCaseDetail(ctx: AssemblyContext, caseId: string): CaseDetail {
  const entry = (ctx.dataset.cases ?? []).find((item) => item.id === caseId || item.legacyCaseId === caseId);
  if (!entry) {
    throw new Error(`Case ${caseId} not found in dataset ${ctx.dataset.id}`);
  }
  const binding = (ctx.project.caseBindings ?? []).find((item) => item.caseId === entry.id);

  const hotspots = entry.hotspots
    ?? (ctx.dataset.functions ?? [])
      .filter((fn) => (fn.caseIds ?? []).includes(entry.id))
      .map((fn) => ({
        id: fn.id,
        symbol: fn.symbol,
        component: fn.component ?? "",
        category: fn.categoryL1 ?? "",
        delta: fn.metrics?.delta ?? "-",
        patternCount: (fn.patternIds ?? []).length,
      }));

  const patternIds = entry.patterns
    ?? Array.from(new Set(hotspots.flatMap((hotspot) => {
      const fn = (ctx.dataset.functions ?? []).find((item) => item.id === hotspot.id);
      return fn?.patternIds ?? [];
    })));

  const rootCauses = entry.rootCauses
    ?? Array.from(
      new Set(
        (ctx.dataset.patterns ?? [])
          .filter((pattern) => patternIds.includes(pattern.id))
          .flatMap((pattern) => pattern.rootCauseIds),
      ),
    );

  return {
    id: entry.legacyCaseId ?? entry.id,
    name: entry.name,
    semanticNotes: entry.semanticNotes ?? binding?.notes ?? "由四层模型示例组装的用例详情。",
    knownDeviations: entry.knownDeviations ?? [],
    artifactIds: entry.artifactIds ?? binding?.primaryArtifactIds ?? [],
    metrics: {
      demo: {
        arm: entry.metrics?.demo?.arm ?? "-",
        x86: entry.metrics?.demo?.x86 ?? "-",
        delta: entry.metrics?.demo?.delta ?? entry.metrics?.demoDelta ?? "-",
      },
      tm: {
        arm: entry.metrics?.tm?.arm ?? "-",
        x86: entry.metrics?.tm?.x86 ?? "-",
        delta: entry.metrics?.tm?.delta ?? entry.metrics?.tmDelta ?? "-",
      },
      operator: {
        arm: entry.metrics?.operator?.arm ?? "-",
        x86: entry.metrics?.operator?.x86 ?? "-",
        delta: entry.metrics?.operator?.delta ?? entry.metrics?.operatorDelta ?? "-",
      },
      framework: {
        arm: entry.metrics?.framework?.arm ?? "-",
        x86: entry.metrics?.framework?.x86 ?? "-",
        delta: entry.metrics?.framework?.delta ?? entry.metrics?.frameworkDelta ?? "-",
      },
    },
    hotspots,
    patterns: patternIds,
    rootCauses,
  };
}

export function assembleCategoryDetail(ctx: AssemblyContext, categoryId: string): CategoryDetail {
  const category = (ctx.dataset.categoryDetails ?? []).find((entry) => entry.id === categoryId);
  if (!category) {
    throw new Error(`Category ${categoryId} not found in dataset ${ctx.dataset.id}`);
  }

  const functionsById = new Map((ctx.dataset.functions ?? []).map((entry) => [entry.id, entry]));
  const hotspots = category.hotspotIds.map((id) => {
    const entry = functionsById.get(id);
    if (!entry) {
      throw new Error(`Function ${id} not found for category ${categoryId}`);
    }

    return {
      id: entry.id,
      symbol: entry.symbol,
      component: entry.component ?? "",
      selfArm: entry.metrics?.selfArm ?? "-",
      selfX86: entry.metrics?.selfX86 ?? "-",
      totalArm: entry.metrics?.totalArm ?? "-",
      totalX86: entry.metrics?.totalX86 ?? "-",
      armShare: entry.metrics?.armShare ?? "-",
      x86Share: entry.metrics?.x86Share ?? "-",
      delta: entry.metrics?.delta ?? "-",
      deltaContribution: entry.metrics?.deltaContribution ?? "-",
    };
  });

  return {
    id: category.id,
    name: category.name,
    level: category.level,
    parentCategoryId: category.parentCategoryId,
    componentIds: category.componentIds,
    armTime: category.armTime,
    x86Time: category.x86Time,
    armShare: category.armShare,
    x86Share: category.x86Share,
    delta: category.delta,
    deltaContribution: category.deltaContribution,
    caseIds: category.caseIds,
    hotspots,
    patternIds: category.patternIds,
    artifactIds: category.artifactIds,
  };
}

export function assembleFunctionDetail(ctx: AssemblyContext, functionId: string): FunctionDetail {
  const entry = (ctx.dataset.functions ?? []).find((item) => item.id === functionId);
  if (!entry) {
    throw new Error(`Function ${functionId} not found in dataset ${ctx.dataset.id}`);
  }

  const binding = (ctx.project.functionBindings ?? []).find((item) => item.functionId === functionId);
  const anchorsById = new Map((ctx.source.sourceAnchors ?? []).map((anchor) => [anchor.id, anchor]));

  const existingArtifactIds = entry.artifactIds ?? [];
  const boundArtifactIds = [...(binding?.armArtifactIds ?? []), ...(binding?.x86ArtifactIds ?? [])];
  const artifactIds = [...new Set([...existingArtifactIds, ...boundArtifactIds])];

  const diffView = entry.diffView
    ? {
        ...entry.diffView,
        analysisBlocks: entry.diffView.analysisBlocks.map((block) => {
          if (block.sourceAnchors.length > 0) {
            return block;
          }

          const sourceAnchors = (binding?.sourceAnchorIds ?? [])
            .map((anchorId, index) => {
              const anchor = anchorsById.get(anchorId);
              if (!anchor) {
                return null;
              }

              return {
                id: anchor.id,
                label: anchor.label,
                role: anchor.role,
                location: anchor.location,
                snippet: anchor.snippet ?? "",
                defaultExpanded: index === 0,
              };
            })
            .filter((anchor): anchor is NonNullable<typeof anchor> => anchor !== null);

          return {
            ...block,
            sourceAnchors,
          };
        }),
      }
    : undefined;

  return {
    id: entry.id,
    symbol: entry.symbol,
    component: entry.component ?? "",
    categoryL1: entry.categoryL1 ?? "",
    categoryL2: entry.categoryL2 ?? "",
    caseIds: entry.caseIds ?? [],
    artifactIds,
    metrics: {
      selfArm: entry.metrics?.selfArm ?? "-",
      selfX86: entry.metrics?.selfX86 ?? "-",
      totalArm: entry.metrics?.totalArm ?? "-",
      totalX86: entry.metrics?.totalX86 ?? "-",
      delta: entry.metrics?.delta ?? "-",
    },
    callPath: entry.callPath ?? [],
    patternIds: entry.patternIds ?? [],
    diffView,
  };
}

export function assemblePatternDetail(ctx: AssemblyContext, patternId: string): PatternDetail {
  const entry = (ctx.dataset.patterns ?? []).find((item) => item.id === patternId);
  if (!entry) {
    throw new Error(`Pattern ${patternId} not found in dataset ${ctx.dataset.id}`);
  }
  return entry;
}

export function assembleRootCauseDetail(ctx: AssemblyContext, rootCauseId: string): RootCauseDetail {
  const entry = (ctx.dataset.rootCauses ?? []).find((item) => item.id === rootCauseId);
  if (!entry) {
    throw new Error(`Root cause ${rootCauseId} not found in dataset ${ctx.dataset.id}`);
  }
  return entry;
}

export function assembleComponentDetail(ctx: AssemblyContext, componentId: string): ComponentDetail {
  const component = (ctx.dataset.componentDetails ?? []).find((entry) => entry.id === componentId);
  if (!component) {
    throw new Error(`Component ${componentId} not found in dataset ${ctx.dataset.id}`);
  }

  const functionsById = new Map((ctx.dataset.functions ?? []).map((entry) => [entry.id, entry]));
  const hotspots = component.hotspotIds.map((id) => {
    const entry = functionsById.get(id);
    if (!entry) {
      throw new Error(`Function ${id} not found for component ${componentId}`);
    }

    return {
      id: entry.id,
      symbol: entry.symbol,
      category: entry.categoryL1 ?? "",
      selfArm: entry.metrics?.selfArm ?? "-",
      selfX86: entry.metrics?.selfX86 ?? "-",
      totalArm: entry.metrics?.totalArm ?? "-",
      totalX86: entry.metrics?.totalX86 ?? "-",
      armShare: entry.metrics?.armShare ?? "-",
      x86Share: entry.metrics?.x86Share ?? "-",
      delta: entry.metrics?.delta ?? "-",
      deltaContribution: entry.metrics?.deltaContribution ?? "-",
    };
  });

  return {
    id: component.id,
    name: component.name,
    armTime: component.armTime,
    x86Time: component.x86Time,
    armShare: component.armShare,
    x86Share: component.x86Share,
    delta: component.delta,
    deltaContribution: component.deltaContribution,
    categories: component.categories,
    hotspots,
    patternIds: component.patternIds,
    rootCauseIds: component.rootCauseIds,
    artifactIds: component.artifactIds,
  };
}

async function loadText(path: string): Promise<string> {
  const response = await fetch(resolvePublicPath(path));
  if (!response.ok) {
    throw new Error(`Failed to load text from ${path}`);
  }

  return response.text();
}

export async function assembleArtifactDetail(
  ctx: AssemblyContext,
  artifactId: string,
): Promise<ArtifactDetail> {
  const artifact = (ctx.source.artifactIndex ?? []).find((entry) => entry.id === artifactId);
  if (!artifact) {
    throw new Error(`Artifact ${artifactId} not found in source ${ctx.source.id}`);
  }

  const content = await loadText(artifact.path);

  return {
    ...artifact,
    content,
  };
}
