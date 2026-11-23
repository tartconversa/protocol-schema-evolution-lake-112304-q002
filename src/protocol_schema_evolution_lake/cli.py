"""命令行入口。

子命令：
  build            装载资料并输出兼容性报告与构建摘要（不落盘发布）
  publish          构建并发布为不可变数据集版本
  verify           校验已发布目录的文件哈希
  trace            追溯数据集版本、映射依据与具体记录
  catalog          列出一个数据湖中已发布的数据集
  lineage          输出协议族与映射决策血统
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compatibility import assess_compatibility
from .dataset import (
    affected_partitions_for_dataset,
    build_dataset,
    load_catalog,
    next_dataset_version,
    publish,
    rebuild_parameters,
    resolve_parameters,
    trace_dataset,
    verify_dataset,
)
from .errors import LakeError
from .registry import load_registry


def _add_governance_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reviews", type=Path, default=None, help="实验负责人语义评审 CSV")
    parser.add_argument("--conversions", type=Path, default=None, help="单位换算登记 CSV")
    parser.add_argument("--adjudications", type=Path, default=None, help="数据治理映射裁定 CSV")


def _load(args: argparse.Namespace):
    return load_registry(
        args.data,
        reviews_path=args.reviews,
        conversions_path=args.conversions,
        adjudications_path=args.adjudications,
    )


def _parameters(args: argparse.Namespace):
    return resolve_parameters(
        registry=args.registry,
        include_states=args.include_states,
        versions=args.versions,
        target_version=args.target_version,
        fields=args.fields,
    )


def _summary(build) -> dict[str, object]:
    return {
        "name": build.name,
        "created_at": build.created_at,
        "input_versions": list(build.input_versions),
        "included_protocols": list(build.included_protocols),
        "include_states": list(build.include_states),
        "total_rows": build.total_rows,
        "partition_count": len(build.partitions),
        "comparable_groups": sorted(
            group.group_id for group in build.groups if group.status == "cross_version_comparable"
        ),
        "version_only_groups": sorted(
            group.group_id for group in build.groups if group.status != "cross_version_comparable"
        ),
        "exclusions": _count([item.reason for item in build.exclusions]),
        "issues": _count([f"{item.severity}:{item.code}" for item in build.issues]),
    }


def _count(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def command_lineage(args: argparse.Namespace) -> int:
    registry = _load(args)
    payload = {
        "versions": list(registry.versions),
        "protocol_lineage": registry.protocol_lineage(),
        "mapping_lineage": registry.mapping_lineage(),
        "source_hashes": registry.source_hashes,
        "fingerprints": {
            "registry": registry.registry_fingerprint,
            "mappings": registry.mapping_fingerprint,
            "semantic_reviews": registry.review_fingerprint,
            "unit_conversions": registry.conversion_fingerprint,
            "mapping_adjudications": registry.adjudication_fingerprint,
        },
    }
    _print_json(payload)
    return 0


def command_build(args: argparse.Namespace) -> int:
    args.registry = _load(args)
    parameters = _parameters(args)
    build = build_dataset(args.registry, args.name, parameters)
    assessments, _ = assess_compatibility(args.registry)
    payload = _summary(build)
    payload["compatibility"] = [item.to_dict() for item in build.assessments]
    payload["groups"] = [group.to_dict() for group in build.groups]
    payload["exclusions_detail"] = [item.to_dict() for item in build.exclusions]
    payload["issues_detail"] = [item.to_dict() for item in build.issues]
    _print_json(payload)
    return 0


def command_publish(args: argparse.Namespace) -> int:
    registry = _load(args)
    parameters = resolve_parameters(
        registry,
        include_states=args.include_states,
        versions=args.versions,
        target_version=args.target_version,
        fields=args.fields,
    )
    build = build_dataset(registry, args.name, parameters)
    dataset_version = args.dataset_version or next_dataset_version(args.lake, args.name)
    result = publish(
        build,
        registry,
        args.lake,
        dataset_version,
        supersedes=args.supersedes,
    )
    payload = {
        **_summary(build),
        "lake": str(Path(args.lake)),
        "dataset_version": dataset_version,
        "directory": str(result.directory),
        "created": result.created,
        "supersedes": result.supersedes,
        "affected_partitions": result.affected_partitions,
    }
    _print_json(payload)
    return 0


def command_verify(args: argparse.Namespace) -> int:
    problems = verify_dataset(args.lake, args.name, args.dataset_version)
    if problems:
        _print_json({"ok": False, "problems": problems})
        return 1
    _print_json({"ok": True, "dataset": f"{args.name}/{args.dataset_version}"})
    return 0


def command_trace(args: argparse.Namespace) -> int:
    payload = trace_dataset(
        args.lake,
        args.name,
        args.dataset_version,
        run_id=args.run_id,
        member=args.member,
    )
    payload["catalog_status"] = affected_partitions_for_dataset(
        args.lake, args.name, args.dataset_version
    )
    _print_json(payload)
    return 0


def command_catalog(args: argparse.Namespace) -> int:
    _print_json(load_catalog(args.lake))
    return 0


def command_rebuild(args: argparse.Namespace) -> int:
    """按已发布版本的参数重建；用于验证可重复性。"""

    registry = _load(args)
    parameters = rebuild_parameters(args.lake, args.name, args.dataset_version)
    dataset_version = args.new_dataset_version or next_dataset_version(args.lake, args.name)
    build = build_dataset(registry, args.name, parameters)
    result = publish(
        build,
        registry,
        args.lake,
        dataset_version,
        supersedes=args.dataset_version,
    )
    _print_json(
        {
            **_summary(build),
            "rebuilt_from": args.dataset_version,
            "dataset_version": dataset_version,
            "created": result.created,
            "affected_partitions": result.affected_partitions,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protocol-lake",
        description="跨实验室协议版本迁移与事实表兼容工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser, *, need_name: bool = False) -> None:
        sub.add_argument("--data", type=Path, required=True, help="资料目录（四个契约 CSV）")
        _add_governance_options(sub)
        if need_name:
            sub.add_argument("--name", required=True, help="数据集名称")
        sub.add_argument(
            "--include-states",
            default="approved",
            help="纳入的方案状态，逗号分隔（默认 approved）",
        )
        sub.add_argument("--versions", default=None, help="纳入的版本，逗号分隔（默认全部）")
        sub.add_argument("--target-version", default=None, help="合并组的基准版本")
        sub.add_argument("--fields", default=None, help="仅纳入 version/field_id，逗号分隔")

    build_cmd = subparsers.add_parser("build", help="生成构建摘要与兼容性报告")
    add_common(build_cmd, need_name=True)
    build_cmd.set_defaults(func=command_build)

    publish_cmd = subparsers.add_parser("publish", help="发布不可变数据集版本")
    add_common(publish_cmd, need_name=True)
    publish_cmd.add_argument("--lake", type=Path, required=True, help="数据湖根目录")
    publish_cmd.add_argument("--dataset-version", default=None, help="数据集版本标签")
    publish_cmd.add_argument("--supersedes", default=None, help="被替代的旧数据集版本")
    publish_cmd.set_defaults(func=command_publish)

    verify_cmd = subparsers.add_parser("verify", help="校验已发布数据集的文件哈希")
    verify_cmd.add_argument("--lake", type=Path, required=True)
    verify_cmd.add_argument("--name", required=True)
    verify_cmd.add_argument("--dataset-version", required=True)
    verify_cmd.set_defaults(func=command_verify)

    trace_cmd = subparsers.add_parser("trace", help="追溯数据集版本与记录")
    trace_cmd.add_argument("--lake", type=Path, required=True)
    trace_cmd.add_argument("--name", required=True)
    trace_cmd.add_argument("--dataset-version", required=True)
    trace_cmd.add_argument("--run-id", default=None)
    trace_cmd.add_argument("--member", default=None, help="version/field_id")
    trace_cmd.set_defaults(func=command_trace)

    catalog_cmd = subparsers.add_parser("catalog", help="输出数据集账本")
    catalog_cmd.add_argument("--lake", type=Path, required=True)
    catalog_cmd.set_defaults(func=command_catalog)

    lineage_cmd = subparsers.add_parser("lineage", help="输出协议与映射血统")
    lineage_cmd.add_argument("--data", type=Path, required=True)
    _add_governance_options(lineage_cmd)
    lineage_cmd.set_defaults(func=command_lineage)

    rebuild_cmd = subparsers.add_parser("rebuild", help="按已发布参数重建为新版本")
    rebuild_cmd.add_argument("--data", type=Path, required=True)
    _add_governance_options(rebuild_cmd)
    rebuild_cmd.add_argument("--lake", type=Path, required=True)
    rebuild_cmd.add_argument("--name", required=True)
    rebuild_cmd.add_argument("--dataset-version", required=True, help="被重建的旧版本")
    rebuild_cmd.add_argument("--new-dataset-version", default=None)
    rebuild_cmd.set_defaults(func=command_rebuild)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LakeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
