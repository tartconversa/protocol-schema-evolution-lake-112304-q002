"""兼容性裁决：物理证据 + 治理审批 + 实验负责人语义评审。

关键规则
--------
* 映射表里的 ``transform_note`` 只是“声明”，不是证据。必须与字段的物理定义
  （名称、单位）核对；声明与物理事实矛盾时一律不可合并。
* ``split`` 表示一个旧字段被拆成多个观察项。历史记录无法在不丢失信息的前提下
  平移到单个新字段，因此即使评审通过也不能做行级重表达，只能要求重观测。
* 审批状态冲突、语义评审冲突、换算依据缺失都会阻止自动合并。
* 只有同时满足物理证据、映射审批、语义评审与必要换算的边才允许合并。
"""

from __future__ import annotations

from collections import defaultdict, deque
from fractions import Fraction
from typing import Iterable, Mapping

from .model import (
    EVIDENCE_CONTRADICTED,
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_NOT_APPLICABLE,
    EVIDENCE_VALID,
    METRIC_COMPARABLE,
    METRIC_VERSION_ONLY,
    OUTCOME_CLAIM_CONTRADICTED,
    OUTCOME_CONVERSION_MISSING,
    OUTCOME_CONVERSION_PENDING,
    OUTCOME_CYCLE_INCONSISTENT,
    OUTCOME_DANGLING,
    OUTCOME_GOVERNANCE_CONFLICT,
    OUTCOME_INVALID_EDGE,
    OUTCOME_MERGEABLE,
    OUTCOME_PENDING,
    OUTCOME_PHYSICAL_CONFLICT,
    OUTCOME_REJECTED,
    OUTCOME_REVIEW_CONFLICT,
    OUTCOME_REVIEW_MISSING,
    OUTCOME_REVIEW_PENDING,
    OUTCOME_REVIEW_REJECTED,
    OUTCOME_REVIEW_INVALID,
    OUTCOME_SEMANTIC_CHANGE,
    OUTCOME_SPLIT_INCOMPLETE,
    OUTCOME_SPLIT_REOBSERVE,
    OUTCOME_UNRESOLVED,
    EdgeAssessment,
    Issue,
    MappingKey,
    MetricGroup,
    MetricRow,
    TransformSpec,
    VersionField,
)
from .registry import Registry
from .util import version_key

_SAME_MEANING_JUDGMENTS = frozenset({"same_quantity", "renamed_only"})


def _version_field_or_none(registry: Registry, version: str, field_id: str) -> VersionField | None:
    return registry.version_fields.get((version, field_id))


def _split_target_counts(registry: Registry) -> dict[tuple[str, str], set[str]]:
    """统计声明为 split 的边，每个源字段实际指向多少个不同目标字段。

    若该映射键存在治理裁定，以裁定的 ``effective_note`` 为准，避免原始重复行
    的矛盾声明干扰拆分完整性判断。
    """

    counts: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key, edge in registry.mapping_edges.items():
        adjudication = registry.adjudications.get(key)
        note = (
            adjudication.latest.effective_note if adjudication is not None else edge.latest.transform_note
        )
        if note == "split":
            counts[(key.source_version, key.source_field)].add(key.target_field)
    return counts


def assess_compatibility(registry: Registry) -> tuple[tuple[EdgeAssessment, ...], tuple[Issue, ...]]:
    """对全部映射键做裁决，并返回问题记录。"""

    split_targets = _split_target_counts(registry)
    assessments: list[EdgeAssessment] = []
    issues: list[Issue] = []
    for key in sorted(registry.mapping_edges):
        source = _version_field_or_none(registry, key.source_version, key.source_field)
        target = _version_field_or_none(registry, key.target_version, key.target_field)
        assessment = _assess_one(
            registry, key, source, target, split_targets, issues
        )
        assessments.append(assessment)
    assessments = _enforce_transform_cycles(assessments, issues)
    return tuple(assessments), tuple(issues)


