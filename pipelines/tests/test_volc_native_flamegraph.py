"""TDD coverage for the native perf-script flamegraph fallback."""

from __future__ import annotations

import unittest

from pyframework_pipeline.adapters.volcoperatorsim.native_flamegraph import (
    render_perf_script_svg,
)


class NativeFlamegraphTest(unittest.TestCase):
    def test_perf_script_stacks_render_as_a_self_describing_svg(self) -> None:
        perf_script = """python 1 1.000: cycles:
        7f01 leaf_a (/usr/lib/liba.so)
        7f02 parent (/usr/lib/libpython.so)

python 1 1.100: cycles:
        7f03 leaf_b (/usr/lib/libb.so)
        7f02 parent (/usr/lib/libpython.so)

python 1 1.200: cycles:
        7f01 leaf_a (/usr/lib/liba.so)
        7f02 parent (/usr/lib/libpython.so)
"""

        svg, metadata = render_perf_script_svg(perf_script)

        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("Native perf sampled flamegraph", svg)
        self.assertIn("parent", svg)
        self.assertIn("leaf_a", svg)
        self.assertIn("leaf_b", svg)
        self.assertEqual(metadata["sampleCount"], 3)
        self.assertEqual(metadata["source"], "perf-script.txt")


if __name__ == "__main__":
    unittest.main()
