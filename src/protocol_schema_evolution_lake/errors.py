"""项目内可预期的错误类型。"""

from __future__ import annotations


class LakeError(Exception):
    """所有数据湖能力错误的基类。"""


class SourceFormatError(LakeError):
    """输入资料不符合资料契约（列名、编码、空行等）。"""


class ConflictingDuplicateError(LakeError):
    """同一业务键出现内容不一致的重复行。"""


class PhysicalSchemaConflict(LakeError):
    """同一字段的物理定义（名称/类型/单位）互相冲突。"""


class ReferentialIntegrityError(LakeError):
    """记录引用了不存在的方案、版本或字段。"""


class ReviewError(LakeError):
    """语义评审资料不合法。"""


class TransformError(LakeError):
    """数值转换无法执行（缺少依据、除零、类型不符等）。"""


class PublicationError(LakeError):
    """数据集发布失败。"""


class ImmutableArtifactError(PublicationError):
    """试图覆盖已发布的数据集版本。"""


class ArtifactVerificationError(PublicationError):
    """已发布产物与清单哈希不一致。"""
