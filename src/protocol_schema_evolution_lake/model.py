"""领域模型：方案登记、字段、观察结果、映射决策与兼容性产物。

设计原则：
* 原始语义只增不改；注销、撤回、缺失与争议各自保留。
* 跨版本兼容必须同时具备物理证据与治理依据，不能因为“新版本多一个字段”
  就默认可合并。
* 所有集合输出使用确定性顺序，便于哈希与追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

REVIEW_STATES = frozenset({"draft", "approved", "retired"})
APPROVAL_STATES = frozenset({"approved", "pending", "rejected"})
TRANSFORM_NOTES = frozenset({"same", "split", "unit-convert", "unresolved"})
FIELD_TYPES = frozenset({"int", "float", "string"})
REVIEW_JUDGMENTS = frozenset(
    {"same_quantity", "renamed_only", "changed_meaning", "split", "unknown"}
)
REVIEWER_ROLES = frozenset({"experiment_owner"})

# 映射评估结论。
OUTCOME_MERGEABLE = "mergeable"
OUTCOME_REJECTED = "rejected"
OUTCOME_PENDING = "pending_approval"
OUTCOME_UNRESOLVED = "unresolved_claim"
OUTCOME_INVALID_EDGE = "invalid_edge"
OUTCOME_DANGLING = "dangling_reference"
OUTCOME_PHYSICAL_CONFLICT = "physical_definition_conflict"
OUTCOME_SPLIT_INCOMPLETE = "split_incomplete"
OUTCOME_SPLIT_REOBSERVE = "split_requires_reobservation"
OUTCOME_REVIEW_MISSING = "semantic_review_missing"
OUTCOME_REVIEW_PENDING = "semantic_review_pending"
OUTCOME_REVIEW_REJECTED = "semantic_review_rejected"
OUTCOME_REVIEW_INVALID = "semantic_review_invalid"
OUTCOME_SEMANTIC_CHANGE = "semantic_change_declared"
OUTCOME_CLAIM_CONTRADICTED = "claim_contradicted"
OUTCOME_CONVERSION_MISSING = "unit_conversion_missing"
OUTCOME_CONVERSION_PENDING = "unit_conversion_pending"
OUTCOME_CYCLE_INCONSISTENT = "transform_cycle_inconsistent"
OUTCOME_GOVERNANCE_CONFLICT = "governance_conflict"
OUTCOME_REVIEW_CONFLICT = "semantic_review_conflict"

EVIDENCE_VALID = "valid"
EVIDENCE_INSUFFICIENT = "insufficient"
EVIDENCE_CONTRADICTED = "contradicted"
EVIDENCE_NOT_APPLICABLE = "not_applicable"

METRIC_COMPARABLE = "cross_version_comparable"
METRIC_VERSION_ONLY = "version_only"


@dataclass(frozen=True)
class Issue:
    """一条可审计的诊断记录。"""

    code: str
    severity: str  # info | warning | error
    message: str
    subject: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
            "detail": dict(self.detail),
        }

    def sort_key(self) -> tuple[str, str, str, str]:
        return (self.severity, self.code, self.subject, self.message)


@dataclass(frozen=True, order=True)
class Annotation:
    """字段的治理注解。重复且冲突时不能自动取其一。"""

    semantic_ref: str
    required: bool

    def to_dict(self) -> dict[str, object]:
        return {"semantic_ref": self.semantic_ref, "required": self.required}


@dataclass(frozen=True)
class FieldDefinition:
    """某个方案版本中的一个字段定义（已按业务键归并重复行）。"""

    protocol_id: str
    version: str
    field_id: str
    name: str
    dtype: str
    unit: str
    annotations: tuple[Annotation, ...]
    source_lines: tuple[int, ...]
    value_columns: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.protocol_id, self.version, self.field_id)

    @property
    def annotation_status(self) -> str:
        return "resolved" if len(self.annotations) == 1 else "disputed"

    @property
    def semantic_ref(self) -> str | None:
        return self.annotations[0].semantic_ref if len(self.annotations) == 1 else None

    @property
    def required(self) -> bool | None:
        return self.annotations[0].required if len(self.annotations) == 1 else None

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "protocol_id": self.protocol_id,
            "version": self.version,
            "field_id": self.field_id,
            "name": self.name,
            "type": self.dtype,
            "unit": self.unit,
            "annotations": [item.to_dict() for item in self.annotations],
        }


@dataclass(frozen=True)
class ProtocolVersion:
    """一份已登记的方案版本；``parent_version == version`` 表示显式根。"""

    protocol_id: str
    version: str
    lab_id: str
    effective_from: date
    review_state: str
    parent_version: str
    source_lines: tuple[int, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.protocol_id, self.version)

    @property
    def is_root(self) -> bool:
        return self.parent_version == self.version

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "protocol_id": self.protocol_id,
            "version": self.version,
            "lab_id": self.lab_id,
            "effective_from": self.effective_from.isoformat(),
            "review_state": self.review_state,
            "parent_version": self.parent_version,
        }


@dataclass(frozen=True)
class LabFieldView:
    """同一版本字段在某个实验室方案中的视图。"""

    protocol_id: str
    name: str
    dtype: str
    unit: str
    annotations: tuple[Annotation, ...]

    @property
    def annotation_status(self) -> str:
        return "resolved" if len(self.annotations) == 1 else "disputed"

    @property
    def semantic_ref(self) -> str | None:
        return self.annotations[0].semantic_ref if len(self.annotations) == 1 else None

    @property
    def required(self) -> bool | None:
        return self.annotations[0].required if len(self.annotations) == 1 else None


@dataclass(frozen=True)
class VersionField:
    """跨实验室的版本级字段视图。

    名称与单位必须所有实验室一致；类型允许不同但在 ``dtype_status`` 中标注冲突。
    语义引用只有在所有实验室字段注解都唯一且一致时才算 ``resolved``。
    """

    version: str
    field_id: str
    views: tuple[LabFieldView, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.version, self.field_id)

    @property
    def protocols(self) -> tuple[str, ...]:
        return tuple(sorted({view.protocol_id for view in self.views}))

    @property
    def unit(self) -> str | None:
        units = {view.unit for view in self.views}
        return next(iter(units)) if len(units) == 1 else None

    @property
    def name(self) -> str | None:
        names = {view.name for view in self.views}
        return next(iter(names)) if len(names) == 1 else None

    @property
    def physical_resolved(self) -> bool:
        return self.name is not None and self.unit is not None

    @property
    def dtypes(self) -> frozenset[str]:
        return frozenset(view.dtype for view in self.views)

    @property
    def dtype_status(self) -> str:
        return "consistent" if len(self.dtypes) == 1 else "conflict"

    @property
    def annotation_status(self) -> str:
        if not self.views or any(view.annotation_status != "resolved" for view in self.views):
            return "disputed"
        refs = {view.semantic_ref for view in self.views}
        required_flags = {view.required for view in self.views}
        if len(refs) > 1 or len(required_flags) > 1:
            return "conflict"
        return "resolved"

    @property
    def semantic_ref(self) -> str | None:
        if self.annotation_status != "resolved":
            return None
        return self.views[0].semantic_ref

    @property
    def required(self) -> bool | None:
        if self.annotation_status != "resolved":
            return None
        return self.views[0].required

    def view_for(self, protocol_id: str) -> LabFieldView | None:
        for view in self.views:
            if view.protocol_id == protocol_id:
                return view
        return None


@dataclass(frozen=True)
class RunRecord:
    """一条单字段实验结果，保留原始文本并携带质量标记。"""

    run_id: str
    protocol_id: str
    version: str
    field_id: str
    value_text: str
    unit: str
    recorded_at: datetime
    declared_dtype: str
    numeric_value: Decimal | None
    value_flags: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.protocol_id, self.version, self.field_id)

    @property
    def version_field_key(self) -> tuple[str, str]:
        return (self.version, self.field_id)


@dataclass(frozen=True, order=True)
class MappingKey:
    source_version: str
    source_field: str
    target_version: str
    target_field: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_version": self.source_version,
            "source_field": self.source_field,
            "target_version": self.target_version,
            "target_field": self.target_field,
        }

    @property
    def source(self) -> tuple[str, str]:
        return (self.source_version, self.source_field)

    @property
    def target(self) -> tuple[str, str]:
        return (self.target_version, self.target_field)


@dataclass(frozen=True)
class MappingDecision:
    mapping_id: str
    sequence: int
    key: MappingKey
    transform_note: str
    approval_state: str
    source_line: int


@dataclass(frozen=True)
class MappingEdge:
    """同一映射键的全部决策；编号最大者为最新决定。"""

    key: MappingKey
    decisions: tuple[MappingDecision, ...]

    @property
    def latest(self) -> MappingDecision:
        return max(self.decisions, key=lambda item: (item.sequence, item.mapping_id))

    @property
    def history(self) -> tuple[MappingDecision, ...]:
        return tuple(sorted(self.decisions, key=lambda item: (item.sequence, item.mapping_id)))

    @property
    def decision_conflict(self) -> bool:
        return len({item.approval_state for item in self.decisions}) > 1

    @property
    def note_conflict(self) -> bool:
        return len({item.transform_note for item in self.decisions}) > 1


@dataclass(frozen=True)
class MappingAdjudication:
    """数据治理对同一映射键冲突记录的正式裁定。"""

    adjudication_id: str
    sequence: int
    key: MappingKey
    effective_state: str
    effective_note: str
    basis: str
    decided_by: str
    decided_at: str
    source_line: int


@dataclass(frozen=True)
class AdjudicatedMapping:
    key: MappingKey
    adjudications: tuple[MappingAdjudication, ...]

    @property
    def latest(self) -> MappingAdjudication:
        return max(
            self.adjudications,
            key=lambda item: (item.sequence, item.adjudication_id),
        )

    @property
    def history(self) -> tuple[MappingAdjudication, ...]:
        return tuple(
            sorted(self.adjudications, key=lambda item: (item.sequence, item.adjudication_id))
        )

    @property
    def conflict(self) -> bool:
        effective = {
            (item.effective_state, item.effective_note) for item in self.adjudications
        }
        return len(effective) > 1


@dataclass(frozen=True)
class SemanticReviewDecision:
    review_id: str
    sequence: int
    key: MappingKey
    judgment: str
    basis: str
    reviewer_role: str
    decided_by: str
    decided_at: str
    review_state: str
    source_line: int


@dataclass(frozen=True)
class SemanticReview:
    key: MappingKey
    decisions: tuple[SemanticReviewDecision, ...]

    @property
    def latest(self) -> SemanticReviewDecision:
        return max(self.decisions, key=lambda item: (item.sequence, item.review_id))

    @property
    def history(self) -> tuple[SemanticReviewDecision, ...]:
        return tuple(sorted(self.decisions, key=lambda item: (item.sequence, item.review_id)))

    @property
    def decision_conflict(self) -> bool:
        return len({item.judgment for item in self.decisions}) > 1

    @property
    def state_conflict(self) -> bool:
        return len({item.review_state for item in self.decisions}) > 1


@dataclass(frozen=True)
class ConversionDecision:
    conversion_id: str
    sequence: int
    source_unit: str
    target_unit: str
    factor: Decimal
    offset: Decimal
    approval_state: str
    approved_by: str
    basis: str
    source_line: int


@dataclass(frozen=True)
class UnitConversion:
    source_unit: str
    target_unit: str
    decisions: tuple[ConversionDecision, ...]

    @property
    def latest(self) -> ConversionDecision:
        return max(self.decisions, key=lambda item: (item.sequence, item.conversion_id))

    @property
    def history(self) -> tuple[ConversionDecision, ...]:
        return tuple(sorted(self.decisions, key=lambda item: (item.sequence, item.conversion_id)))

    @property
    def decision_conflict(self) -> bool:
        return len({item.approval_state for item in self.decisions}) > 1

    @property
    def factor_conflict(self) -> bool:
        return len({(item.factor, item.offset) for item in self.decisions}) > 1


@dataclass(frozen=True)
class TransformSpec:
    """线性值变换：``target = source * factor + offset``。"""

    kind: str  # identity | linear
    factor: Decimal
    offset: Decimal
    source_unit: str
    target_unit: str
    basis: str

    @classmethod
    def identity(cls, unit: str, basis: str = "同一单位，值保持不变") -> "TransformSpec":
        return cls("identity", Decimal(1), Decimal(0), unit, unit, basis)

    def apply(self, value: Decimal) -> Decimal:
        return value * self.factor + self.offset

    def inverse(self) -> "TransformSpec":
        if self.kind == "identity":
            return TransformSpec.identity(
                self.source_unit, basis=f"{self.basis}（逆向，同值）"
            )
        if self.factor == 0:
            raise ZeroDivisionError("无法反转零因子变换")
        factor = Decimal(1) / self.factor
        offset = -self.offset * factor
        return TransformSpec(
            "linear",
            factor,
            offset,
            self.target_unit,
            self.source_unit,
            f"{self.basis}（逆向）",
        )

    def then(self, after: "TransformSpec") -> "TransformSpec":
        """先执行本变换，再执行 ``after``（两者单位须衔接）。"""

        if self.target_unit != after.source_unit:
            raise ValueError(
                f"变换单位不衔接: {self.target_unit} != {after.source_unit}"
            )
        factor = self.factor * after.factor
        offset = self.offset * after.factor + after.offset
        if factor == 1 and offset == 0 and self.source_unit == after.target_unit:
            kind = "identity"
        else:
            kind = "linear"
        if self.kind == "identity" and after.kind == "identity":
            basis = f"{self.basis}；{after.basis}"
        else:
            basis = f"{self.basis}；再{after.basis}"
        return TransformSpec(kind, factor, offset, self.source_unit, after.target_unit, basis)

    def matches(self, other: "TransformSpec") -> bool:
        return (
            self.factor == other.factor
            and self.offset == other.offset
            and self.source_unit == other.source_unit
            and self.target_unit == other.target_unit
        )

    def describe(self) -> str:
        if self.kind == "identity":
            return "identity"
        return (
            f"linear(factor={self.factor},offset={self.offset},"
            f"from={self.source_unit},to={self.target_unit})"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "factor": str(self.factor),
            "offset": str(self.offset),
            "source_unit": self.source_unit,
            "target_unit": self.target_unit,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class EdgeAssessment:
    """一个映射键的兼容性裁决。"""

    key: MappingKey
    claim: str
    latest_state: str
    evidence_status: str
    outcome: str
    reasons: tuple[str, ...]
    transform: TransformSpec | None
    semantic_basis: str
    review_id: str
    review_state: str
    decision_conflict: bool
    note_conflict: bool
    review_conflict: bool
    history: tuple[MappingDecision, ...]
    adjudication_id: str = ""
    adjudication_basis: str = ""

    @property
    def mergeable(self) -> bool:
        return self.outcome == OUTCOME_MERGEABLE

    def to_dict(self) -> dict[str, object]:
        return {
            "source_version": self.key.source_version,
            "source_field": self.key.source_field,
            "target_version": self.key.target_version,
            "target_field": self.key.target_field,
            "claim": self.claim,
            "latest_mapping_state": self.latest_state,
            "semantic_review_id": self.review_id,
            "semantic_review_state": self.review_state,
            "evidence_status": self.evidence_status,
            "outcome": self.outcome,
            "mergeable": self.mergeable,
            "reasons": list(self.reasons),
            "transform": self.transform.to_dict() if self.transform else None,
            "semantic_basis": self.semantic_basis,
            "mapping_decision_conflict": self.decision_conflict,
            "mapping_note_conflict": self.note_conflict,
            "semantic_review_conflict": self.review_conflict,
            "adjudication_id": self.adjudication_id,
            "adjudication_basis": self.adjudication_basis,
            "decision_history": [
                {
                    "mapping_id": item.mapping_id,
                    "transform_note": item.transform_note,
                    "approval_state": item.approval_state,
                }
                for item in self.history
            ],
        }

    def with_outcome(self, outcome: str, reason: str) -> "EdgeAssessment":
        reasons = tuple(dict.fromkeys((*self.reasons, reason)))
        evidence = EVIDENCE_CONTRADICTED if outcome == OUTCOME_CYCLE_INCONSISTENT else self.evidence_status
        return EdgeAssessment(
            key=self.key,
            claim=self.claim,
            latest_state=self.latest_state,
            evidence_status=evidence,
            outcome=outcome,
            reasons=reasons,
            transform=None,
            semantic_basis=self.semantic_basis,
            review_id=self.review_id,
            review_state=self.review_state,
            decision_conflict=self.decision_conflict,
            note_conflict=self.note_conflict,
            review_conflict=self.review_conflict,
            history=self.history,
            adjudication_id=self.adjudication_id,
            adjudication_basis=self.adjudication_basis,
        )


@dataclass(frozen=True)
class MetricGroup:
    """一个可独立比较的指标组。未合并的版本字段也必须显式成组。"""

    group_id: str
    canonical_version: str
    canonical_field: str
    name: str
    unit: str
    members: tuple[tuple[str, str], ...]
    merged: bool
    buildable: bool
    annotation_status: str
    semantic_ref: str
    semantic_basis: str
    status: str
    reasons: tuple[str, ...]
    transforms: tuple[tuple[str, str, TransformSpec], ...]
    transform_edges: tuple[MappingKey, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "canonical": {
                "version": self.canonical_version,
                "field_id": self.canonical_field,
                "name": self.name,
                "unit": self.unit,
            },
            "members": [
                {"version": version, "field_id": field_id}
                for version, field_id in self.members
            ],
            "merged": self.merged,
            "buildable": self.buildable,
            "annotation_status": self.annotation_status,
            "semantic_ref": self.semantic_ref,
            "semantic_basis": self.semantic_basis,
            "status": self.status,
            "reasons": list(self.reasons),
            "transform_edges": [key.to_dict() for key in self.transform_edges],
        }


@dataclass(frozen=True)
class MetricRow:
    version: str
    field_id: str
    name: str
    unit: str
    dtypes: str
    dtype_status: str
    annotation_status: str
    semantic_ref: str
    group_id: str
    group_merged: bool
    status: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "field_id": self.field_id,
            "name": self.name,
            "unit": self.unit,
            "dtypes": self.dtypes,
            "dtype_status": self.dtype_status,
            "annotation_status": self.annotation_status,
            "semantic_ref": self.semantic_ref,
            "group_id": self.group_id,
            "group_merged": self.group_merged,
            "status": self.status,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Exclusion:
    """被排除出数据集范围的一条结构化记录。"""

    reason: str
    message: str
    run_id: str = ""
    protocol_id: str = ""
    version: str = ""
    field_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "message": self.message,
            "run_id": self.run_id,
            "protocol_id": self.protocol_id,
            "version": self.version,
            "field_id": self.field_id,
        }


@dataclass(frozen=True)
class PartitionRow:
    """兼容数据集分区中的一行。"""

    group_id: str
    run_id: str
    protocol_id: str
    protocol_version: str
    lab_id: str
    field_id: str
    source_name: str
    source_unit: str
    measure_value: str
    measure_unit: str
    original_value: str
    original_unit: str
    transform: str
    semantic_ref: str
    semantic_basis: str
    annotation_status: str
    value_flags: str
    recorded_at: str

    def sort_key(self) -> tuple[str, str, str]:
        return (self.group_id, self.run_id, self.protocol_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "group_id": self.group_id,
            "run_id": self.run_id,
            "protocol_id": self.protocol_id,
            "protocol_version": self.protocol_version,
            "lab_id": self.lab_id,
            "field_id": self.field_id,
            "source_name": self.source_name,
            "source_unit": self.source_unit,
            "measure_value": self.measure_value,
            "measure_unit": self.measure_unit,
            "original_value": self.original_value,
            "original_unit": self.original_unit,
            "transform": self.transform,
            "semantic_ref": self.semantic_ref,
            "semantic_basis": self.semantic_basis,
            "annotation_status": self.annotation_status,
            "value_flags": self.value_flags,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class Partition:
    group: MetricGroup
    rows: tuple[PartitionRow, ...]

    @property
    def row_digest_payload(self) -> list[dict[str, str]]:
        return [row.to_dict() for row in self.rows]

    def to_dict(self) -> dict[str, object]:
        return {
            "group": self.group.to_dict(),
            "row_count": len(self.rows),
        }


@dataclass(frozen=True)
class CompatibilityReport:
    assessments: tuple[EdgeAssessment, ...]
    groups: tuple[MetricGroup, ...]
    metrics: tuple[MetricRow, ...]
    issues: tuple[Issue, ...]
    mergeable_edges: tuple[MappingKey, ...]

    def assessment_for(self, key: MappingKey) -> EdgeAssessment | None:
        for item in self.assessments:
            if item.key == key:
                return item
        return None

    def group_for(self, version: str, field_id: str) -> MetricGroup | None:
        for group in self.groups:
            if (version, field_id) in group.members:
                return group
        return None


@dataclass(frozen=True)
class DatasetBuild:
    """尚未发布的兼容数据集构建结果。"""

    name: str
    created_at: str
    build_mode: str
    requested_versions: tuple[str, ...]
    include_states: tuple[str, ...]
    included_protocols: tuple[str, ...]
    partitions: tuple[Partition, ...]
    groups: tuple[MetricGroup, ...]
    metrics: tuple[MetricRow, ...]
    assessments: tuple[EdgeAssessment, ...]
    exclusions: tuple[Exclusion, ...]
    issues: tuple[Issue, ...]
    source_hashes: Mapping[str, str]
    registry_fingerprint: str
    mapping_fingerprint: str
    review_fingerprint: str
    conversion_fingerprint: str
    adjudication_fingerprint: str
    protocol_states: Mapping[str, str]
    protocol_fingerprints: Mapping[str, str]
    lineage: Mapping[str, object]
    input_versions: tuple[str, ...]
    parameters: Mapping[str, object] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(len(partition.rows) for partition in self.partitions)

    @property
    def affected_partitions(self) -> tuple[dict[str, object], ...]:
        raw = self.lineage.get("affected_partitions", [])
        return tuple(raw) if isinstance(raw, list) else ()

    def partition_by_id(self) -> dict[str, Partition]:
        return {partition.group.group_id: partition for partition in self.partitions}
