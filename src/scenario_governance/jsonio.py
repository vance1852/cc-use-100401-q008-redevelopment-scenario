"""确定性的 JSON 文件读取。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any


class JsonDataError(ValueError):
    """JSON 文件缺失、损坏或不符合契约。"""


def _reject_constant(value: str) -> None:
    raise JsonDataError(f"JSON 不允许非有限数值 {value}")


def load_json(path: Path) -> Any:
    """读取 UTF-8 JSON，并拒绝重复键和非标准常量。"""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise JsonDataError(f"{path} 含重复键 {key}")
            result[key] = value
        return result

    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=pairs_hook,
        )
    except OSError as exc:
        raise JsonDataError(f"无法读取 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise JsonDataError(f"{path} 不是有效 JSON: {exc.msg}") from exc
