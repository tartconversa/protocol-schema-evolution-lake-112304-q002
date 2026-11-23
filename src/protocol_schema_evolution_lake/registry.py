"""协议登记：读取并校验合成资料，构建跨实验室版本视图。

资料中的重复行分三类处理，绝不静默择一：
* 完全相同的重复行：去重并记录 info；
* 同一字段的物理定义（名称/类型/单位）不一致：视为阻断性冲突；
* 同一字段只有注解（semantic_ref/required）不一致：保留全部注解并标为争议。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .errors import (
    ConflictingDuplicateError,
    PhysicalSchemaConflict,
    ReferentialIntegrityError,
    ReviewError,
    SourceFormatError,
)
from .model import (
    APPROVAL_STATES,
    AdjudicatedMapping,
    MappingAdjudication,
    FIELD_TYPES,
    REVIEW_JUDGMENTS,
    REVIEW_STATES,
    REVIEWER_ROLES,
    TRANSFORM_NOTES,
    Annotation,
    ConversionDecision,
    FieldDefinition,
    Issue,
    LabFieldView,
    MappingDecision,
    MappingEdge,
    MappingKey,
    ProtocolVersion,
    RunRecord,
    SemanticReview,
    SemanticReviewDecision,
    UnitConversion,
    VersionField,
)
from .util import (
    parse_iso_date,
    parse_iso_datetime,
    read_contract_csv,
    require_text,
    sha256_bytes,
    sha256_file,
    try_decimal,
    version_key,
)

PROTOCOL_COLUMNS = (
    "protocol_id",
    "version",
    "lab_id",
    "effective_from",
    "review_state",
    "parent_version",
)
SCHEMA_COLUMNS = (
    "protocol_id",
    "version",
    "field_id",
    "name",
    "type",
    "unit",
    "semantic_ref",
    "required",
)
RUN_COLUMNS = (
    "run_id",
    "protocol_id",
    "version",
    "field_id",
    "value",
    "unit",
    "recorded_at",
)
MAPPING_COLUMNS = (
    "mapping_id",
    "source_version",
    "source_field",
    "target_version",
    "target_field",
    "transform_note",
    "approval_state",
)
ADJUDICATION_COLUMNS = (
    "adjudication_id",
    "source_version",
    "source_field",
    "target_version",
    "target_field",
    "effective_state",
    "effective_note",
    "basis",
    "decided_by",
    "decided_at",
)
REVIEW_COLUMNS = (
    "review_id",
    "source_version",
    "source_field",
    "target_version",
    "target_field",
    "judgment",
    "basis",
    "reviewer_role",
    "decided_by",
    "decided_at",
    "review_state",
)
CONVERSION_COLUMNS = (
    "conversion_id",
    "source_unit",
    "target_unit",
    "factor",
    "offset",
    "approval_state",
    "approved_by",
    "basis",
)

REVIEW_DECISION_STATES = frozenset({"approved", "pending", "rejected"})
_REVIEW_ID_RE = re.compile(r"(\d+)$")
_MAPPING_ID_RE = re.compile(r"(\d+)$")


def _sequence_from_id(identifier: str, *, field: str, line_no: int, source: str) -> int:
    match = _REVIEW_ID_RE.search(identifier)
    if not match:
        raise SourceFormatError(f"{source} 第 {line_no} 行 {field} 缺少数字序号: {identifier!r}")
    return int(match.group(1))


@dataclass
class Registry:
    """已装载、已校验的协议登记。"""

    data_dir: Path
    protocols: dict[tuple[str, str], ProtocolVersion]
    fields: dict[tuple[str, str, str], FieldDefinition]
    version_fields: dict[tuple[str, str], VersionField]
    runs: tuple[RunRecord, ...]
    mapping_edges: dict[MappingKey, MappingEdge]
    adjudications: dict[MappingKey, AdjudicatedMapping]
    reviews: dict[MappingKey, SemanticReview]
    conversions: dict[tuple[str, str], UnitConversion]
    issues: tuple[Issue, ...]
    source_hashes: dict[str, str]
    source_paths: dict[str, Path]
    protocol_fingerprints: dict[tuple[str, str], str]
    registry_fingerprint: str
    mapping_fingerprint: str
    review_fingerprint: str
    conversion_fingerprint: str
    adjudication_fingerprint: str

    # ---- 便捷访问 -----------------------------------------------------
    def protocol(self, protocol_id: str, version: str) -> ProtocolVersion | None:
        return self.protocols.get((protocol_id, version))

    def field(self, protocol_id: str, version: str, field_id: str) -> FieldDefinition | None:
        return self.fields.get((protocol_id, version, field_id))

    def version_field(self, version: str, field_id: str) -> VersionField | None:
        return self.version_fields.get((version, field_id))

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(sorted({version for version, _ in self.version_fields}, key=version_key))

    @property
    def labs(self) -> tuple[str, ...]:
        return tuple(sorted({item.lab_id for item in self.protocols.values()}))

    def protocol_key_for_run(self, run: RunRecord) -> tuple[str, str]:
        return (run.protocol_id, run.version)

    # ---- 追溯 ---------------------------------------------------------
    def protocol_lineage(self) -> dict[str, object]:
        """按协议族输出版本链；缺失父版本或成环均显式记录。"""

        families: dict[str, list[ProtocolVersion]] = {}
        for item in self.protocols.values():
            families.setdefault(item.protocol_id, []).append(item)

        chains: list[dict[str, object]] = []
        family_issues: list[dict[str, object]] = []
        for protocol_id in sorted(families):
            versions = sorted(families[protocol_id], key=lambda item: version_key(item.version))
            present = {(item.protocol_id, item.version) for item in versions}
            edges: list[dict[str, object]] = []
            roots: list[str] = []
            for item in versions:
                if item.is_root:
                    roots.append(item.version)
                    edges.append(
                        {
                            "version": item.version,
                            "parent_version": item.version,
                            "parent_present": True,
                            "root": True,
                            "effective_from": item.effective_from.isoformat(),
                        }
                    )
                    continue
                parent_key = (item.protocol_id, item.parent_version)
                parent_present = parent_key in present
                if not parent_present:
                    family_issues.append(
                        {
                            "code": "parent_version_missing",
                            "protocol_id": item.protocol_id,
                            "version": item.version,
                            "parent_version": item.parent_version,
                            "severity": "warning",
                        }
                    )
                edges.append(
                    {
                        "version": item.version,
                        "parent_version": item.parent_version,
                        "parent_present": parent_present,
                        "root": False,
                        "effective_from": item.effective_from.isoformat(),
                    }
                )
            # 沿父指针检测环。
            cycle = self._parent_cycle(protocol_id, families[protocol_id])
            if cycle:
                family_issues.append(
                    {
                        "code": "parent_cycle",
                        "protocol_id": protocol_id,
                        "versions": cycle,
                        "severity": "error",
                    }
                )
            chains.append(
                {
                    "protocol_id": protocol_id,
                    "lab_id": versions[0].lab_id,
                    "versions": [item.version for item in versions],
                    "roots": roots,
                    "edges": edges,
                }
            )
        return {"families": chains, "issues": family_issues}

    @staticmethod
    def _parent_cycle(protocol_id: str, versions: Sequence[ProtocolVersion]) -> list[str]:
        parents = {
            item.version: item.parent_version
            for item in versions
            if not item.is_root
        }
        for start in sorted(parents, key=version_key):
            seen: list[str] = []
            current = start
            while current in parents:
                if current in seen:
                    return seen[seen.index(current) :] + [current]
                seen.append(current)
                current = parents[current]
        return []

    def mapping_lineage(self) -> list[dict[str, object]]:
        """按映射键输出最终裁决依据与决策历史。"""

        result: list[dict[str, object]] = []
        for key in sorted(self.mapping_edges):
            edge = self.mapping_edges[key]
            adjudication = self.adjudications.get(key)
            entry: dict[str, object] = {
                "key": key.to_dict(),
                "latest_mapping_id": edge.latest.mapping_id,
                "latest_state": edge.latest.approval_state,
                "latest_note": edge.latest.transform_note,
                "decision_conflict": edge.decision_conflict,
                "note_conflict": edge.note_conflict,
                "adjudication": None,
                "history": [
                    {
                        "mapping_id": item.mapping_id,
                        "transform_note": item.transform_note,
                        "approval_state": item.approval_state,
                        "source_line": item.source_line,
                    }
                    for item in edge.history
                ],
            }
            if adjudication is not None:
                entry["adjudication"] = {
                    "adjudication_id": adjudication.latest.adjudication_id,
                    "effective_state": adjudication.latest.effective_state,
                    "effective_note": adjudication.latest.effective_note,
                    "basis": adjudication.latest.basis,
                    "decided_by": adjudication.latest.decided_by,
                    "decided_at": adjudication.latest.decided_at,
                    "conflict": adjudication.conflict,
                    "history": [
                        {
                            "adjudication_id": item.adjudication_id,
                            "effective_state": item.effective_state,
                            "effective_note": item.effective_note,
                            "source_line": item.source_line,
                        }
                        for item in adjudication.history
                    ],
                }
            result.append(entry)
        return result

    def input_fingerprint_payload(self) -> dict[str, object]:
        return {
            "protocols": [
                item.fingerprint_payload()
                for item in sorted(self.protocols.values(), key=lambda item: (item.protocol_id, version_key(item.version)))
            ],
            "fields": [
                item.fingerprint_payload()
                for item in sorted(
                    self.fields.values(),
                    key=lambda item: (item.protocol_id, version_key(item.version), item.field_id),
                )
            ],
            "runs": [
                {
                    "run_id": run.run_id,
                    "protocol_id": run.protocol_id,
                    "version": run.version,
                    "field_id": run.field_id,
                    "value": run.value_text,
                    "unit": run.unit,
                    "recorded_at": run.recorded_at.isoformat(),
                    "flags": list(run.value_flags),
                }
                for run in self.runs
            ],
        }


def load_registry(
    data_dir: str | Path,
    *,
    reviews_path: str | Path | None = None,
    conversions_path: str | Path | None = None,
    adjudications_path: str | Path | None = None,
) -> Registry:
    """装载资料目录中的四个契约文件及可选治理文件。"""

    root = Path(data_dir)
    source_hashes: dict[str, str] = {}
    source_paths: dict[str, Path] = {}
    issues: list[Issue] = []

    protocol_rows = _read(root / "protocols.csv", PROTOCOL_COLUMNS)
    source_hashes["protocols.csv"] = sha256_file(root / "protocols.csv")
    source_paths["protocols.csv"] = root / "protocols.csv"

    schema_rows = _read(root / "schema_fields.csv", SCHEMA_COLUMNS)
    source_hashes["schema_fields.csv"] = sha256_file(root / "schema_fields.csv")
    source_paths["schema_fields.csv"] = root / "schema_fields.csv"

    run_rows = _read(root / "run_results.csv", RUN_COLUMNS)
    source_hashes["run_results.csv"] = sha256_file(root / "run_results.csv")
    source_paths["run_results.csv"] = root / "run_results.csv"

    mapping_rows = _read(root / "mappings.csv", MAPPING_COLUMNS)
    source_hashes["mappings.csv"] = sha256_file(root / "mappings.csv")
    source_paths["mappings.csv"] = root / "mappings.csv"

    review_rows: list[tuple[int, dict[str, str]]] = []
    if reviews_path is not None:
        reviews_file = Path(reviews_path)
        review_rows = _read(reviews_file, REVIEW_COLUMNS)
        source_hashes[reviews_file.name] = sha256_file(reviews_file)
        source_paths[reviews_file.name] = reviews_file
    conversion_rows: list[tuple[int, dict[str, str]]] = []
    if conversions_path is not None:
        conversions_file = Path(conversions_path)
        conversion_rows = _read(conversions_file, CONVERSION_COLUMNS)
        source_hashes[conversions_file.name] = sha256_file(conversions_file)
        source_paths[conversions_file.name] = conversions_file
    adjudication_rows: list[tuple[int, dict[str, str]]] = []
    if adjudications_path is not None:
        adjudications_file = Path(adjudications_path)
        adjudication_rows = _read(adjudications_file, ADJUDICATION_COLUMNS)
        source_hashes[adjudications_file.name] = sha256_file(adjudications_file)
        source_paths[adjudications_file.name] = adjudications_file

    protocols = _parse_protocols(protocol_rows, issues)
    fields = _parse_fields(schema_rows, protocols, issues)
    version_fields = _build_version_fields(fields)
    runs = _parse_runs(run_rows, protocols, fields, issues)
    mapping_edges = _parse_mappings(mapping_rows, issues)
    adjudications = _parse_adjudications(adjudication_rows, issues)
    reviews = _parse_reviews(review_rows, issues)
    conversions = _parse_conversions(conversion_rows, issues)

    # 映射引用完整性：仅提示，不删除边；裁决阶段会输出 dangling_reference。
    for key in sorted(mapping_edges):
        if (key.source_version, key.source_field) not in version_fields:
            issues.append(
                Issue(
                    code="mapping_source_missing",
                    severity="warning",
                    message=(
                        f"映射 {key.source_version}/{key.source_field} 未在字段表出现，"
                        "评估将标记为 dangling_reference"
                    ),
                    subject=str(key.to_dict()),
                )
            )
        if (key.target_version, key.target_field) not in version_fields:
            issues.append(
                Issue(
                    code="mapping_target_missing",
                    severity="warning",
                    message=(
                        f"映射 {key.target_version}/{key.target_field} 未在字段表出现，"
                        "评估将标记为 dangling_reference"
                    ),
                    subject=str(key.to_dict()),
                )
            )

    registry_fingerprint = sha256_bytes(
        _canonical_payload(
            {
                "protocols": [
                    item.fingerprint_payload()
                    for item in sorted(
                        protocols.values(),
                        key=lambda item: (item.protocol_id, version_key(item.version)),
                    )
                ],
                "fields": [
                    item.fingerprint_payload()
                    for item in sorted(
                        fields.values(),
                        key=lambda item: (item.protocol_id, version_key(item.version), item.field_id),
                    )
                ],
                "runs": [
                    {
                        "run_id": run.run_id,
                        "protocol_id": run.protocol_id,
                        "version": run.version,
                        "field_id": run.field_id,
                        "value": run.value_text,
                        "unit": run.unit,
                        "recorded_at": run.recorded_at.isoformat(),
                    }
                    for run in runs
                ],
            }
        ).encode("utf-8")
    )
    mapping_fingerprint = sha256_bytes(
        _canonical_payload(
            [
                {
                    "mapping_id": item.mapping_id,
                    "source_version": item.key.source_version,
                    "source_field": item.key.source_field,
                    "target_version": item.key.target_version,
                    "target_field": item.key.target_field,
                    "transform_note": item.transform_note,
                    "approval_state": item.approval_state,
                }
                for key in sorted(mapping_edges)
                for item in mapping_edges[key].history
            ]
        ).encode("utf-8")
    )
    review_fingerprint = sha256_bytes(
        _canonical_payload(
            [
                {
                    "review_id": item.review_id,
                    "key": key.to_dict(),
                    "judgment": item.judgment,
                    "basis": item.basis,
                    "reviewer_role": item.reviewer_role,
                    "decided_by": item.decided_by,
                    "decided_at": item.decided_at,
                    "review_state": item.review_state,
                }
                for key in sorted(reviews)
                for item in reviews[key].history
            ]
        ).encode("utf-8")
    )
    conversion_fingerprint = sha256_bytes(
        _canonical_payload(
            [
                {
                    "conversion_id": item.conversion_id,
                    "source_unit": item.source_unit,
                    "target_unit": item.target_unit,
                    "factor": str(item.factor),
                    "offset": str(item.offset),
                    "approval_state": item.approval_state,
                    "approved_by": item.approved_by,
                    "basis": item.basis,
                }
                for pair in sorted(conversions)
                for item in conversions[pair].history
            ]
        ).encode("utf-8")
    )
    adjudication_fingerprint = sha256_bytes(
        _canonical_payload(
            [
                {
                    "adjudication_id": item.adjudication_id,
                    "key": key.to_dict(),
                    "effective_state": item.effective_state,
                    "effective_note": item.effective_note,
                    "basis": item.basis,
                    "decided_by": item.decided_by,
                    "decided_at": item.decided_at,
                }
                for key in sorted(adjudications)
                for item in adjudications[key].history
            ]
        ).encode("utf-8")
    )
    protocol_fingerprints = {
        key: sha256_bytes(_canonical_payload(protocol.fingerprint_payload()).encode("utf-8"))
        for key, protocol in protocols.items()
    }

    return Registry(
        data_dir=root,
        protocols=protocols,
        fields=fields,
        version_fields=version_fields,
        runs=tuple(runs),
        mapping_edges=mapping_edges,
        adjudications=adjudications,
        reviews=reviews,
        conversions=conversions,
        issues=tuple(sorted(issues, key=lambda item: item.sort_key())),
        source_hashes=source_hashes,
        source_paths=source_paths,
        protocol_fingerprints=protocol_fingerprints,
        registry_fingerprint=registry_fingerprint,
        mapping_fingerprint=mapping_fingerprint,
        review_fingerprint=review_fingerprint,
        conversion_fingerprint=conversion_fingerprint,
        adjudication_fingerprint=adjudication_fingerprint,
    )


def _read(path: Path, columns: Sequence[str]) -> list[tuple[int, dict[str, str]]]:
    return read_contract_csv(path, columns)


def _parse_protocols(
    rows: Iterable[tuple[int, dict[str, str]]], issues: list[Issue]
) -> dict[tuple[str, str], ProtocolVersion]:
    grouped: dict[tuple[str, str], list[tuple[int, dict[str, str]]]] = {}
    for line_no, row in rows:
        protocol_id = require_text(row, "protocol_id", source="protocols.csv", line_no=line_no)
        version = require_text(row, "version", source="protocols.csv", line_no=line_no)
        grouped.setdefault((protocol_id, version), []).append((line_no, row))

    protocols: dict[tuple[str, str], ProtocolVersion] = {}
    for key, duplicates in grouped.items():
        signatures = {tuple(sorted(row.items())) for _, row in duplicates}
        if len(signatures) > 1:
            raise ConflictingDuplicateError(
                f"protocols.csv 中 {key[0]}/{key[1]} 出现内容冲突的重复行: "
                f"{[line for line, _ in duplicates]}"
            )
        if len(duplicates) > 1:
            issues.append(
                Issue(
                    code="duplicate_protocol_row",
                    severity="info",
                    message=f"{key[0]}/{key[1]} 有 {len(duplicates)} 行完全相同记录，已去重",
                    subject=f"{key[0]}/{key[1]}",
                    detail={"lines": [line for line, _ in duplicates]},
                )
            )
        line_no, row = duplicates[0]
        version = key[1]
        review_state = require_text(row, "review_state", source="protocols.csv", line_no=line_no)
        if review_state not in REVIEW_STATES:
            raise SourceFormatError(f"protocols.csv 第 {line_no} 行 review_state 非法: {review_state!r}")
        effective_from = parse_iso_date(
            require_text(row, "effective_from", source="protocols.csv", line_no=line_no),
            f"protocols.csv:{line_no}:effective_from",
        )
        parent_version = require_text(row, "parent_version", source="protocols.csv", line_no=line_no)
        protocols[key] = ProtocolVersion(
            protocol_id=key[0],
            version=version,
            lab_id=require_text(row, "lab_id", source="protocols.csv", line_no=line_no),
            effective_from=effective_from,
            review_state=review_state,
            parent_version=parent_version,
            source_lines=tuple(line for line, _ in duplicates),
        )
    return protocols


def _parse_fields(
    rows: Iterable[tuple[int, dict[str, str]]],
    protocols: Mapping[tuple[str, str], ProtocolVersion],
    issues: list[Issue],
) -> dict[tuple[str, str, str], FieldDefinition]:
    grouped: dict[tuple[str, str, str], list[tuple[int, dict[str, str]]]] = {}
    for line_no, row in rows:
        protocol_id = require_text(row, "protocol_id", source="schema_fields.csv", line_no=line_no)
        version = require_text(row, "version", source="schema_fields.csv", line_no=line_no)
        field_id = require_text(row, "field_id", source="schema_fields.csv", line_no=line_no)
        grouped.setdefault((protocol_id, version, field_id), []).append((line_no, row))

    fields: dict[tuple[str, str, str], FieldDefinition] = {}
    for key, duplicates in grouped.items():
        if key[:2] not in protocols:
            raise ReferentialIntegrityError(
                f"schema_fields.csv 中 {key[0]}/{key[1]}/{key[2]} 引用了未登记的协议版本"
            )
        physical = {
            (
                require_text(row, "name", source="schema_fields.csv", line_no=line_no),
                require_text(row, "type", source="schema_fields.csv", line_no=line_no),
                require_text(row, "unit", source="schema_fields.csv", line_no=line_no),
            )
            for line_no, row in duplicates
        }
        if len(physical) > 1:
            raise PhysicalSchemaConflict(
                f"schema_fields.csv 中 {key[0]}/{key[1]}/{key[2]} 的物理定义冲突: {sorted(physical)}"
            )
        name, dtype, unit = next(iter(physical))
        if dtype not in FIELD_TYPES:
            raise SourceFormatError(
                f"schema_fields.csv 中 {key[0]}/{key[1]}/{key[2]} 的 type 非法: {dtype!r}"
            )
        annotation_set: set[Annotation] = set()
        for line_no, row in duplicates:
            semantic_ref = require_text(row, "semantic_ref", source="schema_fields.csv", line_no=line_no)
            required_text = require_text(row, "required", source="schema_fields.csv", line_no=line_no)
            if required_text not in {"yes", "no"}:
                raise SourceFormatError(
                    f"schema_fields.csv 第 {line_no} 行 required 非法: {required_text!r}"
                )
            annotation_set.add(Annotation(semantic_ref, required_text == "yes"))
        annotations = tuple(sorted(annotation_set))
        if len(annotations) > 1:
            issues.append(
                Issue(
                    code="annotation_conflict",
                    severity="warning",
                    message=(
                        f"{key[0]}/{key[1]}/{key[2]} 的治理注解存在 "
                        f"{len(annotations)} 种说法，不能自动选取；该字段语义状态为争议"
                    ),
                    subject=f"{key[0]}/{key[1]}/{key[2]}",
                    detail={"annotations": [item.to_dict() for item in annotations]},
                )
            )
        elif len({(row["semantic_ref"], row["required"]) for _, row in duplicates}) < len(duplicates):
            issues.append(
                Issue(
                    code="duplicate_schema_row",
                    severity="info",
                    message=f"{key[0]}/{key[1]}/{key[2]} 有完全重复的字段行，已去重",
                    subject=f"{key[0]}/{key[1]}/{key[2]}",
                    detail={"lines": [line for line, _ in duplicates]},
                )
            )
        fields[key] = FieldDefinition(
            protocol_id=key[0],
            version=key[1],
            field_id=key[2],
            name=name,
            dtype=dtype,
            unit=unit,
            annotations=annotations,
            source_lines=tuple(line for line, _ in duplicates),
        )
    return fields


def _build_version_fields(
    fields: Mapping[tuple[str, str, str], FieldDefinition]
) -> dict[tuple[str, str], VersionField]:
    grouped: dict[tuple[str, str], list[LabFieldView]] = {}
    for (protocol_id, version, field_id), definition in fields.items():
        grouped.setdefault((version, field_id), []).append(
            LabFieldView(
                protocol_id=protocol_id,
                name=definition.name,
                dtype=definition.dtype,
                unit=definition.unit,
                annotations=definition.annotations,
            )
        )
    return {
        key: VersionField(
            version=key[0],
            field_id=key[1],
            views=tuple(sorted(views, key=lambda item: item.protocol_id)),
        )
        for key, views in grouped.items()
    }


def _parse_runs(
    rows: Iterable[tuple[int, dict[str, str]]],
    protocols: Mapping[tuple[str, str], ProtocolVersion],
    fields: Mapping[tuple[str, str, str], FieldDefinition],
    issues: list[Issue],
) -> list[RunRecord]:
    runs: list[RunRecord] = []
    seen_ids: dict[str, int] = {}
    for line_no, row in rows:
        run_id = require_text(row, "run_id", source="run_results.csv", line_no=line_no)
        if run_id in seen_ids:
            raise ConflictingDuplicateError(
                f"run_results.csv 中 run_id {run_id!r} 重复（行 {seen_ids[run_id]} 和 {line_no}）"
            )
        seen_ids[run_id] = line_no
        protocol_id = require_text(row, "protocol_id", source="run_results.csv", line_no=line_no)
        version = require_text(row, "version", source="run_results.csv", line_no=line_no)
        field_id = require_text(row, "field_id", source="run_results.csv", line_no=line_no)
        protocol = protocols.get((protocol_id, version))
        if protocol is None:
            raise ReferentialIntegrityError(
                f"run_results.csv 第 {line_no} 行引用未登记协议版本 {protocol_id}/{version}"
            )
        definition = fields.get((protocol_id, version, field_id))
        if definition is None:
            raise ReferentialIntegrityError(
                f"run_results.csv 第 {line_no} 行引用 {protocol_id}/{version} 中不存在的字段 {field_id}"
            )
        unit = require_text(row, "unit", source="run_results.csv", line_no=line_no)
        value_text = row["value"].strip()
        if value_text == "":
            raise SourceFormatError(f"run_results.csv 第 {line_no} 行 value 为空")
        recorded_at = parse_iso_datetime(
            require_text(row, "recorded_at", source="run_results.csv", line_no=line_no),
            f"run_results.csv:{line_no}:recorded_at",
        )
        if recorded_at.date() < protocol.effective_from:
            issues.append(
                Issue(
                    code="run_before_effective",
                    severity="warning",
                    message=(
                        f"{run_id} 记录时间 {recorded_at.date()} 早于方案 "
                        f"{protocol_id}/{version} 生效日期 {protocol.effective_from}"
                    ),
                    subject=run_id,
                )
            )
        flags: list[str] = []
        if unit != definition.unit:
            flags.append("unit_mismatch")
            issues.append(
                Issue(
                    code="run_unit_mismatch",
                    severity="warning",
                    message=(
                        f"{run_id} 单位 {unit!r} 与字段定义 {definition.unit!r} 不一致，"
                        "该行不会进入兼容分区"
                    ),
                    subject=run_id,
                )
            )
        numeric = try_decimal(value_text)
        if definition.dtype in {"int", "float"}:
            if numeric is None:
                flags.append("non_numeric")
                issues.append(
                    Issue(
                        code="run_non_numeric",
                        severity="error",
                        message=f"{run_id} 的数值字段值无法解析为十进制: {value_text!r}",
                        subject=run_id,
                    )
                )
            elif definition.dtype == "int" and numeric != numeric.to_integral_value():
                flags.append("declared_int_nonintegral")
                issues.append(
                    Issue(
                        code="run_int_nonintegral",
                        severity="warning",
                        message=(
                            f"{run_id} 声明为 int 但值 {value_text} 含小数；保留原值，"
                            "分区中以标记形式输出，不四舍五入"
                        ),
                        subject=run_id,
                    )
                )
        else:  # string
            if numeric is not None:
                flags.append("declared_string_numeric")
                issues.append(
                    Issue(
                        code="run_string_numeric",
                        severity="warning",
                        message=(
                            f"{run_id} 声明为 string 但值 {value_text!r} 可解析为十进制；"
                            "分区中作为数值度量并标记来源类型"
                        ),
                        subject=run_id,
                    )
                )
            else:
                flags.append("non_numeric_measure")
                issues.append(
                    Issue(
                        code="run_string_non_numeric",
                        severity="info",
                        message=f"{run_id} 为文本值，不能进入数值兼容分区",
                        subject=run_id,
                    )
                )
        runs.append(
            RunRecord(
                run_id=run_id,
                protocol_id=protocol_id,
                version=version,
                field_id=field_id,
                value_text=value_text,
                unit=unit,
                recorded_at=recorded_at,
                declared_dtype=definition.dtype,
                numeric_value=numeric,
                value_flags=tuple(sorted(flags)),
            )
        )
    return runs


def _parse_mappings(
    rows: Iterable[tuple[int, dict[str, str]]], issues: list[Issue]
) -> dict[MappingKey, MappingEdge]:
    decisions: dict[str, MappingDecision] = {}
    for line_no, row in rows:
        mapping_id = require_text(row, "mapping_id", source="mappings.csv", line_no=line_no)
        note = require_text(row, "transform_note", source="mappings.csv", line_no=line_no)
        state = require_text(row, "approval_state", source="mappings.csv", line_no=line_no)
        if note not in TRANSFORM_NOTES:
            raise SourceFormatError(f"mappings.csv 第 {line_no} 行 transform_note 非法: {note!r}")
        if state not in APPROVAL_STATES:
            raise SourceFormatError(f"mappings.csv 第 {line_no} 行 approval_state 非法: {state!r}")
        sequence = _sequence_from_id(
            mapping_id,
            field="mapping_id",
            line_no=line_no,
            source="mappings.csv",
        )
        key = MappingKey(
            source_version=require_text(row, "source_version", source="mappings.csv", line_no=line_no),
            source_field=require_text(row, "source_field", source="mappings.csv", line_no=line_no),
            target_version=require_text(row, "target_version", source="mappings.csv", line_no=line_no),
            target_field=require_text(row, "target_field", source="mappings.csv", line_no=line_no),
        )
        decision = MappingDecision(
            mapping_id=mapping_id,
            sequence=sequence,
            key=key,
            transform_note=note,
            approval_state=state,
            source_line=line_no,
        )
        previous = decisions.get(mapping_id)
        if previous is not None:
            if previous == decision:
                issues.append(
                    Issue(
                        code="duplicate_mapping_id",
                        severity="info",
                        message=f"mapping_id {mapping_id} 完全重复，已去重",
                        subject=mapping_id,
                    )
                )
                continue
            raise ConflictingDuplicateError(
                f"mappings.csv 中 mapping_id {mapping_id} 出现内容冲突的重复行"
            )
        decisions[mapping_id] = decision

    grouped: dict[MappingKey, list[MappingDecision]] = {}
    for decision in decisions.values():
        grouped.setdefault(decision.key, []).append(decision)
    edges: dict[MappingKey, MappingEdge] = {}
    for key, items in grouped.items():
        edge = MappingEdge(key=key, decisions=tuple(sorted(items, key=lambda item: (item.sequence, item.mapping_id))))
        edges[key] = edge
        if edge.decision_conflict:
            issues.append(
                Issue(
                    code="mapping_decision_conflict",
                    severity="warning",
                    message=(
                        f"映射 {key.source_version}/{key.source_field} → "
                        f"{key.target_version}/{key.target_field} 的审批状态在重复记录间冲突；"
                        "以编号最大的记录为准，并显式报告"
                    ),
                    subject=str(key.to_dict()),
                    detail={
                        "history": [
                            {
                                "mapping_id": item.mapping_id,
                                "approval_state": item.approval_state,
                            }
                            for item in edge.history
                        ]
                    },
                )
            )
        if edge.note_conflict:
            issues.append(
                Issue(
                    code="mapping_note_conflict",
                    severity="warning",
                    message=(
                        f"映射 {key.source_version}/{key.source_field} → "
                        f"{key.target_version}/{key.target_field} 的转换说明在重复记录间冲突；"
                        "以编号最大的记录为准，并显式报告"
                    ),
                    subject=str(key.to_dict()),
                )
            )
    return edges


def _parse_adjudications(
    rows: Iterable[tuple[int, dict[str, str]]], issues: list[Issue]
) -> dict[MappingKey, AdjudicatedMapping]:
    decisions: dict[str, MappingAdjudication] = {}
    for line_no, row in rows:
        adjudication_id = require_text(
            row, "adjudication_id", source="mapping_adjudications.csv", line_no=line_no
        )
        state = require_text(
            row, "effective_state", source="mapping_adjudications.csv", line_no=line_no
        )
        note = require_text(
            row, "effective_note", source="mapping_adjudications.csv", line_no=line_no
        )
        if state not in APPROVAL_STATES:
            raise ReviewError(
                f"mapping_adjudications.csv 第 {line_no} 行 effective_state 非法: {state!r}"
            )
        if note not in TRANSFORM_NOTES:
            raise ReviewError(
                f"mapping_adjudications.csv 第 {line_no} 行 effective_note 非法: {note!r}"
            )
        decided_at = require_text(
            row, "decided_at", source="mapping_adjudications.csv", line_no=line_no
        )
        parse_iso_datetime(decided_at, f"mapping_adjudications.csv:{line_no}:decided_at")
        key = MappingKey(
            source_version=require_text(
                row, "source_version", source="mapping_adjudications.csv", line_no=line_no
            ),
            source_field=require_text(
                row, "source_field", source="mapping_adjudications.csv", line_no=line_no
            ),
            target_version=require_text(
                row, "target_version", source="mapping_adjudications.csv", line_no=line_no
            ),
            target_field=require_text(
                row, "target_field", source="mapping_adjudications.csv", line_no=line_no
            ),
        )
        sequence = _sequence_from_id(
            adjudication_id,
            field="adjudication_id",
            line_no=line_no,
            source="mapping_adjudications.csv",
        )
        decision = MappingAdjudication(
            adjudication_id=adjudication_id,
            sequence=sequence,
            key=key,
            effective_state=state,
            effective_note=note,
            basis=require_text(
                row, "basis", source="mapping_adjudications.csv", line_no=line_no
            ),
            decided_by=require_text(
                row, "decided_by", source="mapping_adjudications.csv", line_no=line_no
            ),
            decided_at=decided_at,
            source_line=line_no,
        )
        previous = decisions.get(adjudication_id)
        if previous is not None:
            if previous == decision:
                continue
            raise ReviewError(
                f"mapping_adjudications.csv 中 adjudication_id {adjudication_id} 内容冲突"
            )
        decisions[adjudication_id] = decision

    grouped: dict[MappingKey, list[MappingAdjudication]] = {}
    for decision in decisions.values():
        grouped.setdefault(decision.key, []).append(decision)
    result: dict[MappingKey, AdjudicatedMapping] = {}
    for key, items in grouped.items():
        adjudicated = AdjudicatedMapping(
            key=key,
            adjudications=tuple(
                sorted(items, key=lambda item: (item.sequence, item.adjudication_id))
            ),
        )
        result[key] = adjudicated
        if adjudicated.conflict:
            issues.append(
                Issue(
                    code="adjudication_conflict",
                    severity="warning",
                    message=(
                        f"映射 {key.source_version}/{key.source_field} → "
                        f"{key.target_version}/{key.target_field} 的裁定记录互相冲突；"
                        "以编号最大的裁定为准，并显式报告"
                    ),
                    subject=str(key.to_dict()),
                )
            )
    return result


def _parse_reviews(
    rows: Iterable[tuple[int, dict[str, str]]], issues: list[Issue]
) -> dict[MappingKey, SemanticReview]:
    decisions: dict[str, SemanticReviewDecision] = {}
    for line_no, row in rows:
        review_id = require_text(row, "review_id", source="semantic_reviews.csv", line_no=line_no)
        judgment = require_text(row, "judgment", source="semantic_reviews.csv", line_no=line_no)
        state = require_text(row, "review_state", source="semantic_reviews.csv", line_no=line_no)
        role = require_text(row, "reviewer_role", source="semantic_reviews.csv", line_no=line_no)
        if judgment not in REVIEW_JUDGMENTS:
            raise ReviewError(f"semantic_reviews.csv 第 {line_no} 行 judgment 非法: {judgment!r}")
        if state not in REVIEW_DECISION_STATES:
            raise ReviewError(f"semantic_reviews.csv 第 {line_no} 行 review_state 非法: {state!r}")
        if role not in REVIEWER_ROLES:
            raise ReviewError(
                f"semantic_reviews.csv 第 {line_no} 行 reviewer_role 非法: {role!r}；"
                "科学含义判断必须由实验负责人记录"
            )
        decided_at = require_text(row, "decided_at", source="semantic_reviews.csv", line_no=line_no)
        parse_iso_datetime(decided_at, f"semantic_reviews.csv:{line_no}:decided_at")
        key = MappingKey(
            source_version=require_text(row, "source_version", source="semantic_reviews.csv", line_no=line_no),
            source_field=require_text(row, "source_field", source="semantic_reviews.csv", line_no=line_no),
            target_version=require_text(row, "target_version", source="semantic_reviews.csv", line_no=line_no),
            target_field=require_text(row, "target_field", source="semantic_reviews.csv", line_no=line_no),
        )
        sequence = _sequence_from_id(
            review_id, field="review_id", line_no=line_no, source="semantic_reviews.csv"
        )
        decision = SemanticReviewDecision(
            review_id=review_id,
            sequence=sequence,
            key=key,
            judgment=judgment,
            basis=require_text(row, "basis", source="semantic_reviews.csv", line_no=line_no),
            reviewer_role=role,
            decided_by=require_text(row, "decided_by", source="semantic_reviews.csv", line_no=line_no),
            decided_at=decided_at,
            review_state=state,
            source_line=line_no,
        )
        previous = decisions.get(review_id)
        if previous is not None:
            if previous == decision:
                continue
            raise ReviewError(f"semantic_reviews.csv 中 review_id {review_id} 内容冲突")
        decisions[review_id] = decision

    grouped: dict[MappingKey, list[SemanticReviewDecision]] = {}
    for decision in decisions.values():
        grouped.setdefault(decision.key, []).append(decision)
    reviews: dict[MappingKey, SemanticReview] = {}
    for key, items in grouped.items():
        review = SemanticReview(
            key=key,
            decisions=tuple(sorted(items, key=lambda item: (item.sequence, item.review_id))),
        )
        reviews[key] = review
        if review.decision_conflict:
            issues.append(
                Issue(
                    code="semantic_review_conflict",
                    severity="warning",
                    message=(
                        f"映射 {key.source_version}/{key.source_field} → "
                        f"{key.target_version}/{key.target_field} 存在不同科学含义判断；"
                        "以编号最大的评审为准，并显式报告"
                    ),
                    subject=str(key.to_dict()),
                )
            )
    return reviews


def _parse_conversions(
    rows: Iterable[tuple[int, dict[str, str]]], issues: list[Issue]
) -> dict[tuple[str, str], UnitConversion]:
    decisions: dict[str, ConversionDecision] = {}
    for line_no, row in rows:
        conversion_id = require_text(row, "conversion_id", source="unit_conversions.csv", line_no=line_no)
        state = require_text(row, "approval_state", source="unit_conversions.csv", line_no=line_no)
        if state not in APPROVAL_STATES:
            raise ReviewError(f"unit_conversions.csv 第 {line_no} 行 approval_state 非法: {state!r}")
        factor_text = require_text(row, "factor", source="unit_conversions.csv", line_no=line_no)
        offset_text = require_text(row, "offset", source="unit_conversions.csv", line_no=line_no)
        factor = try_decimal(factor_text)
        offset = try_decimal(offset_text)
        if factor is None or offset is None:
            raise ReviewError(f"unit_conversions.csv 第 {line_no} 行 factor/offset 不是十进制数")
        if factor == 0:
            raise ReviewError(f"unit_conversions.csv 第 {line_no} 行 factor 不能为 0")
        source_unit = require_text(row, "source_unit", source="unit_conversions.csv", line_no=line_no)
        target_unit = require_text(row, "target_unit", source="unit_conversions.csv", line_no=line_no)
        if source_unit == target_unit:
            raise ReviewError(f"unit_conversions.csv 第 {line_no} 行源/目标单位相同，无需换算")
        sequence = _sequence_from_id(
            conversion_id,
            field="conversion_id",
            line_no=line_no,
            source="unit_conversions.csv",
        )
        decision = ConversionDecision(
            conversion_id=conversion_id,
            sequence=sequence,
            source_unit=source_unit,
            target_unit=target_unit,
            factor=factor,
            offset=offset,
            approval_state=state,
            approved_by=require_text(row, "approved_by", source="unit_conversions.csv", line_no=line_no),
            basis=require_text(row, "basis", source="unit_conversions.csv", line_no=line_no),
            source_line=line_no,
        )
        previous = decisions.get(conversion_id)
        if previous is not None:
            if previous == decision:
                continue
            raise ReviewError(f"unit_conversions.csv 中 conversion_id {conversion_id} 内容冲突")
        decisions[conversion_id] = decision

    grouped: dict[tuple[str, str], list[ConversionDecision]] = {}
    for decision in decisions.values():
        grouped.setdefault((decision.source_unit, decision.target_unit), []).append(decision)
    conversions: dict[tuple[str, str], UnitConversion] = {}
    for pair, items in grouped.items():
        conversion = UnitConversion(
            source_unit=pair[0],
            target_unit=pair[1],
            decisions=tuple(sorted(items, key=lambda item: (item.sequence, item.conversion_id))),
        )
        conversions[pair] = conversion
        if conversion.decision_conflict or conversion.factor_conflict:
            issues.append(
                Issue(
                    code="unit_conversion_conflict",
                    severity="warning",
                    message=(
                        f"单位换算 {pair[0]} → {pair[1]} 在重复记录间状态或系数冲突；"
                        "以编号最大的记录为准，并显式报告"
                    ),
                    subject=f"{pair[0]}->{pair[1]}",
                )
            )
    return conversions


def _canonical_payload(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
