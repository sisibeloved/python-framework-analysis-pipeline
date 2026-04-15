import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyframework_pipeline.config import resolve_four_layer_root
from pyframework_pipeline.validators.schema import validate_json_schema


JsonObject = dict[str, Any]
SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "schemas"


@dataclass
class ValidationError:
    code: str
    message: str
    path: str

    def to_dict(self) -> JsonObject:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }


@dataclass
class ValidationReport:
    project_id: str
    root: Path
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "ok" if not self.errors else "error"

    def add(self, code: str, message: str, path: str) -> None:
        self.errors.append(ValidationError(code=code, message=message, path=path))

    def to_dict(self) -> JsonObject:
        return {
            "status": self.status,
            "projectId": self.project_id,
            "root": str(self.root),
            "errorCount": len(self.errors),
            "errors": [error.to_dict() for error in self.errors],
        }


def validate_four_layer_project(path: Path) -> ValidationReport:
    root = resolve_four_layer_root(path).resolve()
    project = load_single_json(root / "projects", ".project.json")
    project_id = str(project.get("id", "unknown-project"))
    report = ValidationReport(project_id=project_id, root=root)

    framework = load_ref_json(root / "frameworks", project.get("frameworkRef"), ".framework.json", report)
    dataset = load_ref_json(root / "datasets", project.get("datasetRef"), ".dataset.json", report)
    source = load_ref_json(root / "sources", project.get("sourceRef"), ".source.json", report)
    if not framework or not dataset or not source:
        return report

    validate_schema(framework, "framework.schema.json", "Framework", report)
    validate_schema(dataset, "dataset.schema.json", "Dataset", report)
    validate_schema(source, "source.schema.json", "Source", report)
    validate_schema(project, "project.schema.json", "Project", report)

    require_id(framework, project.get("frameworkRef"), "framework", report)
    require_id(dataset, project.get("datasetRef"), "dataset", report)
    require_id(source, project.get("sourceRef"), "source", report)

    functions = index_by_id(dataset.get("functions", []), "Dataset.functions", report)
    cases = index_by_id(dataset.get("cases", []), "Dataset.cases", report)
    patterns = index_by_id(dataset.get("patterns", []), "Dataset.patterns", report)
    root_causes = index_by_id(dataset.get("rootCauses", []), "Dataset.rootCauses", report)
    source_anchors = index_by_id(source.get("sourceAnchors", []), "Source.sourceAnchors", report)
    artifacts = index_by_id(source.get("artifactIndex", []), "Source.artifactIndex", report)

    validate_case_bindings(project, cases, source_anchors, artifacts, report)
    validate_function_bindings(project, functions, source_anchors, artifacts, report)
    validate_pattern_bindings(project, functions, source_anchors, artifacts, patterns, report)
    validate_root_cause_bindings(project, root_causes, patterns, artifacts, report)
    validate_stack_overview(dataset, functions, report)
    validate_dataset_references(dataset, functions, cases, patterns, root_causes, artifacts, report)

    return report


def load_single_json(directory: Path, suffix: str) -> JsonObject:
    matches = sorted(directory.glob(f"*{suffix}"))
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {suffix} in {directory}, found {len(matches)}")
    return load_json(matches[0])


def load_ref_json(directory: Path, ref: Any, suffix: str, report: ValidationReport) -> JsonObject | None:
    if not isinstance(ref, str) or not ref:
        report.add("missing_ref", f"Missing reference for {suffix}", str(directory))
        return None

    path = directory / f"{ref}{suffix}"
    if not path.exists():
        report.add("missing_file", f"Referenced file does not exist: {path}", str(path))
        return None
    return load_json(path)


def load_json(path: Path) -> JsonObject:
    return json.loads(path.read_text())


def validate_schema(obj: JsonObject, schema_name: str, path: str, report: ValidationReport) -> None:
    schema = load_json(SCHEMA_ROOT / schema_name)
    for issue in validate_json_schema(obj, schema, path):
        report.add("schema_error", issue.message, issue.path)


def require_id(obj: JsonObject, expected: Any, label: str, report: ValidationReport) -> None:
    actual = obj.get("id")
    if actual != expected:
        report.add("id_mismatch", f"{label} id {actual!r} does not match reference {expected!r}", label)


def index_by_id(items: Any, path: str, report: ValidationReport) -> dict[str, JsonObject]:
    indexed: dict[str, JsonObject] = {}
    if not isinstance(items, list):
        report.add("invalid_list", f"{path} must be a list", path)
        return indexed

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            report.add("invalid_item", f"{path}[{index}] must be an object", f"{path}[{index}]")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            report.add("missing_id", f"{path}[{index}] is missing id", f"{path}[{index}]")
            continue
        if item_id in indexed:
            report.add("duplicate_id", f"Duplicate id {item_id} in {path}", f"{path}[{index}]")
        indexed[item_id] = item
    return indexed


