export type PlatformId = "arm64" | "x86_64";

export type ExecutiveMetric = {
  label: string;
  armValue: string;
  x86Value: string;
  delta: string;
  target?: string;
};

export type ExecutiveSummary = {
  title: string;
  subtitle: string;
  metrics: ExecutiveMetric[];
  topPattern: string;
  topRootCause: string;
};

export type CaseIndexEntry = {
  id: string;
  name: string;
  demoDelta: string;
  tmDelta: string;
  operatorDelta: string;
  frameworkDelta: string;
  demoArm?: string;
  demoX86?: string;
  tmArm?: string;
  tmX86?: string;
  operatorArm?: string;
  operatorX86?: string;
  frameworkArm?: string;
  frameworkX86?: string;
  scale?: string;
  workloadForm?: string;
  semanticStatus?: string;
  pythonUdfMode?: string;
  sourceSqlArtifactId?: string;
  pythonUdfArtifactId?: string;
  notes?: string;
};

export type StackOverviewComponent = {
  id: string;
  name: string;
  armTime: string;
  x86Time: string;
  armShare: string;
  x86Share: string;
  delta: string;
  deltaContribution: string;
};

export type StackOverviewCategory = {
  id: string;
  name: string;
  level: string;
  parentCategoryId?: string;
  armTime: string;
  x86Time: string;
  armShare: string;
  x86Share: string;
  delta: string;
  deltaContribution: string;
  topFunction: string;
  topFunctionId?: string;
};

export type StackOverview = {
  platformTotals: {
    arm: string;
    x86: string;
  };
  components: StackOverviewComponent[];
  categories: StackOverviewCategory[];
};

export type PatternIndexEntry = {
  id: string;
  title: string;
  confidence: string;
};

export type RootCauseIndexEntry = {
  id: string;
  title: string;
  confidence: string;
};

export type OpportunityRankingEntry = {
  id: string;
  title: string;
  impact: string;
  effort: string;
  estimatedGainPct: number;
  rootCauseId?: string;
};

export type ComparisonMetric = {
  arm: string;
  x86: string;
  delta: string;
};

export type CaseHotspot = {
  id: string;
  symbol: string;
  component: string;
  category: string;
  delta: string;
  patternCount: number;
};

export type CaseDetail = {
  id: string;
  name: string;
  semanticNotes: string;
  knownDeviations: string[];
  artifactIds: string[];
  metrics: {
    demo: ComparisonMetric;
    tm: ComparisonMetric;
    operator: ComparisonMetric;
    framework: ComparisonMetric;
  };
  hotspots: CaseHotspot[];
  patterns: string[];
  rootCauses: string[];
};

export type FunctionDetail = {
  id: string;
  symbol: string;
  component: string;
  categoryL1: string;
  categoryL2: string;
  caseIds: string[];
  artifactIds: string[];
  metrics: {
    selfArm: string;
    selfX86: string;
    totalArm: string;
    totalX86: string;
    delta: string;
  };
  callPath: string[];
  patternIds: string[];
  diffView?: {
    functionId: string;
    sourceFile: string;
    sourceLocation: string;
    diffGuide: string;
    analysisBlocks: Array<{
      id: string;
      label: string;
      summary: string;
      patternTag?: string;
      mappingType: string;
      sourceAnchors: Array<{
        id: string;
        label: string;
        role?: string;
        location: string;
        snippet: string;
        defaultExpanded: boolean;
      }>;
      armRegions: Array<{
        id: string;
        label: string;
        location: string;
        role: string;
        snippet: string;
        highlights: string[];
        defaultExpanded: boolean;
      }>;
      x86Regions: Array<{
        id: string;
        label: string;
        location: string;
        role: string;
        snippet: string;
        highlights: string[];
        defaultExpanded: boolean;
      }>;
      mappings: Array<{
        id: string;
        label: string;
        sourceAnchorIds: string[];
        armRegionIds: string[];
        x86RegionIds: string[];
        note: string;
      }>;
      diffSignals: string[];
      alignmentNote: string;
      performanceNote: string;
      defaultExpanded: boolean;
    }>;
  };
};

export type PatternDetail = {
  id: string;
  title: string;
  summary: string;
  confidence: string;
  caseIds: string[];
  functionIds: string[];
  rootCauseIds: string[];
  artifactIds: string[];
};

export type RootCauseDetail = {
  id: string;
  title: string;
  summary: string;
  confidence: string;
  patternIds: string[];
  artifactIds: string[];
  optimizationIdeas: string[];
  validationPlan: string[];
};

export type ScopeSummary = {
  includedScope: string[];
  excludedScope: string[];
  metrics: Array<{
    name: string;
    definition: string;
    boundary: string;
    normalization: string;
  }>;
  taxonomy: {
    level1Categories: string[];
    componentAxis: string[];
    unknownWarningThreshold: string;
  };
  pageHighlights?: Array<{
    label: string;
    value: string;
    detail: string;
  }>;
};

export type ComponentDetail = {
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
  hotspots: Array<{
    id: string;
    symbol: string;
    category: string;
    selfArm: string;
    selfX86: string;
    totalArm: string;
    totalX86: string;
    armShare: string;
    x86Share: string;
    delta: string;
    deltaContribution: string;
  }>;
  patternIds: string[];
  rootCauseIds: string[];
  artifactIds: string[];
};

export type CategoryDetail = {
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
  hotspots: Array<{
    id: string;
    symbol: string;
    component: string;
    selfArm: string;
    selfX86: string;
    totalArm: string;
    totalX86: string;
    armShare: string;
    x86Share: string;
    delta: string;
    deltaContribution: string;
  }>;
  patternIds: string[];
  artifactIds: string[];
};

export type ArtifactDetail = {
  id: string;
  title: string;
  type: string;
  description: string;
  path: string;
  contentType: string;
  content: string;
};
