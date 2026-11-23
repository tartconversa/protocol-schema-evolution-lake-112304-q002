"""兼容数据集生成、发布与版本追溯。

发布模型
--------
* 每个数据集版本是不可变目录：一旦发布，内容哈希写进清单，后续校验据此比对。
* 目录外的 ``catalog.json`` 是可变账本，负责记录发布顺序与替代关系。
* 新版本可以显式 ``supersedes`` 旧版本。此时旧版本原样保留，只在账本中标记
  ``superseded_by`` 与其“受影响分区”，绝不静默改写旧结果。
* 内容相同、版本标签相同视为重复发布，直接复用；版本标签相同但内容不同则拒绝。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .compatibility import assess_compatibility, build_groups
from .errors import (
    ArtifactVerificationError,
    ImmutableArtifactError,
    PublicationError,
)
from .model import (
    DatasetBuild,
    Exclusion,
    Issue,
    MetricGroup,
    Partition,
    PartitionRow,
)
from .registry import Registry
from .util import (
    atomic_write_bytes,
    canonical_json_bytes,
    format_decimal,
    sha256_bytes,
    sha256_file,
    utc_now_iso,
    version_key,
    write_csv,
)

PARTITION_COLUMNS = (
    "group_id",
    "run_id",
    "protocol_id",
    "protocol_version",
    "lab_id",
    "field_id",
    "source_name",
    "source_unit",
    "measure_value",
    "measure_unit",
    "original_value",
    "original_unit",
    "transform",
    "semantic_ref",
    "semantic_basis",
    "annotation_status",
    "value_flags",
    "recorded_at",
)
METRIC_COLUMNS = (
    "version",
    "field_id",
    "name",
    "unit",
    "dtypes",
    "dtype_status",
    "annotation_status",
    "semantic_ref",
    "group_id",
    "group_merged",
    "status",
    "reasons",
)
COMPATIBILITY_COLUMNS = (
    "source_version",
    "source_field",
    "target_version",
    "target_field",
    "claim",
    "latest_mapping_state",
    "semantic_review_id",
    "semantic_review_state",
    "evidence_status",
    "outcome",
    "mergeable",
    "transform",
    "semantic_basis",
    "mapping_decision_conflict",
    "mapping_note_conflict",
    "semantic_review_conflict",
    "adjudication_id",
    "reasons",
)
EXCLUSION_COLUMNS = ("reason", "message", "run_id", "protocol_id", "version", "field_id")
ISSUE_COLUMNS = ("code", "severity", "message", "subject", "detail")

DEFAULT_INCLUDE_STATES = ("approved",)
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class BuildParameters:
    """可重建的数据集构建参数。"""

    include_states: tuple[str, ...]
    versions: tuple[str, ...]
    target_version: str | None
    fields: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "include_states": list(self.include_states),
            "versions": list(self.versions),
            "target_version": self.target_version,
            "fields": list(self.fields),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "BuildParameters":
        def _tuple(value: object) -> tuple[str, ...]:
            return tuple(str(item) for item in (value or ()))  # type: ignore[union-attr]

        target = payload.get("target_version")
        return cls(
            include_states=_tuple(payload.get("include_states")),
            versions=_tuple(payload.get("versions")),
            target_version=str(target) if target else None,
            fields=_tuple(payload.get("fields")),
        )


@dataclass(frozen=True)
class PublicationResult:
    name: str
    dataset_version: str
    directory: Path
    manifest: Mapping[str, object]
    created: bool
    supersedes: str | None
    affected_partitions: Mapping[str, object]


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def _parse_field_selector(fields: Iterable[str]) -> set[tuple[str, str]]:
    selected: set[tuple[str, str]] = set()
    for entry in fields:
        for token in entry.replace(";", ",").split(","):
            token = token.strip()
            if not token:
                continue
            if "/" not in token:
                raise PublicationError(f"字段选择器应为 'version/field_id'，实际 {token!r}")
            version, field_id = token.split("/", 1)
            selected.add((version.strip(), field_id.strip()))
    return selected


def resolve_parameters(
    registry: Registry,
    *,
    include_states: Sequence[str] | str | None = None,
    versions: Sequence[str] | str | None = None,
    target_version: str | None = None,
    fields: Sequence[str] | str | None = None,
) -> BuildParameters:
    if include_states is None:
        states: tuple[str, ...] = DEFAULT_INCLUDE_STATES
    elif isinstance(include_states, str):
        states = tuple(item.strip() for item in include_states.split(",") if item.strip())
    else:
        states = tuple(include_states)
    if not states:
        raise PublicationError("include_states 不能为空")
    unknown = sorted(set(states) - {"draft", "approved", "retired"})
    if unknown:
        raise PublicationError(f"未知的方案状态: {unknown}")
    if versions is None:
        resolved_versions = registry.versions
    elif isinstance(versions, str):
        resolved_versions = tuple(item.strip() for item in versions.split(",") if item.strip())
    else:
        resolved_versions = tuple(versions)
    unknown_versions = sorted(set(resolved_versions) - set(registry.versions))
    if unknown_versions:
        raise PublicationError(f"资料中不存在的版本: {unknown_versions}")
    if target_version is not None and target_version not in resolved_versions:
        raise PublicationError(f"目标版本 {target_version} 不在所选版本 {list(resolved_versions)} 中")
    field_list: tuple[str, ...] = ()
    if fields is not None:
        if isinstance(fields, str):
            field_list = (fields,)
        else:
            field_list = tuple(fields)
        selector = _parse_field_selector(field_list)
        for version, field_id in sorted(selector):
            if (version, field_id) not in registry.version_fields:
                raise PublicationError(f"字段选择器 {version}/{field_id} 不存在")
    return BuildParameters(
        include_states=states,
        versions=tuple(sorted(set(resolved_versions), key=version_key)),
        target_version=target_version,
        fields=field_list,
    )


def _validate_path_segment(value: str, label: str) -> None:
    if not _SAFE_NAME_RE.match(value):
        raise PublicationError(
            f"{label} 只能包含字母、数字、点、下划线与连字符，且不能以符号开头: {value!r}"
        )


def build_dataset(
    registry: Registry,
    name: str,
    parameters: BuildParameters,
    *,
    created_at: str | None = None,
) -> DatasetBuild:
    """按参数生成兼容数据集（尚未落盘）。"""

    if not name.strip():
        raise PublicationError("数据集名称不能为空")
    _validate_path_segment(name, "数据集名称")
    include_states = set(parameters.include_states)
    requested_versions = set(parameters.versions)
    field_filter = _parse_field_selector(parameters.fields) if parameters.fields else None

    eligible_protocol_keys = {
        key
        for key, protocol in registry.protocols.items()
        if protocol.review_state in include_states and key[1] in requested_versions
    }
    included_protocols = tuple(
        f"{protocol_id}/{version}"
        for protocol_id, version in sorted(
            eligible_protocol_keys, key=lambda item: (item[0], version_key(item[1]))
        )
    )

    assessments, issue_list = assess_compatibility(registry)
    issues: list[Issue] = list(issue_list)
    scoped_assessments = tuple(
        item
        for item in assessments
        if item.key.source_version in requested_versions
        and item.key.target_version in requested_versions
    )
    groups, metrics, group_issues = build_groups(
        registry, assessments, parameters.versions, parameters.target_version
    )
    issues.extend(group_issues)

    if field_filter is not None:
        allowed = {(version, field) for version, field in field_filter}
        groups = tuple(
            group
            for group in groups
            if (group.canonical_version, group.canonical_field) in allowed
            or any(member in allowed for member in group.members)
        )
        metrics = tuple(
            metric for metric in metrics if (metric.version, metric.field_id) in allowed
        )

    excluded_versions = sorted(requested_versions - {key[1] for key in eligible_protocol_keys}, key=version_key)
    for version in excluded_versions:
        reasons = sorted(
            {
                protocol.review_state
                for (protocol_id, protocol_version), protocol in registry.protocols.items()
                if protocol_version == version
            }
            - include_states
        )
        issues.append(
            Issue(
                code="version_has_no_eligible_protocol",
                severity="info",
                message=(
                    f"版本 {version} 没有任何状态属于 {sorted(include_states)} 的方案，"
                    f"该版本不会产生数据行"
                    + (f"（存在的状态: {reasons}）" if reasons else "")
                ),
                subject=version,
            )
        )

    # ---- 数据行 -------------------------------------------------------
    group_by_member: dict[tuple[str, str], MetricGroup] = {}
    for group in groups:
        for member in group.members:
            group_by_member[member] = group
    transform_by_member: dict[tuple[str, str], object] = {}
    for group in groups:
        for version, field_id, spec in group.transforms:
            transform_by_member[(version, field_id)] = spec
    member_unit: dict[tuple[str, str], str] = {}
    for group in groups:
        for version, field_id, spec in group.transforms:
            member_unit[(version, field_id)] = spec.target_unit or ""

    exclusions: list[Exclusion] = []
    partition_rows: dict[str, list[PartitionRow]] = {group.group_id: [] for group in groups}
    run_count_by_group: dict[str, int] = {group.group_id: 0 for group in groups}

    for run in registry.runs:
        if run.version not in requested_versions:
            continue
        protocol_key = (run.protocol_id, run.version)
        if protocol_key not in eligible_protocol_keys:
            protocol = registry.protocols[protocol_key]
            exclusions.append(
                Exclusion(
                    reason="protocol_state_excluded",
                    message=(
                        f"方案 {run.protocol_id}/{run.version} 状态为 {protocol.review_state}，"
                        f"不在本次纳入状态 {sorted(include_states)} 内"
                    ),
                    run_id=run.run_id,
                    protocol_id=run.protocol_id,
                    version=run.version,
                    field_id=run.field_id,
                )
            )
            continue
        member = (run.version, run.field_id)
        group = group_by_member.get(member)
        if group is None:
            exclusions.append(
                Exclusion(
                    reason="field_not_selected",
                    message="该记录所属字段未包含在本次数据集范围内",
                    run_id=run.run_id,
                    protocol_id=run.protocol_id,
                    version=run.version,
                    field_id=run.field_id,
                )
            )
            continue
        if not group.buildable:
            exclusions.append(
                Exclusion(
                    reason="group_not_buildable",
                    message="该指标组缺少完整依据（物理定义或换算路径），整体不进入数据集",
                    run_id=run.run_id,
                    protocol_id=run.protocol_id,
                    version=run.version,
                    field_id=run.field_id,
                )
            )
            continue
        if "unit_mismatch" in run.value_flags:
            exclusions.append(
                Exclusion(
                    reason="run_unit_mismatch",
                    message=f"记录单位 {run.unit!r} 与字段定义不一致，需人工核对后再纳入",
                    run_id=run.run_id,
                    protocol_id=run.protocol_id,
                    version=run.version,
                    field_id=run.field_id,
                )
            )
            continue
        if run.numeric_value is None:
            exclusions.append(
                Exclusion(
                    reason="non_numeric_value",
                    message=f"值 {run.value_text!r} 无法作为数值度量，保留在原始资料中但未进入兼容分区",
                    run_id=run.run_id,
                    protocol_id=run.protocol_id,
                    version=run.version,
                    field_id=run.field_id,
                )
            )
            continue
        spec = transform_by_member.get(member)
        if spec is None:  # pragma: no cover - 分组保证存在
            exclusions.append(
                Exclusion(
                    reason="missing_transform",
                    message="缺少从该字段到基准字段的变换依据",
                    run_id=run.run_id,
                    protocol_id=run.protocol_id,
                    version=run.version,
                    field_id=run.field_id,
                )
            )
            continue
        definition = registry.fields[(run.protocol_id, run.version, run.field_id)]
        measured = spec.apply(run.numeric_value)
        measure_unit = member_unit.get(member) or definition.unit
        lab_semantic = (
            definition.annotations[0].semantic_ref
            if definition.annotation_status == "resolved"
            else ""
        )
        row = PartitionRow(
            group_id=group.group_id,
            run_id=run.run_id,
            protocol_id=run.protocol_id,
            protocol_version=run.version,
            lab_id=protocol_lab(registry, run.protocol_id, run.version),
            field_id=run.field_id,
            source_name=definition.name,
            source_unit=definition.unit,
            measure_value=format_decimal(measured),
            measure_unit=measure_unit,
            original_value=run.value_text,
            original_unit=run.unit,
            transform=spec.describe(),
            semantic_ref=lab_semantic or group.semantic_ref,
            semantic_basis=group.semantic_basis if group.merged else "",
            annotation_status=definition.annotation_status,
            value_flags="|".join(run.value_flags),
            recorded_at=run.recorded_at.isoformat().replace("+00:00", "Z"),
        )
        partition_rows[group.group_id].append(row)
        run_count_by_group[group.group_id] += 1

    partitions = tuple(
        Partition(
            group=group,
            rows=tuple(sorted(partition_rows[group.group_id], key=lambda row: row.sort_key())),
        )
        for group in groups
        if partition_rows.get(group.group_id)
    )

    # 未包含状态的方案也会出现在排除清单（按协议粒度给一条摘要）。
    for protocol_key in sorted(registry.protocols, key=lambda item: (item[0], version_key(item[1]))):
        protocol = registry.protocols[protocol_key]
        if protocol_key[1] not in requested_versions:
            continue
        if protocol.review_state in include_states:
            continue
        if not any(
            exclusion.reason == "protocol_state_excluded"
            and exclusion.protocol_id == protocol.protocol_id
            and exclusion.version == protocol.version
            for exclusion in exclusions
        ):
            exclusions.append(
                Exclusion(
                    reason="protocol_state_excluded",
                    message=(
                        f"方案 {protocol.protocol_id}/{protocol.version} 状态为 "
                        f"{protocol.review_state}，本次未纳入"
                    ),
                    protocol_id=protocol.protocol_id,
                    version=protocol.version,
                )
            )

    exclusions.sort(
        key=lambda item: (
            item.reason,
            item.protocol_id,
            item.version,
            item.field_id,
            item.run_id,
            item.message,
        )
    )

    protocol_states = {
        f"{protocol_id}/{version}": registry.protocols[(protocol_id, version)].review_state
        for protocol_id, version in sorted(
            eligible_protocol_keys, key=lambda item: (item[0], version_key(item[1]))
        )
    }
    protocol_fingerprints = {
        f"{protocol_id}/{version}": registry.protocol_fingerprints[(protocol_id, version)]
        for protocol_id, version in sorted(
            eligible_protocol_keys, key=lambda item: (item[0], version_key(item[1]))
        )
    }

    lineage = {
        "protocol_lineage": registry.protocol_lineage(),
        "mapping_lineage": registry.mapping_lineage(),
        "governance_files": sorted(
            name
            for name in registry.source_hashes
            if name not in {"protocols.csv", "schema_fields.csv", "run_results.csv", "mappings.csv"}
        ),
        "transformations": [
            {
                "group_id": group.group_id,
                "merged": group.merged,
                "status": group.status,
                "members": [
                    {
                        "version": version,
                        "field_id": field_id,
                        "transform": spec.describe(),
                        "basis": spec.basis,
                        "source_unit": spec.source_unit,
                        "target_unit": spec.target_unit,
                    }
                    for version, field_id, spec in group.transforms
                ],
                "transform_edges": [key.to_dict() for key in group.transform_edges],
            }
            for group in groups
        ],
    }

    warnings = tuple(sorted(issues, key=lambda item: item.sort_key()))
    return DatasetBuild(
        name=name.strip(),
        created_at=created_at or utc_now_iso(),
        build_mode="strict",
        requested_versions=parameters.versions,
        include_states=parameters.include_states,
        included_protocols=included_protocols,
        partitions=partitions,
        groups=groups,
        metrics=metrics,
        assessments=scoped_assessments,
        exclusions=tuple(exclusions),
        issues=warnings,
        source_hashes=dict(sorted(registry.source_hashes.items())),
        registry_fingerprint=registry.registry_fingerprint,
        mapping_fingerprint=registry.mapping_fingerprint,
        review_fingerprint=registry.review_fingerprint,
        conversion_fingerprint=registry.conversion_fingerprint,
        adjudication_fingerprint=registry.adjudication_fingerprint,
        protocol_states=protocol_states,
        protocol_fingerprints=protocol_fingerprints,
        lineage=lineage,
        input_versions=parameters.versions,
        parameters=parameters,
    )


def protocol_lab(registry: Registry, protocol_id: str, version: str) -> str:
    return registry.protocols[(protocol_id, version)].lab_id


# ---------------------------------------------------------------------------
# 落盘与发布
# ---------------------------------------------------------------------------


def partition_digest(partition: Partition) -> str:
    payload = {
        "group": partition.group.to_dict(),
        "rows": partition.row_digest_payload,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def compare_partitions(
    previous: Mapping[str, Mapping[str, object]],
    current: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """比较两个清单中的分区摘要，返回受影响分区。"""

    previous_ids = set(previous)
    current_ids = set(current)
    added = sorted(current_ids - previous_ids)
    removed = sorted(previous_ids - current_ids)
    changed = sorted(
        group_id
        for group_id in previous_ids & current_ids
        if previous[group_id].get("digest") != current[group_id].get("digest")
    )
    unchanged = sorted(
        group_id
        for group_id in previous_ids & current_ids
        if previous[group_id].get("digest") == current[group_id].get("digest")
    )
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "affected": sorted(set(added) | set(removed) | set(changed)),
    }


def _partition_summary(build: DatasetBuild) -> dict[str, dict[str, object]]:
    return {
        partition.group.group_id: {
            "digest": partition_digest(partition),
            "row_count": len(partition.rows),
            "status": partition.group.status,
            "canonical": {
                "version": partition.group.canonical_version,
                "field_id": partition.group.canonical_field,
            },
        }
        for partition in build.partitions
    }


def _group_summary(build: DatasetBuild) -> list[dict[str, object]]:
    return [group.to_dict() for group in build.groups]


def _metric_rows(build: DatasetBuild) -> list[dict[str, object]]:
    rows = []
    for metric in build.metrics:
        rows.append(
            {
                **metric.to_dict(),
                "reasons": " | ".join(metric.reasons),
                "group_merged": "yes" if metric.group_merged else "no",
            }
        )
    return rows


def _compatibility_rows(build: DatasetBuild) -> list[dict[str, object]]:
    rows = []
    for assessment in build.assessments:
        payload = assessment.to_dict()
        rows.append(
            {
                "source_version": payload["source_version"],
                "source_field": payload["source_field"],
                "target_version": payload["target_version"],
                "target_field": payload["target_field"],
                "claim": payload["claim"],
                "latest_mapping_state": payload["latest_mapping_state"],
                "semantic_review_id": payload["semantic_review_id"],
                "semantic_review_state": payload["semantic_review_state"],
                "evidence_status": payload["evidence_status"],
                "outcome": payload["outcome"],
                "mergeable": "yes" if payload["mergeable"] else "no",
                "transform": (
                    payload["transform"]["basis"] if payload["transform"] else ""
                ),
                "semantic_basis": payload["semantic_basis"],
                "mapping_decision_conflict": "yes" if payload["mapping_decision_conflict"] else "no",
                "mapping_note_conflict": "yes" if payload["mapping_note_conflict"] else "no",
                "semantic_review_conflict": "yes" if payload["semantic_review_conflict"] else "no",
                "adjudication_id": payload["adjudication_id"],
                "reasons": " | ".join(payload["reasons"]),
            }
        )
    return rows


def _exclusion_rows(build: DatasetBuild) -> list[dict[str, object]]:
    return [exclusion.to_dict() for exclusion in build.exclusions]


def _issue_rows(build: DatasetBuild) -> list[dict[str, object]]:
    rows = []
    for issue in build.issues:
        payload = issue.to_dict()
        rows.append(
            {
                "code": payload["code"],
                "severity": payload["severity"],
                "message": payload["message"],
                "subject": payload["subject"],
                "detail": json.dumps(payload["detail"], ensure_ascii=False, sort_keys=True),
            }
        )
    return rows


def _partition_rows(partition: Partition) -> list[dict[str, object]]:
    return [row.to_dict() for row in partition.rows]


def content_digest(build: DatasetBuild) -> str:
    payload = {
        "name": build.name,
        "parameters": build.parameters.to_dict(),
        "included_protocols": list(build.included_protocols),
        "partitions": [
            {"digest": partition_digest(partition), "group": partition.group.to_dict()}
            for partition in build.partitions
        ],
        "metrics": [metric.to_dict() for metric in build.metrics],
        "compatibility": [assessment.to_dict() for assessment in build.assessments],
        "exclusions": [exclusion.to_dict() for exclusion in build.exclusions],
        "source_hashes": dict(build.source_hashes),
        "registry_fingerprint": build.registry_fingerprint,
        "mapping_fingerprint": build.mapping_fingerprint,
        "review_fingerprint": build.review_fingerprint,
        "conversion_fingerprint": build.conversion_fingerprint,
        "adjudication_fingerprint": build.adjudication_fingerprint,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def write_build(directory: Path, build: DatasetBuild, dataset_version: str) -> dict[str, object]:
    """把构建结果写成不可变目录，返回清单（不含目录外信息）。"""

    if directory.exists():
        raise ImmutableArtifactError(f"目标目录已存在，拒绝覆盖: {directory}")
    directory.mkdir(parents=True)
    partitions_dir = directory / "partitions"
    partitions_dir.mkdir()

    write_csv(directory / "metrics.csv", METRIC_COLUMNS, _metric_rows(build))
    write_csv(directory / "compatibility.csv", COMPATIBILITY_COLUMNS, _compatibility_rows(build))
    write_csv(directory / "exclusions.csv", EXCLUSION_COLUMNS, _exclusion_rows(build))
    write_csv(directory / "issues.csv", ISSUE_COLUMNS, _issue_rows(build))

    partition_summaries: dict[str, dict[str, object]] = {}
    for partition in build.partitions:
        group_id = partition.group.group_id
        relative = f"partitions/{group_id}.csv"
        write_csv(directory / relative, PARTITION_COLUMNS, _partition_rows(partition))
        partition_summaries[group_id] = {
            "digest": partition_digest(partition),
            "row_count": len(partition.rows),
            "status": partition.group.status,
            "canonical": {
                "version": partition.group.canonical_version,
                "field_id": partition.group.canonical_field,
            },
            "file": relative,
        }

    lineage = {
        **build.lineage,
        "dataset_name": build.name,
        "dataset_version": dataset_version,
        "created_at": build.created_at,
        "input_versions": list(build.input_versions),
        "included_protocols": list(build.included_protocols),
        "protocol_states": dict(build.protocol_states),
        "source_hashes": dict(build.source_hashes),
        "fingerprints": {
            "registry": build.registry_fingerprint,
            "mapping": build.mapping_fingerprint,
            "semantic_reviews": build.review_fingerprint,
            "unit_conversions": build.conversion_fingerprint,
            "mapping_adjudications": build.adjudication_fingerprint,
        },
        "protocol_fingerprints": dict(build.protocol_fingerprints),
        "partition_summaries": partition_summaries,
    }
    atomic_write_bytes(directory / "lineage.json", canonical_json_bytes(lineage, indent=2))
    return {
        "name": build.name,
        "created_at": build.created_at,
        "parameters": build.parameters.to_dict(),
        "included_protocols": list(build.included_protocols),
        "input_versions": list(build.input_versions),
        "partition_summaries": partition_summaries,
        "content_digest": content_digest(build),
    }


# ---------------------------------------------------------------------------
# 账本与追溯
# ---------------------------------------------------------------------------


def _catalog_path(lake_dir: Path) -> Path:
    return lake_dir / "catalog.json"


def load_catalog(lake_dir: str | Path) -> dict[str, object]:
    path = _catalog_path(Path(lake_dir))
    if not path.is_file():
        return {"format_version": 1, "datasets": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PublicationError(f"catalog.json 不是合法 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise PublicationError("catalog.json 顶层必须是对象")
    return payload


def _save_catalog(lake_dir: Path, catalog: Mapping[str, object]) -> None:
    atomic_write_bytes(
        _catalog_path(lake_dir),
        canonical_json_bytes(catalog, indent=2),
    )


def dataset_directory(lake_dir: str | Path, name: str, dataset_version: str) -> Path:
    return Path(lake_dir) / "datasets" / name / dataset_version


def next_dataset_version(lake_dir: str | Path, name: str) -> str:
    catalog = load_catalog(lake_dir)
    entries = _dataset_entries(catalog, name)
    used = {entry.get("dataset_version") for entry in entries}
    index = 1
    while f"v{index}" in used:
        index += 1
    return f"v{index}"


def _dataset_entries(catalog: Mapping[str, object], name: str) -> list[dict[str, object]]:
    datasets = catalog.get("datasets")
    if not isinstance(datasets, dict):
        return []
    entry = datasets.get(name)
    if not isinstance(entry, dict):
        return []
    versions = entry.get("versions")
    if not isinstance(versions, list):
        return []
    return [item for item in versions if isinstance(item, dict)]


def _write_snapshots(
    directory: Path, registry: Registry, build: DatasetBuild
) -> list[str]:
    registry_snapshot = {
        "protocols": [
            {
                "protocol_id": protocol.protocol_id,
                "version": protocol.version,
                "lab_id": protocol.lab_id,
                "effective_from": protocol.effective_from.isoformat(),
                "review_state": protocol.review_state,
                "parent_version": protocol.parent_version,
                "source_lines": list(protocol.source_lines),
                "fingerprint": registry.protocol_fingerprints[protocol.key],
            }
            for protocol in sorted(
                registry.protocols.values(),
                key=lambda item: (item.protocol_id, version_key(item.version)),
            )
        ],
        "fields": [
            {
                "protocol_id": definition.protocol_id,
                "version": definition.version,
                "field_id": definition.field_id,
                "name": definition.name,
                "type": definition.dtype,
                "unit": definition.unit,
                "annotations": [annotation.to_dict() for annotation in definition.annotations],
                "annotation_status": definition.annotation_status,
                "source_lines": list(definition.source_lines),
                "fingerprint": sha256_bytes(
                    canonical_json_bytes(definition.fingerprint_payload())
                ),
            }
            for definition in sorted(
                registry.fields.values(),
                key=lambda item: (item.protocol_id, version_key(item.version), item.field_id),
            )
        ],
        "versions": list(registry.versions),
    }
    governance_snapshot = {
        "mappings": [
            {
                "key": key.to_dict(),
                "latest": {
                    "mapping_id": edge.latest.mapping_id,
                    "transform_note": edge.latest.transform_note,
                    "approval_state": edge.latest.approval_state,
                },
                "decision_conflict": edge.decision_conflict,
                "note_conflict": edge.note_conflict,
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
            for key, edge in sorted(registry.mapping_edges.items())
        ],
        "mapping_adjudications": [
            {
                "key": key.to_dict(),
                "latest": {
                    "adjudication_id": adjudication.latest.adjudication_id,
                    "effective_state": adjudication.latest.effective_state,
                    "effective_note": adjudication.latest.effective_note,
                    "basis": adjudication.latest.basis,
                    "decided_by": adjudication.latest.decided_by,
                    "decided_at": adjudication.latest.decided_at,
                },
                "conflict": adjudication.conflict,
                "history": [
                    {
                        "adjudication_id": item.adjudication_id,
                        "effective_state": item.effective_state,
                        "effective_note": item.effective_note,
                        "basis": item.basis,
                        "decided_by": item.decided_by,
                        "decided_at": item.decided_at,
                    }
                    for item in adjudication.history
                ],
            }
            for key, adjudication in sorted(registry.adjudications.items())
        ],
        "semantic_reviews": [
            {
                "key": key.to_dict(),
                "latest": {
                    "review_id": review.latest.review_id,
                    "judgment": review.latest.judgment,
                    "review_state": review.latest.review_state,
                    "basis": review.latest.basis,
                    "decided_by": review.latest.decided_by,
                    "decided_at": review.latest.decided_at,
                },
                "history": [
                    {
                        "review_id": item.review_id,
                        "judgment": item.judgment,
                        "review_state": item.review_state,
                        "basis": item.basis,
                        "reviewer_role": item.reviewer_role,
                        "decided_by": item.decided_by,
                        "decided_at": item.decided_at,
                    }
                    for item in review.history
                ],
            }
            for key, review in sorted(registry.reviews.items())
        ],
        "unit_conversions": [
            {
                "source_unit": conversion.source_unit,
                "target_unit": conversion.target_unit,
                "latest": {
                    "conversion_id": conversion.latest.conversion_id,
                    "factor": str(conversion.latest.factor),
                    "offset": str(conversion.latest.offset),
                    "approval_state": conversion.latest.approval_state,
                    "approved_by": conversion.latest.approved_by,
                    "basis": conversion.latest.basis,
                },
                "history": [
                    {
                        "conversion_id": item.conversion_id,
                        "factor": str(item.factor),
                        "offset": str(item.offset),
                        "approval_state": item.approval_state,
                        "approved_by": item.approved_by,
                        "basis": item.basis,
                    }
                    for item in conversion.history
                ],
            }
            for _, conversion in sorted(registry.conversions.items())
        ],
    }
    atomic_write_bytes(
        directory / "registry_snapshot.json", canonical_json_bytes(registry_snapshot, indent=2)
    )
    atomic_write_bytes(
        directory / "governance_snapshot.json",
        canonical_json_bytes(governance_snapshot, indent=2),
    )
    return ["registry_snapshot.json", "governance_snapshot.json"]


def publish(
    build: DatasetBuild,
    registry: Registry,
    lake_dir: str | Path,
    dataset_version: str,
    *,
    supersedes: str | None = None,
) -> PublicationResult:
    """发布构建结果；旧版本原样保留，新版本记录替代关系。

    目录先在临时暂存区生成并完整写入，再整体原子移入正式位置，最后更新账本。
    任一步失败都不会留下半成品目录。
    """

    if not dataset_version.strip():
        raise PublicationError("数据集版本不能为空")
    _validate_path_segment(dataset_version, "数据集版本")
    lake = Path(lake_dir)
    lake.mkdir(parents=True, exist_ok=True)
    target = dataset_directory(lake, build.name, dataset_version)
    digest = content_digest(build)

    if target.exists():
        manifest = read_manifest(target)
        existing_digest = manifest.get("content_digest")
        if existing_digest != digest:
            raise ImmutableArtifactError(
                f"数据集 {build.name}/{dataset_version} 已发布且内容不同；"
                "请使用新的数据集版本，不能覆盖已引用的结果"
            )
        _ensure_catalog_entry(lake, build.name, dataset_version, manifest)
        return PublicationResult(
            name=build.name,
            dataset_version=dataset_version,
            directory=target,
            manifest=manifest,
            created=False,
            supersedes=(
                str(manifest.get("supersedes")) if manifest.get("supersedes") else None
            ),
            affected_partitions=dict(manifest.get("affected_partitions") or {}),
        )

    catalog = load_catalog(lake)
    entries = _dataset_entries(catalog, build.name)
    known_versions = {str(entry.get("dataset_version")) for entry in entries}
    if supersedes is not None and supersedes not in known_versions:
        raise PublicationError(f"要替代的数据集版本不存在: {build.name}/{supersedes}")
    if supersedes == dataset_version:
        raise PublicationError("数据集不能替代自身")

    previous_manifest: Mapping[str, object] | None = None
    affected: dict[str, object] = {}
    if supersedes is not None:
        previous_dir = dataset_directory(lake, build.name, supersedes)
        previous_manifest = read_manifest(previous_dir)
        affected = compare_partitions(
            previous_manifest.get("partition_summaries") or {},  # type: ignore[arg-type]
            _partition_summary(build),
        )

    _write_staged_and_promote(lake, target, build, registry, dataset_version, supersedes, affected)

    manifest = read_manifest(target)
    catalog_entry = {
        "dataset_version": dataset_version,
        "status": "published",
        "published_at": manifest["published_at"],
        "created_at": manifest["created_at"],
        "content_digest": digest,
        "manifest_sha256": sha256_file(target / "manifest.json"),
        "input_versions": list(build.input_versions),
        "included_protocols": list(build.included_protocols),
        "parameters": build.parameters.to_dict(),
        "total_rows": build.total_rows,
        "comparable_groups": manifest["comparable_groups"],
        "version_only_groups": manifest["version_only_groups"],
        "supersedes": supersedes,
        "superseded_by": None,
        "affected_partitions": list(affected.get("affected", [])) if affected else [],
    }
    datasets = catalog.setdefault("datasets", {})
    assert isinstance(datasets, dict)
    dataset_entry = datasets.setdefault(build.name, {"name": build.name, "versions": []})
    assert isinstance(dataset_entry, dict)
    versions = dataset_entry.setdefault("versions", [])
    assert isinstance(versions, list)
    for entry in versions:
        assert isinstance(entry, dict)
        if entry.get("dataset_version") == dataset_version:
            raise PublicationError(
                f"catalog 中已存在 {build.name}/{dataset_version}，但目录缺失；请人工核查"
            )
        if entry.get("dataset_version") == supersedes:
            entry["superseded_by"] = dataset_version
            entry["status"] = "superseded"
            entry["affected_partitions"] = sorted(
                set(entry.get("affected_partitions") or []) | set(affected.get("affected", []))
            )
            entry["affected_by_digest"] = digest
    versions.append(catalog_entry)
    dataset_entry["latest"] = dataset_version
    dataset_entry["versions"] = versions
    catalog["format_version"] = catalog.get("format_version", 1)
    _save_catalog(lake, catalog)

    return PublicationResult(
        name=build.name,
        dataset_version=dataset_version,
        directory=target,
        manifest=manifest,
        created=True,
        supersedes=supersedes,
        affected_partitions=affected,
    )


def _write_staged_and_promote(
    lake: Path,
    target: Path,
    build: DatasetBuild,
    registry: Registry,
    dataset_version: str,
    supersedes: str | None,
    affected: Mapping[str, object],
) -> None:
    """在暂存目录完整生成，再原子改名，避免留下半成品。"""

    staging_root = lake / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / f"{build.name}-{dataset_version}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        write_build(staging, build, dataset_version)
        _write_snapshots(staging, registry, build)
        lineage = json.loads((staging / "lineage.json").read_text(encoding="utf-8"))
        lineage["supersedes"] = supersedes
        lineage["affected_partitions"] = dict(affected)
        atomic_write_bytes(staging / "lineage.json", canonical_json_bytes(lineage, indent=2))

        files = {}
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                relative = path.relative_to(staging).as_posix()
                files[relative] = {
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
        previous_digest = None
        if supersedes is not None:
            previous_manifest = read_manifest(dataset_directory(lake, build.name, supersedes))
            previous_digest = str(previous_manifest.get("content_digest") or "") or None
        manifest = _build_manifest(
            build,
            dataset_version,
            supersedes,
            affected,
            files,
            previous_digest,
        )
        atomic_write_bytes(staging / "manifest.json", canonical_json_bytes(manifest, indent=2))

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ImmutableArtifactError(f"目标目录在发布过程中被创建: {target}")
        staging.rename(target)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        try:
            staging_root.rmdir()
        except OSError:
            pass


def _build_manifest(
    build: DatasetBuild,
    dataset_version: str,
    supersedes: str | None,
    affected: Mapping[str, object],
    files: Mapping[str, Mapping[str, object]],
    previous_digest: str | None,
) -> dict[str, object]:
    return {
        "format_version": 1,
        "name": build.name,
        "dataset_version": dataset_version,
        "created_at": build.created_at,
        "published_at": utc_now_iso(),
        "parameters": build.parameters.to_dict(),
        "input_versions": list(build.input_versions),
        "included_protocols": list(build.included_protocols),
        "protocol_states": dict(build.protocol_states),
        "source_hashes": dict(build.source_hashes),
        "fingerprints": {
            "registry": build.registry_fingerprint,
            "mapping": build.mapping_fingerprint,
            "semantic_reviews": build.review_fingerprint,
            "unit_conversions": build.conversion_fingerprint,
            "mapping_adjudications": build.adjudication_fingerprint,
        },
        "content_digest": content_digest(build),
        "total_rows": build.total_rows,
        "partition_summaries": _partition_summary(build),
        "groups": _group_summary(build),
        "comparable_groups": sorted(
            group.group_id for group in build.groups if group.status == "cross_version_comparable"
        ),
        "version_only_groups": sorted(
            group.group_id for group in build.groups if group.status != "cross_version_comparable"
        ),
        "exclusion_counts": _count_exclusions(build),
        "issue_counts": _count_issues(build),
        "files": dict(files),
        "supersedes": supersedes,
        "affected_partitions": dict(affected),
        "previous_content_digest": previous_digest,
    }


def _ensure_catalog_entry(
    lake: Path, name: str, dataset_version: str, manifest: Mapping[str, object]
) -> None:
    """若目录已存在但账本缺失（上次发布在改名后、记账前中断），补齐账本。"""

    catalog = load_catalog(lake)
    entries = _dataset_entries(catalog, name)
    if any(entry.get("dataset_version") == dataset_version for entry in entries):
        return
    datasets = catalog.setdefault("datasets", {})
    assert isinstance(datasets, dict)
    dataset_entry = datasets.setdefault(name, {"name": name, "versions": []})
    assert isinstance(dataset_entry, dict)
    versions = dataset_entry.setdefault("versions", [])
    assert isinstance(versions, list)
    supersedes = manifest.get("supersedes")
    if supersedes:
        # 补齐“替代关系”这一半：若旧版本账本条目仍存在，标记其已被替代。
        for entry in versions:
            assert isinstance(entry, dict)
            if entry.get("dataset_version") == supersedes and not entry.get("superseded_by"):
                entry["superseded_by"] = dataset_version
                entry["status"] = "superseded"
    versions.append(
        {
            "dataset_version": dataset_version,
            "status": "published",
            "published_at": manifest.get("published_at"),
            "created_at": manifest.get("created_at"),
            "content_digest": manifest.get("content_digest"),
            "manifest_sha256": sha256_file(dataset_directory(lake, name, dataset_version) / "manifest.json"),
            "input_versions": manifest.get("input_versions", []),
            "included_protocols": manifest.get("included_protocols", []),
            "parameters": manifest.get("parameters", {}),
            "total_rows": manifest.get("total_rows", 0),
            "comparable_groups": manifest.get("comparable_groups", []),
            "version_only_groups": manifest.get("version_only_groups", []),
            "supersedes": manifest.get("supersedes"),
            "superseded_by": None,
            "affected_partitions": list((manifest.get("affected_partitions") or {}).get("affected", [])),
        }
    )
    dataset_entry["latest"] = dataset_version
    _save_catalog(lake, catalog)


def _count_exclusions(build: DatasetBuild) -> dict[str, int]:
    counts: dict[str, int] = {}
    for exclusion in build.exclusions:
        counts[exclusion.reason] = counts.get(exclusion.reason, 0) + 1
    return dict(sorted(counts.items()))


def _count_issues(build: DatasetBuild) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in build.issues:
        key = f"{issue.severity}:{issue.code}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def read_manifest(directory: Path) -> dict[str, object]:
    path = directory / "manifest.json"
    if not path.is_file():
        raise PublicationError(f"缺少清单文件: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactVerificationError(f"清单文件不是合法 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ArtifactVerificationError(f"清单文件顶层必须是对象: {path}")
    return payload


def verify_dataset(lake_dir: str | Path, name: str, dataset_version: str) -> list[str]:
    """校验已发布产物的文件哈希，返回问题列表（空表示通过）。"""

    directory = dataset_directory(lake_dir, name, dataset_version)
    manifest = read_manifest(directory)
    problems: list[str] = []
    files = manifest.get("files") or {}
    if not isinstance(files, dict):
        return ["清单缺少 files 段"]
    for relative, info in sorted(files.items()):
        path = directory / str(relative)
        if not path.is_file():
            problems.append(f"缺少文件: {relative}")
            continue
        expected = str((info or {}).get("sha256", ""))
        actual = sha256_file(path)
        if expected != actual:
            problems.append(f"文件哈希不一致: {relative}")
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative == "manifest.json":
            continue
        if relative not in files:
            problems.append(f"清单未记录的文件: {relative}")
    return problems


def trace_dataset(
    lake_dir: str | Path,
    name: str,
    dataset_version: str,
    *,
    run_id: str | None = None,
    member: str | None = None,
) -> dict[str, object]:
    """汇总版本追溯信息；可选按 run_id 或 version/field 定位。"""

    directory = dataset_directory(lake_dir, name, dataset_version)
    manifest = read_manifest(directory)
    lineage = json.loads((directory / "lineage.json").read_text(encoding="utf-8"))
    payload: dict[str, object] = {
        "dataset": {"name": name, "dataset_version": dataset_version},
        "publication": {
            "created_at": manifest.get("created_at"),
            "published_at": manifest.get("published_at"),
            "content_digest": manifest.get("content_digest"),
            "supersedes": manifest.get("supersedes"),
            "previous_content_digest": manifest.get("previous_content_digest"),
            "affected_partitions": manifest.get("affected_partitions"),
        },
        "parameters": manifest.get("parameters"),
        "included_protocols": manifest.get("included_protocols"),
        "protocol_states": manifest.get("protocol_states"),
        "fingerprints": manifest.get("fingerprints"),
        "source_hashes": manifest.get("source_hashes"),
        "protocol_lineage": lineage.get("protocol_lineage"),
        "mapping_lineage": lineage.get("mapping_lineage"),
        "transformations": lineage.get("transformations"),
    }
    if run_id:
        payload["run"] = _find_run(directory, run_id)
    if member:
        payload["field"] = _find_field(directory, member)
    return payload


def _find_run(directory: Path, run_id: str) -> dict[str, object] | None:
    for path in sorted((directory / "partitions").glob("*.csv")):
        import csv

        with path.open("r", encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("run_id") == run_id:
                    return {"partition": path.stem, **row}
    return None


def _find_field(directory: Path, member: str) -> dict[str, object] | None:
    if "/" not in member:
        return None
    version, field_id = member.split("/", 1)
    import csv

    with (directory / "metrics.csv").open("r", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("version") == version and row.get("field_id") == field_id:
                return dict(row)
    return None


def rebuild_parameters(lake_dir: str | Path, name: str, dataset_version: str) -> BuildParameters:
    manifest = read_manifest(dataset_directory(lake_dir, name, dataset_version))
    parameters = manifest.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise PublicationError("清单中的 parameters 段无效")
    return BuildParameters.from_dict(parameters)


def affected_partitions_for_dataset(
    lake_dir: str | Path, name: str, dataset_version: str
) -> Mapping[str, object]:
    catalog = load_catalog(lake_dir)
    for entry in _dataset_entries(catalog, name):
        if entry.get("dataset_version") == dataset_version:
            return {
                "superseded_by": entry.get("superseded_by"),
                "affected_partitions": entry.get("affected_partitions") or [],
            }
    raise PublicationError(f"catalog 中不存在 {name}/{dataset_version}")
