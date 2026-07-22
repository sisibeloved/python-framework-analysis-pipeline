"""TDD coverage for deterministic black-box operator planning."""

from __future__ import annotations

import copy
import unittest

from pyframework_pipeline.adapters.volcoperatorsim.operator_plan import (
    build_operator_plan,
    build_stage_snapshot_id,
    render_full_task,
    render_isolated_task,
    render_snapshot_task,
)


REVISION = "56d3b6856895427a0519cbaa437d55443fcb578b"


def _formal_config() -> dict:
    return {
        "groups": {"core_dual_engine": {"tasks": ["pipeline_text"]}},
        "pipelines": {
            "pipeline_text": {
                "task": "pipeline_text",
                "modality": "text",
                "engines": ["daft_ray", "datajuicer_native"],
            }
        },
    }


def _task() -> dict:
    return {
        "task_id": "pipeline_text@v0",
        "input": {
            "kind": "lance",
            "path": "fixtures/text.lance",
            "jsonl_mirror": "fixtures/text.jsonl",
            "mirror_meta": "fixtures/text.meta.json",
        },
        "pipeline": [
            {"dj_ops": "clean_html_mapper", "category": "mapper"},
            {
                "dj_ops": "text_length_filter",
                "category": "filter",
                "params": {"min_len": 5},
            },
            {
                "dj_ops": "write_lance",
                "category": "sink",
                "params": {"output_uri": "fixtures/out.lance"},
            },
        ],
        "engine_overrides": {"ray_num_cpus": 16, "dj_np": 16},
    }


