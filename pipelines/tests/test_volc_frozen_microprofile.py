from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pyframework_pipeline.adapters.volcoperatorsim.frozen_microprofile import (
    cycle_items,
    read_jsonl_field,
)


class VolcFrozenMicroprofileTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
