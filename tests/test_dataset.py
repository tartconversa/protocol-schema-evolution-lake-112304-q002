"""数据集生成、发布、不可变性与版本追溯测试。"""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
import unittest

import support
from protocol_schema_evolution_lake.dataset import (
    BuildParameters,
    affected_partitions_for_dataset,
    build_dataset,
    compare_partitions,
    content_digest,
    load_catalog,
    next_dataset_version,
    publish,
    rebuild_parameters,
    resolve_parameters,
    trace_dataset,
    verify_dataset,
)
from protocol_schema_evolution_lake.errors import (
    ImmutableArtifactError,
    PublicationError,
)
from protocol_schema_evolution_lake.registry import load_registry


def load_fixture(name: str):
    root = support.FIXTURES / name
    return load_registry(root, **support.governance_paths(root))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


class ParameterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_fixture("coherent")

    def test_defaults_include_approved_only(self) -> None:
        params = resolve_parameters(self.registry)
        self.assertEqual(params.include_states, ("approved",))
        self.assertEqual(params.versions, ("v1", "v2"))

    def test_string_inputs_are_split(self) -> None:
        params = resolve_parameters(
            self.registry,
            include_states="approved,draft",
            versions="v2,v1",
            fields="v1/F-01, v2/F-02",
        )
        self.assertEqual(params.include_states, ("approved", "draft"))
        self.assertEqual(params.versions, ("v1", "v2"))
        self.assertEqual(params.fields, ("v1/F-01, v2/F-02",))

    def test_unknown_version_rejected(self) -> None:
        with self.assertRaises(PublicationError):
            resolve_parameters(self.registry, versions="v9")

    def test_parameters_round_trip(self) -> None:
        params = BuildParameters(
            include_states=("approved",),
            versions=("v1", "v2"),
            target_version="v2",
            fields=("v1/F-01",),
        )
        restored = BuildParameters.from_dict(params.to_dict())
        self.assertEqual(restored, params)


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_fixture("coherent")
        cls.build = build_dataset(
            cls.registry,
            "coherent",
            resolve_parameters(cls.registry),
            created_at="2026-04-01T00:00:00Z",
        )

    def test_units_are_normalized_to_canonical(self) -> None:
        partition = next(
            item for item in self.build.partitions if item.group.group_id == "G-v1-F-02"
        )
        v2_rows = [row for row in partition.rows if row.protocol_version == "v2"]
        v1_rows = [row for row in partition.rows if row.protocol_version == "v1"]
        self.assertTrue(v2_rows and v1_rows)
        for row in v1_rows:
            self.assertEqual(row.measure_unit, "kg")
            self.assertEqual(row.original_unit, "g")
            self.assertEqual(
                _format(float(row.original_value) * 0.001),
                row.measure_value,
            )

    def test_incomparable_fields_stay_separate(self) -> None:
        group_ids = {partition.group.group_id for partition in self.build.partitions}
        self.assertIn("G-v1-F-03", group_ids)
        self.assertIn("G-v2-F-04", group_ids)
        self.assertIn("G-v2-F-05", group_ids)
        # 拆分与语义变更字段不会合到同一分区。
        self.assertNotEqual(
            next(
                item.group.group_id
                for item in self.build.partitions
                if ("v1", "F-03") in item.group.members
            ),
            next(
                item.group.group_id
                for item in self.build.partitions
                if ("v2", "F-04") in item.group.members
            ),
        )

    def test_original_values_preserved_in_rows(self) -> None:
        for partition in self.build.partitions:
            for row in partition.rows:
                self.assertEqual(row.original_value, row.original_value.strip())
                self.assertNotEqual(row.original_value, "")

    def test_metrics_report_comparability(self) -> None:
        metrics = {(row.version, row.field_id): row for row in self.build.metrics}
        self.assertEqual(metrics[("v1", "F-01")].status, "cross_version_comparable")
        self.assertEqual(metrics[("v2", "F-04")].status, "version_only")
        self.assertEqual(metrics[("v2", "F-08")].status, "version_only")

    def test_content_digest_is_stable(self) -> None:
        again = build_dataset(
            self.registry,
            "coherent",
            resolve_parameters(self.registry),
            created_at="2026-04-01T00:00:00Z",
        )
        self.assertEqual(content_digest(self.build), content_digest(again))

    def test_unsafe_dataset_name_rejected(self) -> None:
        for bad in ("../evil", "a/b", ".hidden", ""):
            with self.subTest(name=bad):
                with self.assertRaises(PublicationError):
                    build_dataset(self.registry, bad, resolve_parameters(self.registry))

    def test_version_filter_excludes_rows(self) -> None:
        build = build_dataset(
            self.registry, "v2-only", resolve_parameters(self.registry, versions="v2")
        )
        for partition in build.partitions:
            for row in partition.rows:
                self.assertEqual(row.protocol_version, "v2")

    def test_field_filter_limits_scope(self) -> None:
        build = build_dataset(
            self.registry,
            "one-field",
            resolve_parameters(self.registry, fields="v1/F-01,v2/F-01"),
        )
        group_ids = {partition.group.group_id for partition in build.partitions}
        self.assertEqual(group_ids, {"G-v1-F-01"})