def validate_case_bindings(
    project: JsonObject,
    cases: dict[str, JsonObject],
    source_anchors: dict[str, JsonObject],
    artifacts: dict[str, JsonObject],
    report: ValidationReport,
) -> None:
    for index, binding in enumerate(project.get("caseBindings", [])):
        base = f"Project.caseBindings[{index}]"
        require_existing(binding.get("caseId"), cases, "missing_case", base, report)
        require_many(binding.get("sourceAnchorIds", []), source_anchors, "missing_source_anchor", base, report)
        require_many(binding.get("primaryArtifactIds", []), artifacts, "missing_artifact_reference", base, report)


def validate_function_bindings(
    project: JsonObject,
    functions: dict[str, JsonObject],
    source_anchors: dict[str, JsonObject],
    artifacts: dict[str, JsonObject],
    report: ValidationReport,
) -> None:
    for index, binding in enumerate(project.get("functionBindings", [])):
        base = f"Project.functionBindings[{index}]"
        require_existing(binding.get("functionId"), functions, "missing_function", base, report)
        require_many(binding.get("sourceAnchorIds", []), source_anchors, "missing_source_anchor", base, report)
        require_many(binding.get("armArtifactIds", []), artifacts, "missing_artifact_reference", base, report)
        require_many(binding.get("x86ArtifactIds", []), artifacts, "missing_artifact_reference", base, report)


def validate_pattern_bindings(
    project: JsonObject,
    functions: dict[str, JsonObject],
    source_anchors: dict[str, JsonObject],
    artifacts: dict[str, JsonObject],
    patterns: dict[str, JsonObject],
    report: ValidationReport,
) -> None:
    for index, binding in enumerate(project.get("patternBindings", [])):
        base = f"Project.patternBindings[{index}]"
        require_existing(binding.get("patternId"), patterns, "missing_pattern", base, report)
        require_many(binding.get("functionIds", []), functions, "missing_function", base, report)
        require_many(binding.get("sourceAnchorIds", []), source_anchors, "missing_source_anchor", base, report)
        require_many(binding.get("artifactIds", []), artifacts, "missing_artifact_reference", base, report)


def validate_root_cause_bindings(
    project: JsonObject,
    root_causes: dict[str, JsonObject],
    patterns: dict[str, JsonObject],
    artifacts: dict[str, JsonObject],
    report: ValidationReport,
) -> None:
    for index, binding in enumerate(project.get("rootCauseBindings", [])):
        base = f"Project.rootCauseBindings[{index}]"
        require_existing(binding.get("rootCauseId"), root_causes, "missing_root_cause", base, report)
        require_many(binding.get("patternIds", []), patterns, "missing_pattern", base, report)
        require_many(binding.get("artifactIds", []), artifacts, "missing_artifact_reference", base, report)


def validate_stack_overview(dataset: JsonObject, functions: dict[str, JsonObject], report: ValidationReport) -> None:
    categories = dataset.get("stackOverview", {}).get("categories", [])
    for index, category in enumerate(categories):
        top_function_id = category.get("topFunctionId")
        if top_function_id:
            require_existing(top_function_id, functions, "missing_top_function", f"Dataset.stackOverview.categories[{index}]", report)


def validate_dataset_references(
    dataset: JsonObject,
    functions: dict[str, JsonObject],
    cases: dict[str, JsonObject],
    patterns: dict[str, JsonObject],
    root_causes: dict[str, JsonObject],
    artifacts: dict[str, JsonObject],
    report: ValidationReport,
) -> None:
    for index, function in enumerate(dataset.get("functions", [])):
        base = f"Dataset.functions[{index}]"
        require_many(function.get("caseIds", []), cases, "missing_case", base, report)
        require_many(function.get("patternIds", []), patterns, "missing_pattern", base, report)
        require_many(function.get("artifactIds", []), artifacts, "missing_artifact_reference", base, report)

    for index, pattern in enumerate(dataset.get("patterns", [])):
        base = f"Dataset.patterns[{index}]"
        require_many(pattern.get("caseIds", []), cases, "missing_case", base, report)
        require_many(pattern.get("functionIds", []), functions, "missing_function", base, report)
        require_many(pattern.get("rootCauseIds", []), root_causes, "missing_root_cause", base, report)
        require_many(pattern.get("artifactIds", []), artifacts, "missing_artifact_reference", base, report)

    for index, root_cause in enumerate(dataset.get("rootCauses", [])):
        base = f"Dataset.rootCauses[{index}]"
        require_many(root_cause.get("patternIds", []), patterns, "missing_pattern", base, report)
        require_many(root_cause.get("artifactIds", []), artifacts, "missing_artifact_reference", base, report)


def require_many(ids: Any, indexed: dict[str, JsonObject], code: str, path: str, report: ValidationReport) -> None:
    if not isinstance(ids, list):
        report.add("invalid_reference_list", f"{path} references must be a list", path)
        return
    for item_id in ids:
        require_existing(item_id, indexed, code, path, report)


def require_existing(item_id: Any, indexed: dict[str, JsonObject], code: str, path: str, report: ValidationReport) -> None:
    if not isinstance(item_id, str) or not item_id:
        report.add(code, f"{path} has an empty reference", path)
        return
    if item_id not in indexed:
        report.add(code, f"{path} references missing id: {item_id}", path)