class OperatorPlanTest(unittest.TestCase):
    def test_plan_can_select_one_pipeline_from_a_larger_formal_group(self) -> None:
        formal = _formal_config()
        formal["groups"]["core_dual_engine"]["tasks"].append("pipeline_other")
        formal["pipelines"]["pipeline_other"] = {
            "task": "pipeline_other",
            "modality": "text",
            "engines": ["daft_ray"],
        }
        other_task = _task()
        other_task["task_id"] = "pipeline_other@v0"

        plan = build_operator_plan(
            formal_config=formal,
            task_documents={
                "pipeline_text": _task(),
                "pipeline_other": other_task,
            },
            group="core_dual_engine",
            selected_pipelines=("pipeline_other",),
            run_id="run-1",
            platform="arm",
            source_revision=REVISION,
        )

        self.assertEqual(
            [task["pipelineId"] for task in plan["tasks"]],
            ["pipeline_other"],
        )

    def test_plan_rejects_selected_pipeline_outside_the_formal_group(self) -> None:
        with self.assertRaisesRegex(ValueError, "not in formal group"):
            build_operator_plan(
                formal_config=_formal_config(),
                task_documents={"pipeline_text": _task()},
                group="core_dual_engine",
                selected_pipelines=("pipeline_missing",),
                run_id="run-1",
                platform="arm",
                source_revision=REVISION,
            )

    def test_plan_excludes_sink_and_chains_snapshot_input(self) -> None:
        plan = build_operator_plan(
            formal_config=_formal_config(),
            task_documents={"pipeline_text": _task()},
            group="core_dual_engine",
            run_id="run-1",
            platform="arm",
            source_revision=REVISION,
        )

        self.assertEqual(plan["schemaVersion"], 1)
        self.assertEqual(plan["sourceRevision"], REVISION)
        task = plan["tasks"][0]
        self.assertEqual(task["pipelineId"], "pipeline_text")
        self.assertEqual(task["taskSpecId"], "pipeline_text@v0")
        self.assertEqual(len(task["operators"]), 2)
        self.assertEqual(task["operators"][0]["input"]["kind"], "canonical")
        self.assertEqual(
            task["operators"][0]["input"]["spec"]["input_fingerprint"],
            task["operators"][0]["input"]["fingerprint"],
        )
        self.assertEqual(task["operators"][1]["input"]["kind"], "snapshot")
        self.assertEqual(
            task["operators"][1]["input"]["snapshotId"],
            task["snapshots"][0]["snapshotId"],
        )
        self.assertEqual(
            task["operators"][0]["engines"],
            ["daft_ray", "datajuicer_native"],
        )
        self.assertEqual(task["snapshots"][0]["builderVersion"], "4")
        self.assertEqual(task["snapshots"][0]["logicalField"], "text")
        self.assertEqual(task["pseudoStages"][0]["operatorId"], "__write_lance__")

    def test_snapshot_id_changes_with_revision_input_or_task_document(self) -> None:
        task = _task()
        base = build_stage_snapshot_id(
            task_spec_id="pipeline_text@v0",
            task_document=task,
            through_order=0,
            source_revision=REVISION,
            input_fingerprint="sha256:one",
        )
        changed_revision = build_stage_snapshot_id(
            task_spec_id="pipeline_text@v0",
            task_document=task,
            through_order=0,
            source_revision="a" * 40,
            input_fingerprint="sha256:one",
        )
        changed_input = build_stage_snapshot_id(
            task_spec_id="pipeline_text@v0",
            task_document=task,
            through_order=0,
            source_revision=REVISION,
            input_fingerprint="sha256:two",
        )
        changed_task = copy.deepcopy(task)
        changed_task["pipeline"][0]["params"] = {"x": 1}
        changed_document = build_stage_snapshot_id(
            task_spec_id="pipeline_text@v0",
            task_document=changed_task,
            through_order=0,
            source_revision=REVISION,
            input_fingerprint="sha256:one",
        )

        self.assertEqual(len({base, changed_revision, changed_input, changed_document}), 4)
        self.assertRegex(base, r"^[0-9a-f]{16}$")

    def test_plan_fails_loudly_when_group_task_document_is_missing(self) -> None:
        with self.assertRaisesRegex(ValueError, "pipeline_text"):
            build_operator_plan(
                formal_config=_formal_config(),
                task_documents={},
                group="core_dual_engine",
                run_id="run-1",
                platform="arm",
                source_revision=REVISION,
            )

    def test_sink_in_middle_is_rejected(self) -> None:
        task = _task()
        task["pipeline"] = [task["pipeline"][2], task["pipeline"][0]]
        with self.assertRaisesRegex(ValueError, "sink.*trailing"):
            build_operator_plan(
                formal_config=_formal_config(),
                task_documents={"pipeline_text": task},
                group="core_dual_engine",
                run_id="run-1",
                platform="arm",
                source_revision=REVISION,
            )

    def test_engine_policy_marks_unsupported_without_fabricating_a_case(self) -> None:
        task = _task()
        task["pipeline"][0]["supported_engines"] = ["daft_ray"]
        task["pipeline"][1]["isolation"] = "unsupported"
        task["pipeline"][1]["isolation_reason"] = "requires external state"

        plan = build_operator_plan(
            formal_config=_formal_config(),
            task_documents={"pipeline_text": task},
            group="core_dual_engine",
            run_id="run-1",
            platform="arm",
            source_revision=REVISION,
        )

        first, second = plan["tasks"][0]["operators"]
        self.assertEqual(first["engines"], ["daft_ray"])
        self.assertEqual(first["isolationStatus"], "supported")
        self.assertEqual(second["engines"], [])
        self.assertEqual(second["isolationStatus"], "unsupported")
        self.assertEqual(second["isolationReason"], "requires external state")

    def test_motion_filter_uses_object_layer_snapshot_before_frame_extraction(self) -> None:
        formal = {
            "groups": {"core_dual_engine": {"tasks": ["video"]}},
            "pipelines": {
                "video": {
                    "modality": "video",
                    "engines": ["daft_ray", "datajuicer_native"],
                }
            },
        }
        task = {
            "task_id": "video@v0",
            "input": {"kind": "lance", "field": "file_path"},
            "pipeline": [
                {"dj_ops": "video_resize_resolution_mapper", "category": "mapper"},
                {"dj_ops": "video_resolution_filter", "category": "filter"},
                {
                    "dj_ops": "video_extract_frames_mapper",
                    "category": "mapper",
                    "params": {"frame_sampling_method": "all_keyframes"},
                },
                {
                    "dj_ops": "video_motion_score_filter",
                    "category": "filter",
                    "params": {"min_score": 0.0},
                },
            ],
        }

        plan = build_operator_plan(
            formal_config=formal,
            task_documents={"video": task},
            group="core_dual_engine",
            run_id="run-1",
            platform="arm",
            source_revision=REVISION,
        )

        planned = plan["tasks"][0]
        motion = planned["operators"][3]
        snapshot_after_filter = next(
            item for item in planned["snapshots"] if item["afterOrder"] == 1
        )
        self.assertEqual(motion["inputSnapshotAfterOrder"], 1)
        self.assertEqual(
            motion["input"]["snapshotId"], snapshot_after_filter["snapshotId"]
        )
        self.assertEqual(
            motion["inputRouting"]["mode"],
            "object_layer_before_frame_extraction",
        )
        self.assertIn("video_extract_frames_mapper", motion["inputRouting"]["reason"])


