from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


ZERO = Decimal("0")
ONE = Decimal("1")
POLYMARKET_MIN_BUY_USDC = Decimal("1.01")


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class Book:
    bid: Level | None
    ask: Level | None
    min_order_size: Decimal = ZERO


@dataclass(frozen=True)
class Selection:
    venue: str
    market_id: str
    selection_id: str
    token_id: str
    label: str
    book: Book


def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value
