"""登记装载与输入校验测试。"""

from __future__ import annotations

import csv
import shutil
import tempfile
from pathlib import Path
import unittest

import support  # noqa: F401  (设置 sys.path)
from protocol_schema_evolution_lake.errors import (
    ConflictingDuplicateError,
    PhysicalSchemaConflict,
    ReferentialIntegrityError,
    SourceFormatError,
)
from protocol_schema_evolution_lake.registry import load_registry


class RegistryLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_registry(support.DATA)

    def test_counts_and_versions(self) -> None:
        self.assertEqual(len(self.registry.protocols), 12)
        self.assertEqual(len(self.registry.fields), 48)
        self.assertEqual(len(self.registry.version_fields), 16)
        self.assertEqual(len(self.registry.runs), 160)
        self.assertEqual(self.registry.versions, ("v1", "v2", "v3", "v4"))

    def test_duplicate_protocol_rows_collapsed(self) -> None:
        # 资料中每条方案记录重复三次且完全一致；应去重为一条并给出 info。
        protocol = self.registry.protocol("PR-01", "v2")
        assert protocol is not None
        self.assertEqual(len(protocol.source_lines), 3)
        codes = [issue.code for issue in self.registry.issues]
        self.assertIn("duplicate_protocol_row", codes)

    def test_conflicting_annotations_preserved_not_dropped(self) -> None:
        # 同一字段的两行物理定义一致、治理注解冲突：保留全部注解并标记争议。
        field = self.registry.field("PR-01", "v2", "F-01")
        assert field is not None
        self.assertGreaterEqual(len(field.annotations), 2)
        self.assertEqual(field.annotation_status, "disputed")
        self.assertIsNone(field.semantic_ref)
        self.assertEqual(len(field.source_lines), 2)

    def test_annotation_conflict_issue_recorded(self) -> None:
        issues = [issue for issue in self.registry.issues if issue.code == "annotation_conflict"]
        self.assertEqual(len(issues), 32)

    def test_run_values_preserved_with_flags(self) -> None:
        int_runs = [
            run
            for run in self.registry.runs
            if run.declared_dtype == "int" and "declared_int_nonintegral" in run.value_flags
        ]
        self.assertEqual(len(int_runs), 53)
        for run in int_runs:
            # 原始文本不被截断或四舍五入。
            self.assertEqual(run.value_text, run.value_text.strip())
            self.assertNotEqual(run.numeric_value, run.numeric_value.to_integral_value())

    def test_string_typed_numeric_values_flagged(self) -> None:
        flagged = [run for run in self.registry.runs if "declared_string_numeric" in run.value_flags]
        self.assertEqual(len(flagged), 53)

    def test_mapping_edges_group_duplicates(self) -> None:
        self.assertEqual(len(self.registry.mapping_edges), 16)
        for edge in self.registry.mapping_edges.values():
            self.assertGreaterEqual(len(edge.decisions), 3)

    def test_mapping_fingerprint_is_deterministic(self) -> None:
        again = load_registry(support.DATA)
        self.assertEqual(self.registry.mapping_fingerprint, again.mapping_fingerprint)
        self.assertEqual(self.registry.registry_fingerprint, again.registry_fingerprint)

    def test_protocol_lineage_reports_missing_parents(self) -> None:
        lineage = self.registry.protocol_lineage()
        issues = [item for item in lineage["issues"] if item["code"] == "parent_version_missing"]
        # 12 个协议族中，9 个族的当前版本引用了本族未登记的父版本。
        self.assertEqual(len(issues), 9)
        # 显式根版本（parent_version == version）不应被误报为环。
        cycles = [item for item in lineage["issues"] if item["code"] == "parent_cycle"]
        self.assertEqual(cycles, [])
        families = lineage["families"]
        self.assertEqual(len(families), 12)

    def test_mapping_lineage_includes_history(self) -> None:
        entries = self.registry.mapping_lineage()
        self.assertEqual(len(entries), 16)
        for entry in entries:
            self.assertTrue(entry["history"])
            self.assertEqual(entry["latest_mapping_id"], entry["history"][-1]["mapping_id"])


class RegistryValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "data"
        shutil.copytree(support.DATA, self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _rewrite(self, filename: str, mutate) -> None:
        path = self.root / filename
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            rows = list(reader)
        rows = mutate(rows)
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def test_physical_schema_conflict_rejected(self) -> None:
        def mutate(rows):
            extra = dict(rows[0])
            extra["name"] = "different_name"
            extra["semantic_ref"] = "SEM-X"
            return rows + [extra]

        self._rewrite("schema_fields.csv", mutate)
        with self.assertRaises(PhysicalSchemaConflict):
            load_registry(self.root)

    def test_conflicting_protocol_duplicate_rejected(self) -> None:
        def mutate(rows):
            extra = dict(rows[0])
            extra["review_state"] = "retired"
            return rows + [extra]

        self._rewrite("protocols.csv", mutate)
        with self.assertRaises(ConflictingDuplicateError):
            load_registry(self.root)

    def test_unknown_protocol_reference_rejected(self) -> None:
        def mutate(rows):
            rows[0]["protocol_id"] = "PR-99"
            return rows

        self._rewrite("run_results.csv", mutate)
        with self.assertRaises(ReferentialIntegrityError):
            load_registry(self.root)

    def test_bad_review_state_rejected(self) -> None:
        def mutate(rows):
            for row in rows:
                if row["protocol_id"] == "PR-01" and row["version"] == "v2":
                    row["review_state"] = "archived"
            return rows

        self._rewrite("protocols.csv", mutate)
        with self.assertRaises(SourceFormatError):
            load_registry(self.root)

    def test_duplicate_run_id_rejected(self) -> None:
        def mutate(rows):
            rows[1]["run_id"] = rows[0]["run_id"]
            return rows

        self._rewrite("run_results.csv", mutate)
        with self.assertRaises(ConflictingDuplicateError):
            load_registry(self.root)


class GovernanceFileTests(unittest.TestCase):
    def test_coherent_fixture_loads_governance(self) -> None:
        root = support.FIXTURES / "coherent"
        registry = load_registry(root, **support.governance_paths(root))
        self.assertEqual(len(registry.reviews), 6)
        self.assertEqual(len(registry.conversions), 1)
        self.assertEqual(registry.adjudications, {})

    def test_invalid_reviewer_role_rejected(self) -> None:
        root = support.FIXTURES / "coherent"
        with tempfile.TemporaryDirectory() as temp:
            review_file = Path(temp) / "reviews.csv"
            text = (root / "semantic_reviews.csv").read_text(encoding="utf-8")
            review_file.write_text(text.replace("experiment_owner", "data_clerk", 1), encoding="utf-8")
            with self.assertRaises(Exception):
                load_registry(root, reviews_path=review_file)


if __name__ == "__main__":
    unittest.main()
