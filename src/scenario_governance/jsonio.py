"""确定性的规范化 JSON 与内容摘要。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Iterable


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"不能序列化 {type(value).__name__}")


def canonical_json(value: object) -> str:
    """生成跨平台一致的紧凑 JSON 文本，禁止 NaN/Infinity。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def parse_json(text: str, *, path: str = "内容") -> Any:
    """解析 JSON 文本，拒绝重复键与非标准常量。"""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{path} 含重复键 {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"{path} 不允许非有限数值 {value}")

    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=pairs_hook)


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def text_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
