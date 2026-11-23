"""通用工具：确定性 CSV/JSON、哈希、数值与版本处理。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .errors import SourceFormatError


_VERSION_RE = re.compile(r"^v(\d+)$")


def version_key(version: str) -> tuple[int, object]:
    """返回可排序的版本键：``v2`` < ``v10``；非 ``vN`` 形式按字符串排序。"""

    match = _VERSION_RE.match(version)
    if match:
        return (0, int(match.group(1)))
    return (1, version)


def sorted_versions(versions: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(versions), key=version_key))


def parse_iso_date(value: str, field: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SourceFormatError(f"{field} 不是 ISO 日期: {value!r}") from exc


def parse_iso_datetime(value: str, field: str = "datetime") -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SourceFormatError(f"{field} 不是 ISO 时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise SourceFormatError(f"{field} 必须包含时区: {value!r}")
    return parsed


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def try_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None


def format_decimal(value: Decimal) -> str:
    """稳定的十进制文本：不使用科学计数法，整数值不带小数点。"""

    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    return format(value.normalize(), "f")


def read_contract_csv(
    path: Path, expected_columns: Sequence[str]
) -> list[tuple[int, dict[str, str]]]:
    """按资料契约读取 CSV，返回 (行号, 行) 列表，并做基础格式校验。"""

    if not path.is_file():
        raise SourceFormatError(f"缺少输入文件: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(expected_columns):
            raise SourceFormatError(
                f"{path.name} 列名不符: 期望 {list(expected_columns)}，实际 {reader.fieldnames}"
            )
        rows: list[tuple[int, dict[str, str]]] = []
        for line_no, row in enumerate(reader, start=2):
            if None in row:
                raise SourceFormatError(f"{path.name} 第 {line_no} 行存在多余列")
            if any(value is None for value in row.values()):
                raise SourceFormatError(f"{path.name} 第 {line_no} 行列数不足")
            rows.append((line_no, {key: value for key, value in row.items()}))
    return rows


def require_text(row: Mapping[str, str], key: str, *, source: str, line_no: int) -> str:
    value = row[key].strip()
    if not value:
        raise SourceFormatError(f"{source} 第 {line_no} 行字段 {key} 为空")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: object, *, indent: int | None = None) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ": ") if indent is not None else (",", ":"),
    )
    return (text + "\n").encode("utf-8")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    """写入确定性 CSV：``\\n`` 换行、UTF-8、无 BOM。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=list(columns),
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column, "")) for column in columns})


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def append_jsonl(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    entries: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SourceFormatError(f"{path.name} 第 {line_no} 行不是合法 JSON") from exc
            if not isinstance(item, dict):
                raise SourceFormatError(f"{path.name} 第 {line_no} 行不是 JSON 对象")
            entries.append(item)
    return entries
