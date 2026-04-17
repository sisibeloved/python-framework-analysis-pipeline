"""Tests for bridge module (issue template, comment parser, manifest, analysis)."""

import json
import tempfile
import unittest
from pathlib import Path

from pyframework_pipeline.bridge.comment_parser import (
    ParsedAnalysis,
    find_analysis_comment,
    parse_comment_body,
)
from pyframework_pipeline.bridge.issue_template import (
    build_asm_diff_issue,
    check_chunking,
)
from pyframework_pipeline.bridge.manifest import (
    BridgeIssueEntry,
    BridgeManifest,
    load_bridge_manifest,
)


# ---------------------------------------------------------------------------
# Issue template tests
# ---------------------------------------------------------------------------

class TestBuildAsmDiffIssue(unittest.TestCase):
    """Tests for build_asm_diff_issue()."""

    def test_dual_platform(self):
        func = {"symbol": "my_func", "component": "cpython", "categoryL1": "memory"}
        result = build_asm_diff_issue(func, arm_asm="ldr x0", x86_asm="mov rax")
        self.assertEqual(result["title"], "my_func跨平台机器码差异分析")
        self.assertIn("Kunpeng", result["body"])
        self.assertIn("Zen4", result["body"])
        self.assertIn("ldr x0", result["body"])
        self.assertIn("mov rax", result["body"])
        self.assertIn("跨平台机器码差异分析：my_func", result["body"])

    def test_arm_only(self):
        func = {"symbol": "arm_only_func"}
        result = build_asm_diff_issue(func, arm_asm="ldr x0", x86_asm=None)
        self.assertIn("Kunpeng only", result["title"])
        self.assertIn("Kunpeng 机器码分析", result["body"])

    def test_x86_only(self):
        func = {"symbol": "x86_only_func"}
        result = build_asm_diff_issue(func, arm_asm=None, x86_asm="mov rax")
        self.assertIn("Zen4 only", result["title"])
        self.assertIn("Zen4 机器码分析", result["body"])

    def test_both_none_raises(self):
        func = {"symbol": "no_asm"}
        with self.assertRaises(ValueError):
            build_asm_diff_issue(func, arm_asm=None, x86_asm=None)

    def test_with_source_code(self):
        func = {"symbol": "has_source"}
        result = build_asm_diff_issue(
            func, arm_asm="nop", x86_asm="nop", source_code="int main() {}",
        )
        self.assertIn("int main()", result["body"])
        self.assertIn("```c", result["body"])

    def test_no_source_code(self):
        func = {"symbol": "no_source"}
        result = build_asm_diff_issue(
            func, arm_asm="nop", x86_asm="nop", source_code=None,
        )
        self.assertIn("（无源码）", result["body"])

    def test_component_display(self):
        func = {"symbol": "f", "component": "cpython"}
        result = build_asm_diff_issue(func, arm_asm="nop", x86_asm="nop")
        self.assertIn("CPython", result["body"])

    def test_truncation(self):
        long_asm = "\n".join([f"instr_{i}" for i in range(3000)])
        func = {"symbol": "long_func"}
        result = build_asm_diff_issue(
            func, arm_asm=long_asm, x86_asm=long_asm, max_lines=100,
        )
        self.assertIn("截断", result["body"])

    def test_framework_name_in_prompt(self):
        func = {"symbol": "f", "component": "cpython"}
        result = build_asm_diff_issue(
            func, arm_asm="nop", x86_asm="nop",
            framework_name="CPython 3.14",
        )
        self.assertIn("CPython 3.14", result["body"])


class TestCheckChunking(unittest.TestCase):
    def test_short_body(self):
        result = check_chunking("short body")
        self.assertFalse(result["needs_chunking"])

    def test_long_body(self):
        result = check_chunking("x" * 70000)
        self.assertTrue(result["needs_chunking"])
        self.assertGreater(result["line_count"], 0)


# ---------------------------------------------------------------------------
# Comment parser tests
# ---------------------------------------------------------------------------

_SAMPLE_DUAL_COMMENT = """\
## 跨平台机器码差异分析：_PyObject_Malloc

### 总览

| 源码段 | ARM 指令数 | x86 指令数 | 差异概要 |
|--------|-----------|-----------|---------|
| Fast-Path | 6 | 4 | ARM 多 2 条 load |
| Slow-Path | 12 | 8 | pool reload 拆分 |

#### 1. Fast-Path Alloc

ARM:
```asm
ldr x0, [pool]
cbz x0, slow
```

x86:
```asm
mov rax, [pool]
test rax, rax
```

| 项目 | ARM | x86 | 差异 |
|------|-----|-----|------|
| 指令数 | 6 | 4 | ARM 多 2 条 |
| 延迟 | 3 cyc | 2 cyc | load 延迟更高 |

#### 2. Slow-Path Pool Reload

ARM 侧保留独立 reload 区块。

### 根因汇总

| 编号 | 劣势来源 | 出现位置 | 热路径影响 | 根因类别 |
|------|---------|---------|-----------|---------|
| R1 | pool reload 拆分 | slow path | 高 | 指令选择 |
| R2 | load 延迟 | fast path | 中 | 微架构 |

### 优化策略

| 编号 | 优化点 | 策略 | 受益方 | ARM收益更高的原因 | 实施方 |
|------|-------|------|-------|-----------------|-------|
| O1 | pool reload | 合并 reload 链 | 仅ARM | x86 已合并 | CPython |
"""


