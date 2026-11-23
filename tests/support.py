"""测试辅助：把 ``src`` 加入导入路径，并提供共享夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DATA = ROOT / "data"
FIXTURES = ROOT / "fixtures"


def governance_paths(directory: Path) -> dict[str, Path]:
    """返回夹具目录中的可选治理文件路径（不存在则不返回）。"""

    mapping = {
        "reviews_path": directory / "semantic_reviews.csv",
        "conversions_path": directory / "unit_conversions.csv",
        "adjudications_path": directory / "mapping_adjudications.csv",
    }
    return {key: path for key, path in mapping.items() if path.is_file()}
