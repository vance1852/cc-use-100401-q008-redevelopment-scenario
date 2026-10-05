"""确定性的二次开发方案经济评价。

全部计算使用 Decimal，结果按固定精度量化，保证同一快照与方案在任何机器、
任何时间重算都得到逐字节一致的结论。评价算法版本固化在 EVALUATION_VERSION，
方案评价结果同时记录该版本，使决策者可以追溯结论的计算口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .contracts import DevelopmentPlan, InputSnapshot


EVALUATION_VERSION = "1.0.0"

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
TEN_THOUSAND = Decimal("10000")
MONEY_Q = Decimal("0.01")
OIL_Q = Decimal("0.001")
FACTOR_Q = Decimal("0.0001")
YEAR_Q = Decimal("0.1")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _oil(value: Decimal) -> Decimal:
    return value.quantize(OIL_Q, rounding=ROUND_HALF_UP)


def _factor(value: Decimal) -> Decimal:
    return value.quantize(FACTOR_Q, rounding=ROUND_HALF_UP)


def _year_fraction(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(YEAR_Q, rounding=ROUND_HALF_UP)


def _text(value: Decimal, quantum: Decimal) -> str:
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")


def _add_years(day: date, years: int) -> date:
    """按日历年推进，2 月 29 日回落到 28 日。"""

    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def _overlap_days(start: date, end: date, window_start: date, window_end: date) -> int:
    lo = max(start, window_start)
    hi = min(end, window_end)
    return max(0, (hi - lo).days + 1)


def outage_factor_for_year(
    snapshot: InputSnapshot, period_year: int
) -> Decimal:
    """该评价年受停产窗口影响后的可用产能系数（窗口按天折减，可叠加）。"""

    base = date.fromisoformat(snapshot.start_date)
    year_start = _add_years(base, period_year - 1)
    year_end = _add_years(base, period_year)
    days_in_year = (year_end - year_start).days
    factor = ONE
    for window in snapshot.outage_windows:
        overlap = _overlap_days(
            year_start,
            year_end,
            date.fromisoformat(window.start_date),
            date.fromisoformat(window.end_date),
        )
        if overlap:
            loss = window.capacity_loss_percent / HUNDRED * Decimal(overlap) / Decimal(days_in_year)
            factor *= ONE - loss
    return max(ZERO, min(ONE, factor))


@dataclass(frozen=True, slots=True)
class YearCashflow:
    year: int
    gross_oil_10kt: Decimal
    outage_factor: Decimal
    effective_oil_10kt: Decimal
    water_cut_percent: Decimal
    capex_wan_yuan: Decimal
    price_usd_bbl: Decimal
    net_wan_yuan: Decimal
    discounted_wan_yuan: Decimal
    cumulative_discounted_wan_yuan: Decimal


def _capex_by_year(plan: DevelopmentPlan, snapshot: InputSnapshot) -> dict[int, Decimal]:
    """选定资本支出项按年归集，含各自不可预见费，再叠加方案超支假设。"""

    selected = set(plan.selected_capex_items)
    schedule: dict[int, Decimal] = {}
    for item in snapshot.capex:
        if item.item_id in selected:
            loaded = item.amount_wan_yuan * (ONE + item.contingency_percent / HUNDRED)
            schedule[item.year] = schedule.get(item.year, ZERO) + loaded
    overrun = ONE + plan.capex_overrun_percent / HUNDRED
    return {year: amount * overrun for year, amount in schedule.items()}


def _price_for_year(base_price: Decimal, escalation_percent: Decimal, year: int) -> Decimal:
    return base_price * (ONE + escalation_percent / HUNDRED) ** (year - 1)


def yearly_cashflows(
    plan: DevelopmentPlan,
    snapshot: InputSnapshot,
    price: Decimal,
) -> list[YearCashflow]:
    price_assumption = snapshot.oil_price
    fx = price_assumption.exchange_rate_cny_usd
    barrels_per_tonne = price_assumption.barrels_per_tonne
    discount = ONE + plan.discount_rate_percent / HUNDRED
    capex_schedule = _capex_by_year(plan, snapshot)
    rows: list[YearCashflow] = []
    cumulative = ZERO
    for index, gross_oil in enumerate(plan.incremental_oil_10kt_yearly, start=1):
        outage = outage_factor_for_year(snapshot, index)
        effective_oil = gross_oil * outage
        water_cut = snapshot.water_cut.yearly_percent[index - 1]
        barrels = effective_oil * TEN_THOUSAND * barrels_per_tonne
        wellhead_price = _price_for_year(price, price_assumption.escalation_percent, index)
        revenue_cny = barrels * wellhead_price * fx
        lifting_cny = barrels * plan.opex_usd_bbl * (ONE + water_cut / HUNDRED) * fx
        capex_wan = capex_schedule.get(index, ZERO)
        net_wan = (revenue_cny - lifting_cny) / TEN_THOUSAND - capex_wan
        discounted = net_wan / discount ** index
        cumulative += discounted
        rows.append(
            YearCashflow(
                year=index,
                gross_oil_10kt=_oil(gross_oil),
                outage_factor=_factor(outage),
                effective_oil_10kt=_oil(effective_oil),
                water_cut_percent=water_cut,
                capex_wan_yuan=_money(capex_wan),
                price_usd_bbl=_money(wellhead_price),
                net_wan_yuan=_money(net_wan),
                discounted_wan_yuan=_money(discounted),
                cumulative_discounted_wan_yuan=_money(cumulative),
            )
        )
    return rows


def npv(rows: Sequence[YearCashflow]) -> Decimal:
    return _money(sum((row.discounted_wan_yuan for row in rows), ZERO))


def _payback(rows: Sequence[YearCashflow], discounted: bool) -> Decimal | None:
    """线性插值的回收年；累计始终为负时返回 None。"""

    cumulative = ZERO
    previous = ZERO
    for row in rows:
        current = row.discounted_wan_yuan if discounted else row.net_wan_yuan
        cumulative_prev = previous
        cumulative = previous + current
        if cumulative >= ZERO and cumulative_prev < ZERO:
            fraction = (-cumulative_prev) / current if current != ZERO else ZERO
            return Decimal(row.year - 1) + fraction
        previous = cumulative
    return None


def _npv_at_rate(rows_net: Sequence[tuple[int, Decimal]], rate: Decimal) -> Decimal:
    return sum((net / (ONE + rate) ** year for year, net in rows_net), ZERO)


def irr(rows: Sequence[YearCashflow]) -> Decimal | None:
    """在 -99% 到 500% 之间二分求内部收益率；无符号变化返回 None。"""

    nets = [(row.year, row.net_wan_yuan) for row in rows]
    low = Decimal("-0.99")
    high = Decimal("5")
    low_value = _npv_at_rate(nets, low)
    high_value = _npv_at_rate(nets, high)
    if low_value == ZERO:
        return _factor(low * HUNDRED)
    if low_value * high_value > ZERO:
        return None
    for _ in range(100):
        middle = (low + high) / 2
        value = _npv_at_rate(nets, middle)
        if value == ZERO or (high - low) < Decimal("0.0000001"):
            break
        if value * low_value < ZERO:
            high = middle
        else:
            low = middle
            low_value = value
    return _factor(middle * HUNDRED)


def _scenario_summary(rows: Sequence[YearCashflow]) -> dict[str, str | None]:
    irr_value = irr(rows)
    return {
        "npv_wan_yuan": _text(npv(rows), MONEY_Q),
        "payback_discounted_years": None if _year_fraction(_payback(rows, True)) is None
        else format(_year_fraction(_payback(rows, True)), "f"),
        "payback_simple_years": None if _year_fraction(_payback(rows, False)) is None
        else format(_year_fraction(_payback(rows, False)), "f"),
        "irr_percent": None if irr_value is None else format(irr_value, "f"),
    }


def _breakeven_price(
    plan: DevelopmentPlan, snapshot: InputSnapshot
) -> Decimal | None:
    """求使基准 NPV 恰好为零的恒定起始油价（美元/桶）。"""

    price_assumption = snapshot.oil_price
    low = Decimal("0.01")
    high = Decimal("10000")
    low_rows = yearly_cashflows(plan, snapshot, low)
    high_rows = yearly_cashflows(plan, snapshot, high)
    if npv(low_rows) * npv(high_rows) >= ZERO:
        return None
    for _ in range(100):
        middle = (low + high) / 2
        value = npv(yearly_cashflows(plan, snapshot, middle))
        if value == ZERO or (high - low) < Decimal("0.0000001"):
            return _money(middle)
        if value < ZERO:
            low = middle
        else:
            high = middle
    return _money((low + high) / 2)


def evaluate(plan: DevelopmentPlan, snapshot: InputSnapshot) -> dict[str, Any]:
    """执行完整评价，返回可持久化、可逐字节复现的结果。"""

    if plan.incremental_oil_10kt_yearly and snapshot.horizon_years != len(plan.incremental_oil_10kt_yearly):
        raise ValueError("方案年限必须与快照评价年限一致")
    price_assumption = snapshot.oil_price
    scenario_rows = {
        name: yearly_cashflows(plan, snapshot, price)
        for name, price in (
            ("low", price_assumption.low_usd_bbl),
            ("base", price_assumption.base_usd_bbl),
            ("high", price_assumption.high_usd_bbl),
        )
    }
    base_rows = scenario_rows["base"]
    band = price_assumption.sensitivity_band_percent / HUNDRED
    band_minus_rows = yearly_cashflows(
        plan, snapshot, price_assumption.base_usd_bbl * (ONE - band))
    band_plus_rows = yearly_cashflows(
        plan, snapshot, price_assumption.base_usd_bbl * (ONE + band))
    breakeven = _breakeven_price(plan, snapshot)

    total_oil = sum((row.effective_oil_10kt for row in base_rows), ZERO)
    gross_oil = sum((row.gross_oil_10kt for row in base_rows), ZERO)
    peak = max((row.effective_oil_10kt for row in base_rows), default=ZERO)
    bottlenecks = tuple(
        item.facility_id for item in snapshot.facility_constraints if item.is_bottleneck
    )
    return {
        "evaluation_version": EVALUATION_VERSION,
        "horizon_years": snapshot.horizon_years,
        "totals": {
            "gross_incremental_oil_10kt": _text(_oil(gross_oil), OIL_Q),
            "effective_incremental_oil_10kt": _text(_oil(total_oil), OIL_Q),
            "peak_year_oil_10kt": _text(_oil(peak), OIL_Q),
            "first_year_oil_10kt": _text(base_rows[0].effective_oil_10kt, OIL_Q),
            "outage_loss_oil_10kt": _text(_oil(gross_oil - total_oil), OIL_Q),
            "bottleneck_facilities": list(bottlenecks),
            "risk_level": plan.risk.risk_level,
            "risk_score": format(plan.risk.risk_score, "f"),
            "risk_factors": list(plan.risk.factors),
        },
        "yearly_base": [
            {
                "year": row.year,
                "gross_oil_10kt": format(row.gross_oil_10kt, "f"),
                "outage_factor": format(row.outage_factor, "f"),
                "effective_oil_10kt": format(row.effective_oil_10kt, "f"),
                "water_cut_percent": format(row.water_cut_percent, "f"),
                "capex_wan_yuan": format(row.capex_wan_yuan, "f"),
                "price_usd_bbl": format(row.price_usd_bbl, "f"),
                "net_wan_yuan": format(row.net_wan_yuan, "f"),
                "discounted_wan_yuan": format(row.discounted_wan_yuan, "f"),
                "cumulative_discounted_wan_yuan": format(row.cumulative_discounted_wan_yuan, "f"),
            }
            for row in base_rows
        ],
        "price_scenarios": {
            name: _scenario_summary(rows) for name, rows in scenario_rows.items()
        },
        "sensitivity": {
            "band_percent": format(price_assumption.sensitivity_band_percent, "f"),
            "npv_band_minus_wan_yuan": _text(npv(band_minus_rows), MONEY_Q),
            "npv_band_plus_wan_yuan": _text(npv(band_plus_rows), MONEY_Q),
            "breakeven_price_usd_bbl": None if breakeven is None else format(breakeven, "f"),
        },
    }