class DerivedTaskTest(unittest.TestCase):
    def test_full_task_redirects_sink_and_records_measurement_scope(self) -> None:
        task = _task()

        full = render_full_task(
            task,
            measurement_scope="pipeline_e2e",
            output_uri="/home/lxy/de_bench_full/results/vectorize-10k.lance",
            engine_overrides={
                "materialize_policy": "end",
                "timing_tier": "p0",
                "include_write_lance_in_elapsed": True,
            },
        )

        self.assertEqual(full["input"], task["input"])
        self.assertEqual(full["pipeline"][:-1], task["pipeline"][:-1])
        self.assertEqual(
            full["pipeline"][-1]["params"]["output_uri"],
            "/home/lxy/de_bench_full/results/vectorize-10k.lance",
        )
        self.assertEqual(full["metadata"]["measurementScope"], "pipeline_e2e")
        self.assertEqual(full["metadata"]["sourceTaskSpecId"], "pipeline_text@v0")
        self.assertEqual(full["engine_overrides"]["materialize_policy"], "end")
        self.assertEqual(full["engine_overrides"]["ray_num_cpus"], 16)
        self.assertEqual(task["pipeline"][-1]["params"]["output_uri"], "fixtures/out.lance")

    def test_isolated_task_keeps_operator_params_and_locks_attribution_profile(self) -> None:
        task = _task()
        input_spec = {
            "kind": "lance",
            "path": "/home/lxy/de_bench_full/operator-cache/s1/data.lance",
            "jsonl_mirror": "/home/lxy/de_bench_full/operator-cache/s1/data.jsonl",
            "mirror_meta": "/home/lxy/de_bench_full/operator-cache/s1/meta.json",
        }

        isolated = render_isolated_task(task, order=1, input_spec=input_spec)

        self.assertEqual(isolated["input"], input_spec)
        self.assertEqual(isolated["pipeline"], [task["pipeline"][1]])
        self.assertEqual(isolated["engine_overrides"]["materialize_policy"], "per_op")
        self.assertEqual(isolated["engine_overrides"]["timing_tier"], "p1")
        self.assertFalse(isolated["engine_overrides"]["fuse_mappers"])
        self.assertEqual(isolated["engine_overrides"]["ray_num_cpus"], 16)

    def test_isolated_filter_allows_a_legitimate_empty_result_only_for_filters(self) -> None:
        task = _task()
        input_spec = task["input"]

        mapper = render_isolated_task(task, order=0, input_spec=input_spec)
        filter_task = render_isolated_task(task, order=1, input_spec=input_spec)

        self.assertNotIn("allow_empty_output", mapper["engine_overrides"])
        self.assertTrue(filter_task["engine_overrides"]["allow_empty_output"])
        self.assertTrue(filter_task["metadata"]["emptyOutputIsValid"])

    def test_snapshot_task_is_prefix_plus_write_lance_outside_measurement(self) -> None:
        task = _task()
        snapshot = render_snapshot_task(
            task,
            through_order=0,
            output_uri="/home/lxy/de_bench_full/operator-cache/s1/data.lance",
        )

        self.assertEqual(snapshot["pipeline"][0], task["pipeline"][0])
        self.assertEqual(snapshot["pipeline"][1]["dj_ops"], "write_lance")
        self.assertEqual(
            snapshot["pipeline"][1]["params"]["output_uri"],
            "/home/lxy/de_bench_full/operator-cache/s1/data.lance",
        )
        self.assertTrue(snapshot["engine_overrides"]["include_write_lance_in_elapsed"])
        self.assertEqual(snapshot["metadata"]["measurementScope"], "snapshot_build")


if __name__ == "__main__":
    unittest.main()
