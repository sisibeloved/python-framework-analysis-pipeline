from __future__ import annotations

import json
import argparse
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pyframework_pipeline.adapters.volcoperatorsim.frozen_microprofile import (
    _resolve,
    _run_document_deduplicator,
    cycle_items,
    read_jsonl_field,
    run,
)


class VolcFrozenMicroprofileTest(unittest.TestCase):
    def test_document_deduplicator_replays_the_real_daft_global_barrier(self) -> None:
        materialized = Mock()
        materialized.count_rows.return_value = 2
        frame = Mock()
        apply_deduplicator = Mock(return_value=(materialized, "text"))
        daft = types.SimpleNamespace(from_pydict=Mock(return_value=frame))
        pipeline_builder = types.SimpleNamespace(
            _apply_deduplicator=apply_deduplicator
        )

        with patch.dict(
            sys.modules,
            {
                "daft": daft,
                "runner.pipeline_builder": pipeline_builder,
            },
        ):
            checksum = _run_document_deduplicator(["a", "a", "b"], repetitions=3)

        self.assertEqual(checksum, 6)
        self.assertEqual(daft.from_pydict.call_count, 3)
        daft.from_pydict.assert_called_with({"text": ["a", "a", "b"]})
        self.assertEqual(apply_deduplicator.call_count, 3)
        apply_deduplicator.assert_called_with(
            frame,
            "text",
            {"dj_ops": "document_deduplicator"},
        )
        self.assertEqual(materialized.count_rows.call_count, 3)

    def test_identity_preparation_preserves_frozen_text(self) -> None:
        identity = _resolve("identity")

        self.assertEqual(identity("frozen text"), "frozen text")

    def test_read_jsonl_field_preserves_frozen_order_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.jsonl"
            manifest.write_text(
                "".join(
                    json.dumps({"file_path": f"/data/{index}.pdf"}) + "\n"
                    for index in range(4)
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                read_jsonl_field(manifest, "file_path", limit=2),
                ["/data/0.pdf", "/data/1.pdf"],
            )

    def test_cycle_items_is_deterministic_and_exact_length(self) -> None:
        self.assertEqual(
            cycle_items(["a", "b", "c"], total=8),
            ["a", "b", "c", "a", "b", "c", "a", "b"],
        )

    def test_cycle_items_rejects_empty_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty frozen source"):
            cycle_items([], total=1)

    def test_resolve_binds_task_params_through_the_operator_registry(self) -> None:
        resolved = Mock(return_value=True)
        registry = types.SimpleNamespace(
            OPERATOR_CATALOG={
                "image_size_filter": types.SimpleNamespace(category="filter")
            },
            resolve_filter_fn=Mock(return_value=resolved),
            resolve_mapper_fn=Mock(),
        )

        with patch.dict(sys.modules, {"registry": registry}):
            function = _resolve(
                "image_size_filter", {"min_size": "1B", "max_size": "20MB"}
            )

        self.assertIs(function, resolved)
        registry.resolve_filter_fn.assert_called_once_with(
            "image_size_filter", {"min_size": "1B", "max_size": "20MB"}
        )

    def test_run_streams_repeated_calls_without_materializing_the_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "values.json"
            source.write_text(json.dumps(["a", "bb"]), encoding="utf-8")
            args = argparse.Namespace(
                input_json=source,
                manifest=None,
                field="text",
                limit=2,
                total_calls=7,
                operator="identity",
                params_json="{}",
                output_json=None,
                summary_json=None,
            )

            with patch(
                "pyframework_pipeline.adapters.volcoperatorsim."
                "frozen_microprofile.cycle_items",
                side_effect=AssertionError("must not materialize repeated calls"),
            ):
                result = run(args)

        self.assertEqual(result["totalCalls"], 7)
        self.assertEqual(result["checksum"], 10)


if __name__ == "__main__":
    unittest.main()