class TestParseCommentBody(unittest.TestCase):
    def test_parse_dual_comment(self):
        result = parse_comment_body(_SAMPLE_DUAL_COMMENT)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.symbol, "_PyObject_Malloc")
        self.assertEqual(len(result.overview_table), 2)
        self.assertEqual(len(result.sections), 2)
        self.assertEqual(len(result.root_causes), 2)
        self.assertEqual(len(result.optimizations), 1)

    def test_overview_table_content(self):
        result = parse_comment_body(_SAMPLE_DUAL_COMMENT)
        assert result is not None
        row = result.overview_table[0]
        self.assertEqual(row["源码段"], "Fast-Path")
        self.assertIn("ARM", row.get("差异概要", ""))

    def test_sections_have_table(self):
        result = parse_comment_body(_SAMPLE_DUAL_COMMENT)
        assert result is not None
        sec1 = result.sections[0]
        self.assertIn("Fast-Path", sec1["title"])
        self.assertTrue(len(sec1["table"]) > 0)

    def test_root_causes_content(self):
        result = parse_comment_body(_SAMPLE_DUAL_COMMENT)
        assert result is not None
        rc = result.root_causes[0]
        self.assertIn("pool reload", rc.get("劣势来源", ""))

    def test_optimizations_content(self):
        result = parse_comment_body(_SAMPLE_DUAL_COMMENT)
        assert result is not None
        opt = result.optimizations[0]
        self.assertEqual(opt.get("优化点"), "pool reload")
        self.assertEqual(opt.get("实施方"), "CPython")

    def test_single_platform_comment(self):
        body = "## Kunpeng 机器码分析：my_func\n\n### 总览\n\n| 指标 | 值 |\n|------|----|\n| 指令数 | 42 |\n"
        result = parse_comment_body(body)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.symbol, "my_func")

    def test_no_matching_comment(self):
        body = "This is just a regular comment with no analysis."
        result = parse_comment_body(body)
        self.assertIsNone(result)

    def test_warnings_on_missing_sections(self):
        body = "## 跨平台机器码差异分析：test_func\n\nSome text without tables.\n"
        result = parse_comment_body(body)
        assert result is not None
        self.assertTrue(len(result.warnings) > 0)

    def test_find_analysis_comment_takes_last(self):
        comments = [
            {"body": "## 跨平台机器码差异分析：f1\n\nold"},
            {"body": "## 跨平台机器码差异分析：f2\n\nnew"},
        ]
        result = find_analysis_comment(comments)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.symbol, "f2")


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------

class TestBridgeManifest(unittest.TestCase):
    def test_roundtrip(self):
        m = BridgeManifest(project_id="test")
        entry = BridgeIssueEntry(
            issue_type="asm-diff",
            function_id="func_001",
            platform="gitcode",
            repo="owner/repo",
            issue_number=42,
            issue_url="https://gitcode.com/owner/repo/issues/42",
            status="created",
            created_at="2026-04-17T10:00:00Z",
        )
        m.issues.append(entry)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)

        try:
            m.write(path)
            loaded = load_bridge_manifest(path)
            self.assertEqual(loaded.project_id, "test")
            self.assertEqual(len(loaded.issues), 1)
            self.assertEqual(loaded.issues[0].function_id, "func_001")
            self.assertEqual(loaded.issues[0].issue_number, 42)
        finally:
            path.unlink()

    def test_load_missing_returns_empty(self):
        loaded = load_bridge_manifest(Path("/nonexistent/manifest.json"))
        self.assertEqual(len(loaded.issues), 0)

    def test_find_by_function(self):
        m = BridgeManifest()
        m.issues.append(BridgeIssueEntry(
            issue_type="asm-diff", function_id="func_a",
            platform="gitcode", repo="o/r", issue_number=1,
            issue_url="", status="created", created_at="",
        ))
        m.issues.append(BridgeIssueEntry(
            issue_type="asm-diff", function_id="func_b",
            platform="gitcode", repo="o/r", issue_number=2,
            issue_url="", status="created", created_at="",
        ))
        result = m.find_by_function("func_a")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].issue_number, 1)

    def test_find_main_issues(self):
        m = BridgeManifest()
        m.issues.append(BridgeIssueEntry(
            issue_type="asm-diff", function_id="f1",
            platform="gitcode", repo="o/r", issue_number=1,
            issue_url="", status="created", created_at="",
        ))
        m.issues.append(BridgeIssueEntry(
            issue_type="asm-diff-chunk", function_id="f2",
            platform="gitcode", repo="o/r", issue_number=2,
            issue_url="", status="created", created_at="",
            parent_issue_number=1,
        ))
        main = m.find_main_issues()
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0].issue_number, 1)

    def test_extra_fields_roundtrip(self):
        m = BridgeManifest()
        entry = BridgeIssueEntry(
            issue_type="asm-diff", function_id="f1",
            platform="gitcode", repo="o/r", issue_number=1,
            issue_url="", status="created", created_at="",
            extra={"chunk_index": 2},
        )
        m.issues.append(entry)
        d = entry.to_dict()
        self.assertEqual(d["chunk_index"], 2)
        loaded_entry = BridgeIssueEntry.from_dict(d)
        self.assertEqual(loaded_entry.extra["chunk_index"], 2)


