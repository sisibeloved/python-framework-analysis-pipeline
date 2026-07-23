from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pyframework_pipeline.adapters.volcoperatorsim.scale_freeze import (
    cycle_jsonl_rows,
)


class VolcScaleFreezeTest(unittest.TestCase):
    def test_cycle_jsonl_rows_is_exact_ordered_and_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jsonl"
            output = root / "frozen" / "manifest.jsonl"
            source.write_text(
                json.dumps({"sample_id": "a", "file_path": "/a.jpg"}) + "\n"
                + json.dumps({"sample_id": "b", "file_path": "/b.jpg"}) + "\n",
                encoding="utf-8",
            )

            result = cycle_jsonl_rows(
                source=source,
                output=output,
                rows=5,
                fixture_id="ad_scale5",
            )

            records = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual([row["file_path"] for row in records], [
                "/a.jpg", "/b.jpg", "/a.jpg", "/b.jpg", "/a.jpg"
            ])
            self.assertEqual(
                [row["sample_id"] for row in records],
                [f"ad_scale5_{index:08d}" for index in range(5)],
            )
            self.assertEqual(
                [row["source_sample_id"] for row in records],
                ["a", "b", "a", "b", "a"],
            )
            self.assertEqual(result["rows"], 5)
            self.assertEqual(result["sourceRows"], 2)
            self.assertEqual(
                result["input_fingerprint"],
                "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest(),
            )

    def test_cycle_jsonl_rows_rejects_empty_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "empty.jsonl"
            source.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty JSONL source"):
                cycle_jsonl_rows(
                    source=source,
                    output=root / "out.jsonl",
                    rows=1,
                    fixture_id="empty",
                )


if __name__ == "__main__":
    unittest.main()