def _assess_one(
    registry: Registry,
    key: MappingKey,
    source: VersionField | None,
    target: VersionField | None,
    split_targets: Mapping[tuple[str, str], set[str]],
    issues: list[Issue],
) -> EdgeAssessment:
    edge = registry.mapping_edges[key]
    adjudication = registry.adjudications.get(key)
    if adjudication is not None:
        latest_adjudication = adjudication.latest
        claim = latest_adjudication.effective_note
        state = latest_adjudication.effective_state
        adjudication_id = latest_adjudication.adjudication_id
        adjudication_basis = latest_adjudication.basis
        adjudication_conflict = adjudication.conflict
    else:
        claim = edge.latest.transform_note
        state = edge.latest.approval_state
        adjudication_id = ""
        adjudication_basis = ""
        adjudication_conflict = False
    reasons: list[str] = []
    decision_conflict = (edge.decision_conflict or edge.note_conflict) and adjudication is None
    if adjudication_conflict:
        reasons.append(
            "数据治理裁定记录本身互相冲突，兼容范围暂不可用"
        )
    if adjudication is not None:
        reasons.append(
            f"治理裁定 {adjudication_id} 生效：审批={state}，说明={claim}"
        )
    review = registry.reviews.get(key)
    review_conflict = bool(review and review.decision_conflict)
    review_id = review.latest.review_id if review else ""
    review_state = review.latest.review_state if review else "missing"
    semantic_basis = ""

    def build(
        outcome: str,
        evidence: str,
        *,
        transform: TransformSpec | None = None,
        extra_reasons: Iterable[str] = (),
        semantic: str | None = None,
    ) -> EdgeAssessment:
        merged_reasons = list(reasons)
        merged_reasons.extend(extra_reasons)
        return EdgeAssessment(
            key=key,
            claim=claim,
            latest_state=state,
            evidence_status=evidence,
            outcome=outcome,
            reasons=tuple(merged_reasons),
            transform=transform,
            semantic_basis=semantic if semantic is not None else semantic_basis,
            review_id=review_id,
            review_state=review_state,
            decision_conflict=edge.decision_conflict,
            note_conflict=edge.note_conflict,
            review_conflict=review_conflict,
            history=edge.history,
            adjudication_id=adjudication_id,
            adjudication_basis=adjudication_basis,
        )

    if source is None or target is None:
        missing = []
        if source is None:
            missing.append(f"源字段 {key.source_version}/{key.source_field}")
        if target is None:
            missing.append(f"目标字段 {key.target_version}/{key.target_field}")
        return build(
            OUTCOME_DANGLING,
            EVIDENCE_NOT_APPLICABLE,
            extra_reasons=(f"映射引用了未登记的字段: {'、'.join(missing)}",),
        )
    if not source.physical_resolved or not target.physical_resolved:
        return build(
            OUTCOME_PHYSICAL_CONFLICT,
            EVIDENCE_CONTRADICTED,
            extra_reasons=("源或目标字段在不同实验室间的名称/单位不一致，不能定义兼容口径",),
        )

    source_unit = source.unit or ""
    target_unit = target.unit or ""
    source_name = source.name or ""
    target_name = target.name or ""

    # ---- 物理证据 -----------------------------------------------------
    if claim == "same":
        if source_unit != target_unit:
            return build(
                OUTCOME_CLAIM_CONTRADICTED,
                EVIDENCE_CONTRADICTED,
                extra_reasons=(
                    f"声明 same，但源单位 {source_unit!r} 与目标单位 {target_unit!r} 不同；"
                    "同名同义的声明与物理定义矛盾",
                ),
            )
        reasons.append(
            f"物理单位一致（{source_unit}）"
            + ("，名称一致" if source_name == target_name else f"，名称由 {source_name!r} 改为 {target_name!r}")
        )
    elif claim == "unit-convert":
        if source_name != target_name:
            return build(
                OUTCOME_CLAIM_CONTRADICTED,
                EVIDENCE_CONTRADICTED,
                extra_reasons=(
                    f"声明 unit-convert，但源字段名 {source_name!r} 与目标字段名 {target_name!r} "
                    "不同；不同观测指标不能仅靠单位换算等同",
                ),
            )
        if source_unit == target_unit:
            return build(
                OUTCOME_CLAIM_CONTRADICTED,
                EVIDENCE_CONTRADICTED,
                extra_reasons=(f"声明 unit-convert，但源/目标单位同为 {source_unit!r}，无须换算",),
            )
        reasons.append(f"同名指标单位由 {source_unit} 换算为 {target_unit}")
    elif claim == "split":
        targets = split_targets.get((key.source_version, key.source_field), set())
        if len(targets) < 2:
            return build(
                OUTCOME_SPLIT_INCOMPLETE,
                EVIDENCE_CONTRADICTED,
                extra_reasons=(
                    f"声明 split，但源字段 {key.source_field} 只登记了 "
                    f"{len(targets)} 个目标字段；拆分声明不完整，无法解释旧值去向",
                ),
            )
        return build(
            OUTCOME_SPLIT_REOBSERVE,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(
                f"源字段 {key.source_field} 被拆为 {len(targets)} 个观察项；"
                "单个旧值无法无歧义地重表达为其中任一项，需按新协议重新观测",
            ),
        )
    elif claim == "unresolved":
        return build(
            OUTCOME_UNRESOLVED,
            EVIDENCE_NOT_APPLICABLE,
            extra_reasons=("映射声明为 unresolved，未给出可执行的对应关系",),
        )
    else:  # pragma: no cover - 词表已在装载时校验
        return build(
            OUTCOME_INVALID_EDGE,
            EVIDENCE_CONTRADICTED,
            extra_reasons=(f"未知 transform_note: {claim!r}",),
        )

    # ---- 治理审批 -----------------------------------------------------
    if adjudication_conflict:
        return build(
            OUTCOME_GOVERNANCE_CONFLICT,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=("数据治理针对同一映射给出的裁定互相冲突，无法确定兼容范围",),
        )
    if decision_conflict:
        detail = "、".join(
            f"{item.mapping_id}={item.approval_state}" for item in edge.history
        )
        return build(
            OUTCOME_GOVERNANCE_CONFLICT,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(
                f"同一映射的重复记录审批状态互相冲突（{detail}）；"
                "冲突未裁决前不得自动合并",
            ),
        )
    if edge.note_conflict and adjudication is None:
        return build(
            OUTCOME_GOVERNANCE_CONFLICT,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=("同一映射的重复记录转换说明互相冲突，不能确定应执行哪种变换",),
        )
    decision_label = adjudication_id or edge.latest.mapping_id
    if state == "rejected":
        return build(
            OUTCOME_REJECTED,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"映射 {decision_label} 已被数据治理拒绝",),
        )
    if state != "approved":
        return build(
            OUTCOME_PENDING,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"映射 {decision_label} 仍为 {state}，尚未获数据治理批准",),
        )

    # ---- 实验负责人语义评审 -------------------------------------------
    if review is None:
        return build(
            OUTCOME_REVIEW_MISSING,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=("缺少实验负责人对科学含义的判断，不能仅凭字段名与单位合并",),
        )
    if review_conflict:
        return build(
            OUTCOME_REVIEW_CONFLICT,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=("同一映射存在互相冲突的语义评审，需先裁决",),
        )
    latest_review = review.latest
    semantic_basis = f"{latest_review.review_id}: {latest_review.judgment}；{latest_review.basis}"
    if latest_review.review_state == "pending":
        return build(
            OUTCOME_REVIEW_PENDING,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"语义评审 {latest_review.review_id} 尚未批准",),
        )
    if latest_review.review_state == "rejected":
        return build(
            OUTCOME_REVIEW_REJECTED,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"语义评审 {latest_review.review_id} 被拒绝",),
        )
    if latest_review.judgment == "changed_meaning":
        return build(
            OUTCOME_SEMANTIC_CHANGE,
            EVIDENCE_CONTRADICTED,
            extra_reasons=(
                f"实验负责人判定科学含义已改变（{latest_review.review_id}）；"
                "不得作为同一指标跨版本合并",
            ),
            semantic=semantic_basis,
        )
    if latest_review.judgment == "split":
        return build(
            OUTCOME_SPLIT_REOBSERVE,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"语义评审 {latest_review.review_id} 确认字段被拆分，需重新观测",),
            semantic=semantic_basis,
        )
    if latest_review.judgment not in _SAME_MEANING_JUDGMENTS:
        return build(
            OUTCOME_REVIEW_INVALID,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"语义评审结论 {latest_review.judgment!r} 不足以保证可比较",),
            semantic=semantic_basis,
        )

    # ---- 变换依据 -----------------------------------------------------
    if claim == "same":
        transform = TransformSpec.identity(
            source_unit,
            basis=(
                f"映射 {edge.latest.mapping_id} 获数据治理批准；"
                f"语义评审 {latest_review.review_id} 判定科学含义未变"
            ),
        )
        return build(
            OUTCOME_MERGEABLE,
            EVIDENCE_VALID,
            transform=transform,
            extra_reasons=("全部依据齐备：物理一致、映射已批、语义评审通过",),
            semantic=semantic_basis,
        )

    # claim == "unit-convert"
    conversion, reverse = _find_conversion(registry, source_unit, target_unit)
    if conversion is None:
        return build(
            OUTCOME_CONVERSION_MISSING,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"缺少经批准的单位换算依据 {source_unit} → {target_unit}",),
            semantic=semantic_basis,
        )
    if conversion.decision_conflict or conversion.factor_conflict:
        detail = "、".join(
            f"{item.conversion_id}={item.approval_state}/{item.factor}"
            for item in conversion.history
        )
        return build(
            OUTCOME_GOVERNANCE_CONFLICT,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"单位换算记录互相冲突（{detail}），不能自动采用",),
            semantic=semantic_basis,
        )
    latest_conversion = conversion.latest
    if latest_conversion.approval_state == "pending":
        return build(
            OUTCOME_CONVERSION_PENDING,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"单位换算 {latest_conversion.conversion_id} 尚未批准",),
            semantic=semantic_basis,
        )
    if latest_conversion.approval_state != "approved":
        return build(
            OUTCOME_CONVERSION_MISSING,
            EVIDENCE_INSUFFICIENT,
            extra_reasons=(f"单位换算 {latest_conversion.conversion_id} 已被拒绝",),
            semantic=semantic_basis,
        )
    spec = TransformSpec(
        "linear",
        latest_conversion.factor,
        latest_conversion.offset,
        latest_conversion.source_unit,
        latest_conversion.target_unit,
        basis=(
            f"换算 {latest_conversion.conversion_id}（{latest_conversion.approved_by} 批准）："
            f"{latest_conversion.basis}"
        ),
    )
    if reverse:
        spec = spec.inverse()
    unit_note = "（反向使用登记记录）" if reverse else ""
    return build(
        OUTCOME_MERGEABLE,
        EVIDENCE_VALID,
        transform=spec,
        extra_reasons=(
            f"全部依据齐备：{source_unit}→{target_unit} 换算已批准{unit_note}",
        ),
        semantic=semantic_basis,
    )


