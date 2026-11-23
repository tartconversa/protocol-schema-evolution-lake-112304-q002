"""兼容性裁决与分组测试。"""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

import support
from protocol_schema_evolution_lake.compatibility import (
    assess_compatibility,
    build_groups,
)
from protocol_schema_evolution_lake.model import (
    OUTCOME_CLAIM_CONTRADICTED,
    OUTCOME_MERGEABLE,
    OUTCOME_REVIEW_MISSING,
    OUTCOME_SEMANTIC_CHANGE,
    OUTCOME_SPLIT_INCOMPLETE,
    OUTCOME_SPLIT_REOBSERVE,
    OUTCOME_UNRESOLVED,
    TransformSpec,
)
from protocol_schema_evolution_lake.registry import load_registry


def load_fixture(name: str):
    root = support.FIXTURES / name
    return load_registry(root, **support.governance_paths(root))


class ShippedDataAssessmentTests(unittest.TestCase):
    """随仓库提供的合成资料：映射声明与物理定义几乎全部矛盾。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry(support.DATA)
        cls.assessments, cls.issues = assess_compatibility(cls.registry)

    def test_nothing_is_mergeable(self) -> None:
        self.assertEqual(len(self.assessments), 16)
        self.assertEqual([item for item in self.assessments if item.mergeable], [])

    def test_contradicted_claims_detected(self) -> None:
        contradicted = [item for item in self.assessments if item.outcome == OUTCOME_CLAIM_CONTRADICTED]
        self.assertEqual(len(contradicted), 8)
        converted = [
            item
            for item in contradicted
            if item.claim == "unit-convert"
        ]
        self.assertTrue(converted)
        for item in converted:
            self.assertTrue(
                any("源字段名" in reason and "目标字段名" in reason for reason in item.reasons)
            )

    def test_unresolved_claims_are_not_treated_as_additive(self) -> None:
        unresolved = [item for item in self.assessments if item.outcome == OUTCOME_UNRESOLVED]
        self.assertEqual(len(unresolved), 4)
        for item in unresolved:
            self.assertIsNone(item.transform)
            self.assertFalse(item.mergeable)

    def test_incomplete_split_reported(self) -> None:
        # 声明 split 但每个源字段只登记了一个目标字段。
        incomplete = [item for item in self.assessments if item.outcome == OUTCOME_SPLIT_INCOMPLETE]
        self.assertEqual(len(incomplete), 4)
        self.assertTrue(all(item.claim == "split" for item in incomplete))

    def test_groups_are_all_version_only(self) -> None:
        groups, metrics, _ = build_groups(self.registry, self.assessments)
        self.assertEqual(len(groups), 16)
        for group in groups:
            self.assertFalse(group.merged)
            # 单版本组仍可产出自己的分区（buildable），但不可跨版本比较。
            self.assertTrue(group.buildable)
            self.assertEqual(group.status, "version_only")
        self.assertEqual(len(metrics), 16)

    def test_annotation_dispute_surfaces_in_groups(self) -> None:
        groups, _, _ = build_groups(self.registry, self.assessments)
        for group in groups:
            self.assertEqual(group.annotation_status, "disputed")


class CoherentFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_fixture("coherent")
        cls.assessments, _ = assess_compatibility(cls.registry)
        cls.by_source = {
            (item.key.source_field, item.key.target_field): item
            for item in cls.assessments
        }

    def test_same_quantity_is_mergeable(self) -> None:
        item = self.by_source[("F-01", "F-01")]
        self.assertEqual(item.outcome, OUTCOME_MERGEABLE)
        assert item.transform is not None
        self.assertTrue(item.transform.is_identity if hasattr(item.transform, "is_identity") else item.transform.kind == "identity")
        self.assertIn("REV-001", item.semantic_basis)

    def test_unit_conversion_uses_approved_factor(self) -> None:
        item = self.by_source[("F-02", "F-02")]
        self.assertEqual(item.outcome, OUTCOME_MERGEABLE)
        assert item.transform is not None
        self.assertEqual(item.transform.factor, Decimal("0.001"))
        self.assertEqual(item.transform.offset, Decimal("0"))
        self.assertEqual(item.transform.source_unit, "g")
        self.assertEqual(item.transform.target_unit, "kg")

    def test_renamed_only_field_is_mergeable(self) -> None:
        item = self.by_source[("F-06", "F-06")]
        self.assertEqual(item.outcome, OUTCOME_MERGEABLE)
        self.assertTrue(any("名称由" in reason for reason in item.reasons))

    def test_changed_meaning_is_blocked(self) -> None:
        item = self.by_source[("F-07", "F-07")]
        self.assertEqual(item.outcome, OUTCOME_SEMANTIC_CHANGE)
        self.assertFalse(item.mergeable)

    def test_split_requires_reobservation(self) -> None:
        for target in ("F-04", "F-05"):
            item = self.by_source[("F-03", target)]
            self.assertEqual(item.outcome, OUTCOME_SPLIT_REOBSERVE)
            self.assertFalse(item.mergeable)
            self.assertIsNone(item.transform)

    def test_groups_merge_only_compatible_fields(self) -> None:
        groups, metrics, _ = build_groups(self.registry, self.assessments)
        by_member = {}
        for group in groups:
            for member in group.members:
                by_member[member] = group
        self.assertEqual(by_member[("v1", "F-01")].status, "cross_version_comparable")
        self.assertEqual(by_member[("v1", "F-02")].status, "cross_version_comparable")
        self.assertEqual(by_member[("v1", "F-06")].status, "cross_version_comparable")
        # 拆分与语义变更的字段必须保持单版本。
        self.assertFalse(by_member[("v1", "F-03")].merged)
        self.assertFalse(by_member[("v2", "F-04")].merged)
        self.assertFalse(by_member[("v2", "F-05")].merged)
        self.assertFalse(by_member[("v1", "F-07")].merged)
        self.assertFalse(by_member[("v2", "F-08")].merged)

    def test_canonical_transform_map_handles_chains(self) -> None:
        # 质量组应能给出 v1→规范单位(kg) 的换算。
        groups, _, _ = build_groups(self.registry, self.assessments)
        group = next(item for item in groups if item.canonical_field == "F-02")
        transforms = {(version, field_id): spec for version, field_id, spec in group.transforms}
        self.assertEqual(transforms[("v2", "F-02")].kind, "identity")
        self.assertEqual(transforms[("v1", "F-02")].factor, Decimal("0.001"))


class AdjudicatedFixtureTests(unittest.TestCase):
    def test_adjudication_resolves_mapping_conflict(self) -> None:
        registry = load_fixture("adjudicated")
        assessments, _ = assess_compatibility(registry)
        item = next(
            entry
            for entry in assessments
            if entry.key.source_field == "F-01" and entry.key.target_field == "F-01"
        )
        self.assertEqual(item.outcome, OUTCOME_MERGEABLE)
        self.assertEqual(item.adjudication_id, "ADJ-001")
        self.assertTrue(item.decision_conflict)  # 原始记录确实冲突

    def test_unadjudicated_conflict_blocks_merge(self) -> None:
        registry = load_registry(
            support.FIXTURES / "adjudicated",
            reviews_path=support.FIXTURES / "adjudicated" / "semantic_reviews.csv",
            conversions_path=support.FIXTURES / "adjudicated" / "unit_conversions.csv",
        )
        assessments, _ = assess_compatibility(registry)
        item = next(
            entry
            for entry in assessments
            if entry.key.source_field == "F-01" and entry.key.target_field == "F-01"
        )
        self.assertEqual(item.outcome, "governance_conflict")
        self.assertFalse(item.mergeable)


class GovernanceGateTests(unittest.TestCase):
    def test_missing_review_blocks_merge(self) -> None:
        root = support.FIXTURES / "coherent"
        registry = load_registry(
            root,
            conversions_path=root / "unit_conversions.csv",
        )
        assessments, _ = assess_compatibility(registry)
        item = next(
            entry
            for entry in assessments
            if entry.key.source_field == "F-01" and entry.key.target_field == "F-01"
        )
        self.assertEqual(item.outcome, OUTCOME_REVIEW_MISSING)
        self.assertFalse(item.mergeable)

    def test_missing_conversion_blocks_merge(self) -> None:
        root = support.FIXTURES / "coherent"
        registry = load_registry(
            root,
            reviews_path=root / "semantic_reviews.csv",
        )
        assessments, _ = assess_compatibility(registry)
        item = next(
            entry
            for entry in assessments
            if entry.key.source_field == "F-02" and entry.key.target_field == "F-02"
        )
        self.assertEqual(item.outcome, "unit_conversion_missing")
        self.assertFalse(item.mergeable)

    def test_changed_meaning_without_review_blocks_merge(self) -> None:
        # 即使映射声明 same，物理一致但语义评审判为 changed_meaning 也不合并。
        root = support.FIXTURES / "coherent"
        registry = load_registry(
            root,
            reviews_path=root / "semantic_reviews.csv",
            conversions_path=root / "unit_conversions.csv",
        )
        assessments, _ = assess_compatibility(registry)
        item = next(
            entry
            for entry in assessments
            if entry.key.source_field == "F-07" and entry.key.target_field == "F-07"
        )
        self.assertEqual(item.outcome, OUTCOME_SEMANTIC_CHANGE)

    def test_reverse_registered_conversion_is_inverted(self) -> None:
        # 登记为 kg→g 的换算，遇到 g→kg 的映射时应取逆变换。
        import csv
        import shutil
        import tempfile

        root = support.FIXTURES / "coherent"
        with tempfile.TemporaryDirectory() as temp:
            copy_dir = Path(temp)
            for name in ("protocols.csv", "schema_fields.csv", "run_results.csv",
                         "mappings.csv", "semantic_reviews.csv", "unit_conversions.csv"):
                shutil.copy(root / name, copy_dir / name)
            conversion_file = copy_dir / "unit_conversions.csv"
            with conversion_file.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                columns = reader.fieldnames
                rows = list(reader)
            for row in rows:
                row["source_unit"], row["target_unit"], row["factor"] = "kg", "g", "1000"
            with conversion_file.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            registry = load_registry(
                copy_dir,
                reviews_path=copy_dir / "semantic_reviews.csv",
                conversions_path=conversion_file,
            )
            assessments, _ = assess_compatibility(registry)
            item = next(
                entry
                for entry in assessments
                if entry.key.source_field == "F-02" and entry.key.target_field == "F-02"
            )
            self.assertEqual(item.outcome, OUTCOME_MERGEABLE)
            self.assertEqual(item.transform.factor, Decimal("0.001"))
            self.assertTrue(any("反向" in reason for reason in item.reasons))


class TransformSpecTests(unittest.TestCase):
    def test_identity_and_inverse(self) -> None:
        spec = TransformSpec.identity("C")
        self.assertEqual(spec.apply(Decimal("3")), Decimal("3"))
        inverse = spec.inverse()
        self.assertEqual(inverse.apply(Decimal("4")), Decimal("4"))

    def test_linear_inverse_round_trip(self) -> None:
        spec = TransformSpec(
            "linear", Decimal("0.001"), Decimal("2"), "g", "kg", "test"
        )
        inverse = spec.inverse()
        value = Decimal("1234")
        round_tripped = inverse.apply(spec.apply(value))
        self.assertEqual(round_tripped, value)

    def test_composition(self) -> None:
        first = TransformSpec("linear", Decimal("2"), Decimal("1"), "a", "b", "x")
        second = TransformSpec("linear", Decimal("3"), Decimal("4"), "b", "c", "y")
        combined = first.then(second)
        value = Decimal("5")
        self.assertEqual(combined.apply(value), second.apply(first.apply(value)))
        self.assertEqual(combined.source_unit, "a")
        self.assertEqual(combined.target_unit, "c")

    def test_cycle_detection_blocks_inconsistent_group(self) -> None:
        from protocol_schema_evolution_lake.model import (
            EdgeAssessment,
            MappingKey,
            MappingDecision,
            MappingEdge,
        )

        def make_assessment(sv, sf, tv, tf, factor):
            key = MappingKey(sv, sf, tv, tf)
            decision = MappingDecision(
                mapping_id="X", sequence=1, key=key, transform_note="same",
                approval_state="approved", source_line=1,
            )
            edge = MappingEdge(key=key, decisions=(decision,))
            return EdgeAssessment(
                key=key,
                claim="same",
                latest_state="approved",
                evidence_status="valid",
                outcome=OUTCOME_MERGEABLE,
                reasons=(),
                transform=TransformSpec("linear", Decimal(factor), Decimal("0"), "u", "u", "t"),
                semantic_basis="test",
                review_id="R",
                review_state="approved",
                decision_conflict=False,
                note_conflict=False,
                review_conflict=False,
                history=edge.history,
            )

        from protocol_schema_evolution_lake.compatibility import _enforce_transform_cycles

        # a→b 因子 2；b→c 因子 4；c→a 因子 1/8。绕一圈 2*4*(1/8)=1 自洽。
        consistent = [
            make_assessment("a", "F", "b", "F", "2"),
            make_assessment("b", "F", "c", "F", "4"),
            make_assessment("c", "F", "a", "F", "0.125"),
        ]
        issues = []
        result = _enforce_transform_cycles(consistent, issues)
        self.assertTrue(all(item.mergeable for item in result))

        inconsistent = [
            make_assessment("a", "F", "b", "F", "2"),
            make_assessment("b", "F", "c", "F", "3"),
            make_assessment("c", "F", "a", "F", "2"),
        ]
        issues = []
        result = _enforce_transform_cycles(inconsistent, issues)
        self.assertFalse(any(item.mergeable for item in result))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "transform_cycle_inconsistent")


class IntraGroupConflictTests(unittest.TestCase):
    """共享上游字段不应让彼此被否决的字段变成可比。"""

    def test_rejected_sibling_edge_blocks_group(self) -> None:
        from protocol_schema_evolution_lake.compatibility import build_groups
        from protocol_schema_evolution_lake.model import (
            EdgeAssessment,
            MappingDecision,
            MappingEdge,
            MappingKey,
        )

        def edge(key, outcome, transform=None):
            decision = MappingDecision(
                mapping_id="M", sequence=1, key=key, transform_note="same",
                approval_state="approved", source_line=1,
            )
            return EdgeAssessment(
                key=key,
                claim="same",
                latest_state="approved",
                evidence_status="valid" if outcome == OUTCOME_MERGEABLE else "insufficient",
                outcome=outcome,
                reasons=(),
                transform=transform,
                semantic_basis="test",
                review_id="R",
                review_state="approved",
                decision_conflict=False,
                note_conflict=False,
                review_conflict=False,
                history=MappingEdge(key=key, decisions=(decision,)).history,
            )

        identity = TransformSpec.identity("u")
        a_b = MappingKey("v1", "A", "v2", "B")
        a_c = MappingKey("v1", "A", "v3", "C")
        b_c = MappingKey("v2", "B", "v3", "C")

        class FakeRegistry:
            version_fields = {
                ("v1", "A"): _vf("v1", "A"),
                ("v2", "B"): _vf("v2", "B"),
                ("v3", "C"): _vf("v3", "C"),
            }
            versions = ("v1", "v2", "v3")

        assessments = (
            edge(a_b, OUTCOME_MERGEABLE, identity),
            edge(a_c, OUTCOME_MERGEABLE, identity),
            edge(b_c, "rejected"),
        )
        groups, _, _ = build_groups(FakeRegistry(), assessments)
        group = next(item for item in groups if ("v1", "A") in item.members)
        self.assertTrue(group.merged)  # 传递合并确实发生了
        self.assertFalse(group.buildable)
        self.assertEqual(group.status, "version_only")
        self.assertTrue(any("显式的不可合并裁决" in reason for reason in group.reasons))


def _vf(version: str, field_id: str):
    from protocol_schema_evolution_lake.model import Annotation, LabFieldView, VersionField

    return VersionField(
        version=version,
        field_id=field_id,
        views=(LabFieldView("P", "n", "float", "u", (Annotation("S", True),)),),
    )


if __name__ == "__main__":
    unittest.main()
