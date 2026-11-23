"""跨实验室协议版本迁移与事实表兼容。

公开 API：

* :func:`load_registry` —— 装载并校验资料与治理记录；
* :func:`assess_compatibility` / :func:`build_groups` —— 兼容性裁决与指标分组；
* :func:`build_dataset` / :func:`publish` —— 生成并发布不可变数据产品；
* :func:`verify_dataset` / :func:`trace_dataset` —— 产物校验与版本追溯。
"""

from .compatibility import assess_compatibility, build_groups
from .dataset import (
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
from .errors import LakeError
from .model import (
    OUTCOME_MERGEABLE,
    Annotation,
    DatasetBuild,
    EdgeAssessment,
    FieldDefinition,
    Issue,
    MappingKey,
    MetricGroup,
    ProtocolVersion,
    RunRecord,
    TransformSpec,
    VersionField,
)
from .registry import Registry, load_registry

__all__ = [
    "Annotation",
    "BuildParameters",
    "DatasetBuild",
    "EdgeAssessment",
    "FieldDefinition",
    "Issue",
    "LakeError",
    "MappingKey",
    "MetricGroup",
    "OUTCOME_MERGEABLE",
    "ProtocolVersion",
    "Registry",
    "RunRecord",
    "TransformSpec",
    "VersionField",
    "affected_partitions_for_dataset",
    "assess_compatibility",
    "build_dataset",
    "build_groups",
    "compare_partitions",
    "content_digest",
    "load_catalog",
    "load_registry",
    "next_dataset_version",
    "publish",
    "rebuild_parameters",
    "resolve_parameters",
    "trace_dataset",
    "verify_dataset",
]
