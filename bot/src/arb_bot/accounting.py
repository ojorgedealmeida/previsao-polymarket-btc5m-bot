from __future__ import annotations

import os
from decimal import Decimal, ROUND_DOWN
from typing import Any

from .core import ONE, ZERO, dec


def parse_polymarket_buy_result(result: Any, fallback_shares: Decimal, fallback_cost: Decimal) -> tuple[Decimal, Decimal]:
    if not isinstance(result, dict):
        return fallback_shares, fallback_cost
    return dec(result.get("takingAmount", fallback_shares)), dec(result.get("makingAmount", fallback_cost))


def hedge_result_overfilled(actual_shares: Decimal, requested_shares: Decimal, order_shares: Decimal) -> bool:
    max_ratio = dec(os.getenv("POLYMARKET_MAX_HEDGE_OVERFILL_RATIO", "1.05"))
    intended_shares = max(requested_shares, order_shares)
    return intended_shares > ZERO and actual_shares > (intended_shares * max_ratio)


def resolve_operation_final_pnl(
    row: dict[str, Any],
    actual_hedge_shares: Decimal | None = None,
    actual_hedge_cost: Decimal | None = None,
) -> dict[str, Any]:
    result_selection_id = row.get("result_selection_id")
    selection_id = row.get("selection_id")
    if result_selection_id is None or selection_id is None:
        return {}
    shares = dec(row.get("shares", "0"))
    previsao_cost = dec(row.get("previsao_cost", "0"))
    actual_hedge_shares = actual_hedge_shares if actual_hedge_shares is not None else dec(row.get("actual_hedge_shares", "0"))
    actual_hedge_cost = actual_hedge_cost if actual_hedge_cost is not None else dec(row.get("actual_hedge_cost", "0"))
    previsao_won = str(selection_id) == str(result_selection_id)
    previsao_leg = (shares if previsao_won else ZERO) - previsao_cost
    hedge_payout_shares = ZERO if previsao_won else actual_hedge_shares
    capped_hedge_payout_shares = ZERO if previsao_won else min(shares, actual_hedge_shares)
    hedge_leg_uncapped = hedge_payout_shares - actual_hedge_cost
    hedge_leg = capped_hedge_payout_shares - actual_hedge_cost
    final_pnl = previsao_leg + hedge_leg
    final_pnl_uncapped = previsao_leg + hedge_leg_uncapped
    return {
        "result_selection_id": str(result_selection_id),
        "previsao_won": previsao_won,
        "previsao_leg_pnl": str(previsao_leg.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "polymarket_leg_pnl": str(hedge_leg.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "polymarket_leg_pnl_uncapped": str(hedge_leg_uncapped.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "gross_profit_final": str(final_pnl.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "gross_profit_final_uncapped": str(final_pnl_uncapped.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "extra_hedge_final_pnl": str((final_pnl_uncapped - final_pnl).quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "final_status": "resolved",
    }