def _format(value: float) -> str:
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return text or "0"


class PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.lake = Path(self.tempdir.name) / "lake"
        self.registry = load_fixture("coherent")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _publish(self, version="v1", *, name="coherent", supersedes=None, **params):
        build = build_dataset(
            self.registry,
            name,
            resolve_parameters(self.registry, **params),
            created_at="2026-04-01T00:00:00Z",
        )
        return publish(
            build, self.registry, self.lake, version, supersedes=supersedes
        ), build

    def test_publish_creates_immutable_artifacts(self) -> None:
        result, build = self._publish()
        self.assertTrue(result.created)
        directory = result.directory
        for relative in ("manifest.json", "lineage.json", "registry_snapshot.json",
                         "governance_snapshot.json", "metrics.csv", "compatibility.csv",
                         "exclusions.csv", "issues.csv"):
            self.assertTrue((directory / relative).is_file(), relative)
        self.assertTrue(list((directory / "partitions").glob("*.csv")))
        self.assertEqual(verify_dataset(self.lake, "coherent", "v1"), [])

    def test_repeat_publish_same_content_is_idempotent(self) -> None:
        first, _ = self._publish()
        second, _ = self._publish()
        self.assertFalse(second.created)
        self.assertEqual(first.manifest["content_digest"], second.manifest["content_digest"])

    def test_publish_different_content_same_version_rejected(self) -> None:
        self._publish("v1")
        with self.assertRaises(ImmutableArtifactError):
            self._publish("v1", versions="v2")

    def test_verify_detects_tampering(self) -> None:
        result, _ = self._publish()
        partition = sorted((result.directory / "partitions").glob("*.csv"))[0]
        partition.write_text(partition.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        problems = verify_dataset(self.lake, "coherent", "v1")
        self.assertTrue(any("哈希不一致" in item for item in problems))

    def test_supersedes_preserves_old_and_marks_affected(self) -> None:
        self._publish("v1")
        old_digest = verify_dataset(self.lake, "coherent", "v1")
        self.assertEqual(old_digest, [])
        old_lineage = (self.lake / "datasets" / "coherent" / "v1" / "lineage.json").read_text(
            encoding="utf-8"
        )
        build2 = build_dataset(
            self.registry,
            "coherent",
            resolve_parameters(self.registry, target_version="v2"),
            created_at="2026-04-02T00:00:00Z",
        )
        result = publish(build2, self.registry, self.lake, "v2", supersedes="v1")
        self.assertTrue(result.created)
        # 旧版本目录内容未被改写。
        self.assertEqual(
            (self.lake / "datasets" / "coherent" / "v1" / "lineage.json").read_text(
                encoding="utf-8"
            ),
            old_lineage,
        )
        # 账本记录替代关系与受影响分区。
        status = affected_partitions_for_dataset(self.lake, "coherent", "v1")
        self.assertEqual(status["superseded_by"], "v2")
        # 新版本清单记录 supersedes 与比较结果。
        manifest = json.loads(
            (self.lake / "datasets" / "coherent" / "v2" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["supersedes"], "v1")
        self.assertIn("affected", manifest["affected_partitions"])

    def test_supersedes_requires_existing_target(self) -> None:
        with self.assertRaises(PublicationError):
            self._publish("v1", supersedes="v9")

    def test_rebuild_parameters_round_trip(self) -> None:
        self._publish("v1", versions="v2", target_version="v2")
        params = rebuild_parameters(self.lake, "coherent", "v1")
        self.assertEqual(params.versions, ("v2",))
        self.assertEqual(params.target_version, "v2")

    def test_catalog_tracks_versions(self) -> None:
        self._publish("v1")
        catalog = load_catalog(self.lake)
        entry = catalog["datasets"]["coherent"]
        self.assertEqual(entry["latest"], "v1")
        self.assertEqual(len(entry["versions"]), 1)
        self.assertEqual(next_dataset_version(self.lake, "coherent"), "v2")

    def test_catalog_reconciled_after_simulated_crash(self) -> None:
        self._publish("v1")
        # 模拟“目录已改名、账本尚未写入”时崩溃。
        (self.lake / "catalog.json").unlink()
        result, _ = self._publish("v1")
        self.assertFalse(result.created)
        catalog = load_catalog(self.lake)
        entries = catalog["datasets"]["coherent"]["versions"]
        self.assertEqual([entry["dataset_version"] for entry in entries], ["v1"])
        self.assertEqual(verify_dataset(self.lake, "coherent", "v1"), [])

    def test_no_staging_leftover(self) -> None:
        self._publish("v1")
        staging = self.lake / ".staging"
        self.assertFalse(staging.exists() and any(staging.iterdir()))

    def test_manifest_records_affected_partitions_relative_paths(self) -> None:
        self._publish("v1")
        build = build_dataset(
            self.registry,
            "coherent",
            resolve_parameters(self.registry, target_version="v2"),
            created_at="2026-04-02T00:00:00Z",
        )
        publish(build, self.registry, self.lake, "v2", supersedes="v1")
        manifest = json.loads(
            (self.lake / "datasets" / "coherent" / "v2" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        for relative in manifest["files"]:
            self.assertFalse(relative.startswith("datasets/"))
            self.assertNotEqual(relative, "manifest.json")


class TraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.lake = Path(self.tempdir.name) / "lake"
        self.registry = load_fixture("coherent")
        build = build_dataset(
            self.registry,
            "coherent",
            resolve_parameters(self.registry),
            created_at="2026-04-01T00:00:00Z",
        )
        publish(build, self.registry, self.lake, "v1")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_trace_includes_protocol_and_mapping_lineage(self) -> None:
        payload = trace_dataset(self.lake, "coherent", "v1")
        self.assertIn("protocol_lineage", payload)
        self.assertIn("mapping_lineage", payload)
        self.assertEqual(payload["publication"]["content_digest"], payload["publication"]["content_digest"])
        self.assertTrue(payload["fingerprints"]["mapping"])

    def test_trace_run(self) -> None:
        payload = trace_dataset(self.lake, "coherent", "v1", run_id="RUN-V2-01")
        run = payload["run"]
        assert run is not None
        self.assertEqual(run["protocol_version"], "v2")
        self.assertEqual(run["transform"], "identity")

    def test_trace_member(self) -> None:
        payload = trace_dataset(self.lake, "coherent", "v1", member="v1/F-02")
        field = payload["field"]
        assert field is not None
        self.assertEqual(field["status"], "cross_version_comparable")
        self.assertEqual(field["group_id"], "G-v1-F-02")


class ComparePartitionsTests(unittest.TestCase):
    def test_changed_and_added(self) -> None:
        previous = {"a": {"digest": "1"}, "b": {"digest": "2"}}
        current = {"b": {"digest": "2"}, "c": {"digest": "3"}}
        result = compare_partitions(previous, current)
        self.assertEqual(result["added"], ["c"])
        self.assertEqual(result["removed"], ["a"])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["affected"], ["a", "c"])

    def test_changed_digest_detected(self) -> None:
        previous = {"a": {"digest": "1"}}
        current = {"a": {"digest": "2"}}
        result = compare_partitions(previous, current)
        self.assertEqual(result["changed"], ["a"])


if __name__ == "__main__":
    unittest.main()