def _find_conversion(
    registry: Registry, source_unit: str, target_unit: str
) -> tuple[object | None, bool]:
    direct = registry.conversions.get((source_unit, target_unit))
    if direct is not None:
        return direct, False
    reverse = registry.conversions.get((target_unit, source_unit))
    if reverse is not None:
        return reverse, True
    return None, False


def _enforce_transform_cycles(
    assessments: list[EdgeAssessment], issues: list[Issue]
) -> list[EdgeAssessment]:
    """检查可合并边的变换是否在任意环上闭合；不一致则整组禁用。

    做法：把每条可合并边视为带线性变换的双向图，对每个连通组从根做 BFS，
    为每个节点求出“换算到根单位”的仿射变换。同一节点若由不同路径得到不同
    变换，说明映射关系自相矛盾。
    """

    edges: list[tuple[tuple[str, str], tuple[str, str], TransformSpec]] = []
    nodes: set[tuple[str, str]] = set()
    for assessment in assessments:
        if not assessment.mergeable or assessment.transform is None:
            continue
        edges.append((assessment.key.source, assessment.key.target, assessment.transform))
        nodes.add(assessment.key.source)
        nodes.add(assessment.key.target)

    adjacency = _undirected_adjacency(edges)
    inconsistent_nodes: set[tuple[str, str]] = set()
    for component in _components(nodes, adjacency):
        root = sorted(component)[0]
        if _compute_potentials(component, adjacency, root) is None:
            inconsistent_nodes |= component
            issues.append(
                Issue(
                    code="transform_cycle_inconsistent",
                    severity="error",
                    message=(
                        "可合并映射在组合环上无法闭合（由多个路径得到的换算结果不一致）；"
                        "该连通组内所有映射暂停合并"
                    ),
                    subject=str(sorted(component)),
                )
            )

    if not inconsistent_nodes:
        return assessments
    updated: list[EdgeAssessment] = []
    for assessment in assessments:
        if assessment.mergeable and (
            assessment.key.source in inconsistent_nodes
            or assessment.key.target in inconsistent_nodes
        ):
            updated.append(
                assessment.with_outcome(
                    OUTCOME_CYCLE_INCONSISTENT,
                    "该映射属于换算不一致的连通组，暂停合并",
                )
            )
        else:
            updated.append(assessment)
    return updated