# ---------------------------------------------------------------------------
# Analysis publish/fetch tests (with temp directories)
# ---------------------------------------------------------------------------

class TestAnalysisPublishDryRun(unittest.TestCase):
    """Test publish in dry-run mode using temp directories."""

    def _make_project(self, tmp: Path, functions: list[dict]) -> Path:
        """Create minimal project structure and return project.yaml path."""
        root = tmp / "four-layer"
        ds_dir = root / "datasets"
        ds_dir.mkdir(parents=True)

        dataset = {
            "id": "test-ds",
            "frameworkId": "cpython",
            "cases": [],
            "stackOverview": {"components": [], "categories": []},
            "functions": functions,
            "patterns": [],
            "rootCauses": [],
        }
        (ds_dir / "test.dataset.json").write_text(
            json.dumps(dataset, ensure_ascii=False), encoding="utf-8",
        )

        src_dir = root / "sources"
        src_dir.mkdir(parents=True)
        (src_dir / "test.source.json").write_text(
            json.dumps({"artifactIndex": [], "sourceAnchors": []}),
            encoding="utf-8",
        )

        yaml_path = tmp / "project.yaml"
        yaml_path.write_text(
            f"fourLayerRoot: {root}\n", encoding="utf-8",
        )
        return yaml_path

    def test_dry_run_no_asm(self):
        from pyframework_pipeline.bridge.analysis import publish

        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = self._make_project(Path(tmp), [
                {"id": "func_001", "symbol": "no_asm_func", "artifactIds": []},
            ])
            result = publish(
                yaml_path, repo="o/r", platform="gitcode",
                token="fake", dry_run=True,
            )
        self.assertEqual(result["published"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_dry_run_with_asm(self):
        from pyframework_pipeline.bridge.analysis import publish

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # Create asm file
            asm_dir = tmp / "asm"
            asm_dir.mkdir()
            (asm_dir / "arm.s").write_text("ldr x0\n", encoding="utf-8")
            (asm_dir / "x86.s").write_text("mov rax\n", encoding="utf-8")

            functions = [{
                "id": "func_001",
                "symbol": "test_func",
                "artifactIds": ["asm_arm_func_001", "asm_x86_func_001"],
            }]

            root = tmp / "four-layer"
            ds_dir = root / "datasets"
            ds_dir.mkdir(parents=True)
            dataset = {
                "id": "test-ds", "frameworkId": "cpython",
                "cases": [], "stackOverview": {"components": [], "categories": []},
                "functions": functions, "patterns": [], "rootCauses": [],
            }
            (ds_dir / "test.dataset.json").write_text(
                json.dumps(dataset, ensure_ascii=False), encoding="utf-8",
            )
            src_dir = root / "sources"
            src_dir.mkdir(parents=True)
            source = {
                "artifactIndex": [
                    {"id": "asm_arm_func_001", "filePath": str(asm_dir / "arm.s")},
                    {"id": "asm_x86_func_001", "filePath": str(asm_dir / "x86.s")},
                ],
                "sourceAnchors": [],
            }
            (src_dir / "test.source.json").write_text(
                json.dumps(source, ensure_ascii=False), encoding="utf-8",
            )

            yaml_path = tmp / "project.yaml"
            yaml_path.write_text(
                f"fourLayerRoot: {root}\n", encoding="utf-8",
            )

            result = publish(
                yaml_path, repo="o/r", platform="gitcode",
                token="fake", dry_run=True,
            )

        self.assertEqual(result["published"], 1)
        self.assertEqual(result["total_functions"], 1)
        self.assertTrue(result["issues"][0]["dry_run"])
        self.assertIn("test_func", result["issues"][0]["title"])


class TestAnalysisStatus(unittest.TestCase):
    def test_status_empty(self):
        from pyframework_pipeline.bridge.analysis import status

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = tmp / "four-layer"
            ds_dir = root / "datasets"
            ds_dir.mkdir(parents=True)
            (ds_dir / "test.dataset.json").write_text(
                json.dumps({
                    "id": "test", "frameworkId": "cpython",
                    "cases": [], "stackOverview": {"components": [], "categories": []},
                    "functions": [], "patterns": [], "rootCauses": [],
                }),
                encoding="utf-8",
            )
            yaml_path = tmp / "project.yaml"
            yaml_path.write_text(
                f"fourLayerRoot: {root}\n", encoding="utf-8",
            )
            result = status(yaml_path)

        self.assertEqual(result["total_issues"], 0)


if __name__ == "__main__":
    unittest.main()