def _undirected_adjacency(
    edges: Iterable[tuple[tuple[str, str], tuple[str, str], TransformSpec]]
) -> dict[tuple[str, str], list[tuple[tuple[str, str], TransformSpec, bool]]]:
    """邻接表项：``(neighbor, transform, forward)``。

    ``forward=True`` 表示 ``transform`` 把当前节点的值表达为邻居单位下的值。
    """

    adjacency: dict[tuple[str, str], list[tuple[tuple[str, str], TransformSpec, bool]]] = defaultdict(list)
    for source, target, transform in edges:
        adjacency[source].append((target, transform, True))
        adjacency[target].append((source, transform, False))
    return adjacency


def _components(
    nodes: Iterable[tuple[str, str]],
    adjacency: Mapping[tuple[str, str], list[tuple[tuple[str, str], TransformSpec, bool]]],
) -> list[set[tuple[str, str]]]:
    remaining = set(nodes)
    components: list[set[tuple[str, str]]] = []
    while remaining:
        start = remaining.pop()
        component = {start}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for neighbor, _, _ in adjacency.get(node, []):
                if neighbor not in component:
                    component.add(neighbor)
                    remaining.discard(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return components


def _compute_potentials(
    component: set[tuple[str, str]],
    adjacency: Mapping[tuple[str, str], list[tuple[tuple[str, str], TransformSpec, bool]]],
    root: tuple[str, str],
) -> dict[tuple[str, str], tuple[Fraction, Fraction]] | None:
    """为组内每个节点求“换算到根单位”的仿射映射 (factor, offset)。

    变换按仿射 ``y = x * factor + offset`` 组合。根节点固定为单位映射。
    任何一条路径给出的系数与已有记录不同，即判定该组不自洽。
    """

    potentials: dict[tuple[str, str], tuple[Fraction, Fraction]] = {
        root: (Fraction(1), Fraction(0))
    }
    queue = deque([root])
    while queue:
        node = queue.popleft()
        base_factor, base_offset = potentials[node]
        for neighbor, transform, forward in adjacency.get(node, []):
            step_factor = Fraction(transform.factor)
            step_offset = Fraction(transform.offset)
            if forward:
                # 邻居 → 根 = transform.inverse() 之后再 base。
                if step_factor == 0:
                    return None
                inv_factor = Fraction(1) / step_factor
                inv_offset = -step_offset * inv_factor
                candidate = _compose_affine(inv_factor, inv_offset, base_factor, base_offset)
            else:
                candidate = _compose_affine(base_factor, base_offset, step_factor, step_offset)
            existing = potentials.get(neighbor)
            if existing is None:
                potentials[neighbor] = candidate
                queue.append(neighbor)
            elif existing != candidate:
                return None
    return potentials


def _compose_affine(
    first_factor: Fraction,
    first_offset: Fraction,
    second_factor: Fraction,
    second_offset: Fraction,
) -> tuple[Fraction, Fraction]:
    """先执行 (first) 再执行 (second)。"""

    return (
        first_factor * second_factor,
        first_offset * second_factor + second_offset,
    )


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}

    def add(self, item: tuple[str, str]) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: tuple[str, str]) -> tuple[str, str]:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: tuple[str, str], right: tuple[str, str]) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root

    def components(self) -> dict[tuple[str, str], tuple[tuple[str, str], ...]]:
        grouped: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for item in self.parent:
            grouped[self.find(item)].append(item)
        return {
            root: tuple(sorted(members))
            for root, members in grouped.items()
        }


def _canonical_transform_map(
    members: tuple[tuple[str, str], ...],
    canonical: tuple[str, str],
    canonical_unit: str,
    mergeable: Mapping[MappingKey, TransformSpec],
) -> dict[tuple[str, str], TransformSpec]:
    """用 BFS 计算每个成员值换算到 canonical 单位的变换。"""

    edges = [(key.source, key.target, transform) for key, transform in mergeable.items()]
    adjacency = _undirected_adjacency(edges)
    result: dict[tuple[str, str], TransformSpec] = {
        canonical: TransformSpec.identity(canonical_unit, basis="基准字段，值保持不变")
    }
    queue: deque[tuple[str, str]] = deque([canonical])
    member_set = set(members)
    while queue:
        node = queue.popleft()
        for neighbor, transform, forward in adjacency.get(node, []):
            if neighbor in result or neighbor not in member_set:
                continue
            # 先得到 neighbor → node 的变换。
            if forward:
                # 边从 node 指向 neighbor：neighbor → node 用逆变换。
                step = transform.inverse()
            else:
                # 边从 neighbor 指向 node：正向变换即 neighbor → node。
                step = transform
            result[neighbor] = step.then(result[node])
            queue.append(neighbor)
    return result


def build_groups(
    registry: Registry,
    assessments: tuple[EdgeAssessment, ...],
    versions: Iterable[str] | None = None,
    target_version: str | None = None,
) -> tuple[tuple[MetricGroup, ...], tuple[MetricRow, ...], tuple[Issue, ...]]:
    """在指定版本范围内生成指标组与逐版本状态表。"""

    requested = (
        tuple(sorted(set(versions))) if versions is not None else registry.versions
    )
    issues: list[Issue] = []
    union = _UnionFind()
    in_scope: list[tuple[str, str]] = []
    for version, field_id in sorted(registry.version_fields):
        if version in requested:
            union.add((version, field_id))
            in_scope.append((version, field_id))

    mergeable_transforms: dict[MappingKey, TransformSpec] = {}
    edge_outcomes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for assessment in assessments:
        key = assessment.key
        if key.source_version not in requested or key.target_version not in requested:
            continue
        if assessment.mergeable and assessment.transform is not None:
            if key.source in union.parent and key.target in union.parent:
                union.union(key.source, key.target)
                mergeable_transforms[key] = assessment.transform
        else:
            for endpoint in (key.source, key.target):
                if endpoint in union.parent:
                    edge_outcomes[endpoint].append(assessment.outcome)

    components = union.components()
    version_field_lookup = registry.version_fields

    # 统一传递合并可能把“彼此被判不可合并”的两个字段绕道并进同一组。
    # 例如 A→B、A→C 可合并，但 B→C 已被拒绝；B 与 C 不应仅因共享 A 就被视为可比。
    # 这里收集组内所有显式非合并裁决，作为阻断依据。
    rejected_pairs: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for assessment in assessments:
        key = assessment.key
        if key.source_version not in requested or key.target_version not in requested:
            continue
        if assessment.mergeable:
            continue
        for node in (key.source, key.target):
            rejected_pairs[node].append(
                {
                    "source": list(key.source),
                    "target": list(key.target),
                    "outcome": assessment.outcome,
                    "reasons": list(assessment.reasons),
                }
            )

    groups: list[MetricGroup] = []
    member_to_group: dict[tuple[str, str], MetricGroup] = {}
    for members in sorted(components.values(), key=lambda items: items[0]):
        # canonical：显式目标版本优先，否则取版本号最大的成员。
        member_versions = sorted({version for version, _ in members}, key=version_key)
        if target_version is not None and target_version in member_versions:
            canonical_version = target_version
        else:
            canonical_version = max(member_versions, key=version_key)
        candidates = [item for item in members if item[0] == canonical_version]
        canonical = sorted(candidates)[0]
        canonical_vf = version_field_lookup.get(canonical)
        merged = len(members) > 1
        reasons: list[str] = []
        buildable = True
        group_semantic_ref = ""
        group_basis = ""
        transforms: list[tuple[str, str, TransformSpec]] = []
        transform_edges: list[MappingKey] = []

        if canonical_vf is None or not canonical_vf.physical_resolved:
            buildable = False
            reasons.append("基准字段在不同实验室间名称/单位不一致，无法定义统一口径")
        for member in members:
            vf = version_field_lookup.get(member)
            if vf is None or not vf.physical_resolved:
                buildable = False
                reasons.append(f"{member[0]}/{member[1]} 的物理定义跨实验室不一致")
        if merged:
            to_canonical = _canonical_transform_map(
                members, canonical, canonical_vf.unit or "", mergeable_transforms
            )
            for member in members:
                spec = to_canonical.get(member)
                if spec is None:
                    buildable = False
                    reasons.append(f"{member[0]}/{member[1]} 与基准之间缺少连续的可合并路径")
                    continue
                transforms.append((member[0], member[1], spec))
            transform_edges = [
                key
                for key, spec in sorted(mergeable_transforms.items())
                if key.source in members and key.target in members
            ]
            for key in transform_edges:
                assessment = next((item for item in assessments if item.key == key), None)
                if assessment and assessment.semantic_basis:
                    group_basis = assessment.semantic_basis
                    break
            if not group_basis:
                reasons.append("合并组缺少语义评审依据")
                buildable = False
        else:
            transforms.append((canonical[0], canonical[1], TransformSpec.identity(canonical_vf.unit or "")))
            if canonical_vf.annotation_status != "resolved":
                reasons.append("字段治理注解存在争议，只能按版本单独使用")

        if canonical_vf is not None and canonical_vf.annotation_status == "resolved":
            group_semantic_ref = canonical_vf.semantic_ref or ""

        if merged:
            explicit_conflicts = [
                item
                for member in members
                for item in rejected_pairs.get(member, [])
                if tuple(item["source"]) in members and tuple(item["target"]) in members
            ]
            if explicit_conflicts:
                buildable = False
                conflicts = sorted(
                    {
                        f"{item['source'][0]}/{item['source'][1]}↔"
                        f"{item['target'][0]}/{item['target'][1]}:{item['outcome']}"
                        for item in explicit_conflicts
                    }
                )
                reasons.append(
                    "组内存在显式的不可合并裁决，不能仅因共享同一上游字段而视为可比："
                    + "、".join(conflicts)
                )

        if merged and buildable:
            status = METRIC_COMPARABLE
        else:
            status = METRIC_VERSION_ONLY
        if not merged:
            endpoint_reasons: list[str] = []
            for member in members:
                endpoint_reasons.extend(edge_outcomes.get(member, []))
            if not endpoint_reasons:
                endpoint_reasons.append("该版本字段没有与其它版本之间获批准的映射")
            for outcome in endpoint_reasons:
                reasons.append(f"映射结论: {outcome}")
        reasons = list(dict.fromkeys(reasons))
        group_id = f"G-{members[0][0]}-{members[0][1]}"
        group = MetricGroup(
            group_id=group_id,
            canonical_version=canonical[0],
            canonical_field=canonical[1],
            name=canonical_vf.name if canonical_vf and canonical_vf.name else "",
            unit=canonical_vf.unit if canonical_vf and canonical_vf.unit else "",
            members=members,
            merged=merged,
            buildable=buildable,
            annotation_status=canonical_vf.annotation_status if canonical_vf else "unknown",
            semantic_ref=group_semantic_ref,
            semantic_basis=group_basis if merged else "",
            status=status,
            reasons=tuple(reasons),
            transforms=tuple(transforms),
            transform_edges=tuple(transform_edges),
        )
        groups.append(group)
        for member in members:
            member_to_group[member] = group

    metrics: list[MetricRow] = []
    for version, field_id in in_scope:
        vf = version_field_lookup[(version, field_id)]
        group = member_to_group[(version, field_id)]
        metrics.append(
            MetricRow(
                version=version,
                field_id=field_id,
                name=vf.name or "",
                unit=vf.unit or "",
                dtypes="|".join(sorted(vf.dtypes)),
                dtype_status=vf.dtype_status,
                annotation_status=vf.annotation_status,
                semantic_ref=vf.semantic_ref or "",
                group_id=group.group_id,
                group_merged=group.merged,
                status=group.status,
                reasons=group.reasons,
            )
        )
    groups.sort(key=lambda item: item.group_id)
    return tuple(groups), tuple(metrics), tuple(issues)
