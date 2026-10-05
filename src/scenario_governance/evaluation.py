"""二次开发方案的确定性评价：增产、风险、回收期与敏感性边界。

全部计算使用十进制定点运算并在关键中间结果上量化，保证同一输入快照与
方案定义在任何机器上得到逐字节一致的结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .canonical import decimal_text, quantize
from .models import (
    CapexAssumption,
    FacilityBottleneck,
    OilPriceAssumption,
    ReservesVersion,
    ScenarioDefinition,
    ShutdownWindow,
    WaterCutForecast,
    WellGroupResponse,
)


ALGORITHM_VERSION = "redevelopment-eval-v1"

# 敏感性扰动幅度：油价 ±20%、井组响应 -20%、资本支出 +20%。
PERTURBATION_PERCENT = Decimal("20")
# 实绩偏差判定含水是否突破边界的允许点数。
WATER_CUT_BOUNDARY_POINTS = Decimal("5")

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
DAYS_PER_YEAR = Decimal("365")
MILLION = Decimal("1000000")

Q_OIL = "0.001"
Q_DAILY = "0.000001"
Q_MONEY = "0.0001"
Q_PERCENT = "0.0001"
Q_PRICE = "0.01"
Q_YEARS = "0.01"
Q_SCORE = "0.1"


@dataclass(frozen=True, slots=True)
class EvalInputs:
    """从输入快照解析出的七类假设。"""

    reserves: ReservesVersion
    well_groups: WellGroupResponse
    water_cut: WaterCutForecast
    facility: FacilityBottleneck
    shutdown: ShutdownWindow
    capex: CapexAssumption
    oil_price: OilPriceAssumption


def _capex_total(inputs: EvalInputs, definition: ScenarioDefinition, capex_multiplier: Decimal) -> Decimal:
    wells = sum(plan.count for plan in definition.infill_wells)
    base = inputs.capex.well_cost_m_cny_per_well * Decimal(wells)
    if definition.facility_upgrade is not None:
        base += inputs.capex.facility_upgrade_cost_m_cny
    return quantize(base * capex_multiplier * (ONE + inputs.capex.contingency_percent / HUNDRED), Q_MONEY)


def _project(
    inputs: EvalInputs,
    definition: ScenarioDefinition,
    *,
    price_multiplier: Decimal = ONE,
    response_multiplier: Decimal = ONE,
    capex_multiplier: Decimal = ONE,
) -> dict[str, Any]:
    """按年推演产量、增量现金流与回收期，返回内部 Decimal 结果。

    经济口径为增量法：净现金流 = 增产量 × 油价 − 增量液量 × 可变成本，
    回收期与 NPV 衡量增量现金流对资本支出的回收，避免全油田现金流
    掩盖零增产方案的真实投资价值。
    """

    horizon = definition.horizon_years
    upgrade = definition.facility_upgrade
    extra_liquid = ZERO if upgrade is None else upgrade.additional_liquid_kl_per_day
    extra_water = ZERO if upgrade is None else upgrade.additional_water_treatment_kl_per_day
    extra_oil = ZERO if upgrade is None else upgrade.additional_oil_processing_kl_per_day
    liquid_cap = inputs.facility.liquid_capacity_kl_per_day + extra_liquid
    water_cap = inputs.facility.water_treatment_kl_per_day + extra_water
    oil_cap = inputs.facility.oil_processing_kl_per_day + extra_oil
    base_liquid_cap = inputs.facility.liquid_capacity_kl_per_day
    base_water_cap = inputs.facility.water_treatment_kl_per_day
    base_oil_cap = inputs.facility.oil_processing_kl_per_day

    incremental_daily_base = ZERO
    confidence_weighted = ZERO
    for plan in definition.infill_wells:
        group = next(item for item in inputs.well_groups.groups if item.well_group == plan.well_group)
        rate = group.incremental_daily_oil_kl_per_well * Decimal(plan.count)
        incremental_daily_base += rate
        confidence_weighted += rate * group.response_confidence_percent
    incremental_daily = quantize(incremental_daily_base * response_multiplier, Q_DAILY)

    capex_total = _capex_total(inputs, definition, capex_multiplier)
    days = Decimal(DAYS_PER_YEAR - Decimal(inputs.shutdown.planned_shutdown_days_per_year))
    decline_step = ONE - inputs.reserves.natural_decline_percent / HUNDRED
    escalation_step = ONE + inputs.oil_price.annual_escalation_percent / HUNDRED
    discount_step = ONE + inputs.oil_price.discount_rate_percent / HUNDRED

    decline_factor = ONE
    price_factor = ONE
    discount_factor = ONE
    cumulative_cash = -capex_total
    npv = -capex_total
    discounted_revenue = ZERO
    discounted_opex = ZERO
    cumulative_oil = ZERO
    cumulative_baseline = ZERO
    cumulative_uplift = ZERO
    cumulative_revenue = ZERO
    payback_years: Decimal | None = Decimal("0") if capex_total <= ZERO else None
    max_liquid_utilization = ZERO
    annual: list[dict[str, Any]] = []

    for year in range(1, horizon + 1):
        water_cut = min(
            inputs.water_cut.ceiling_percent,
            inputs.water_cut.current_water_cut_percent
            + inputs.water_cut.annual_increase_points * Decimal(year - 1),
        )
        water_cut = quantize(water_cut, Q_PERCENT)
        water_fraction = water_cut / HUNDRED
        oil_fraction = ONE - water_fraction

        baseline_daily = quantize(
            inputs.reserves.baseline_annual_oil_kl / DAYS_PER_YEAR * decline_factor, Q_DAILY
        )
        nominal_daily = baseline_daily + incremental_daily

        caps = {
            "oil_processing": oil_cap,
            "liquid_handling": quantize(liquid_cap * oil_fraction, Q_DAILY),
            "water_treatment": (
                quantize(water_cap * oil_fraction / water_fraction, Q_DAILY)
                if water_fraction > ZERO else nominal_daily
            ),
        }
        constrained_daily = max(ZERO, min(nominal_daily, *caps.values()))
        binding = "none"
        if constrained_daily < nominal_daily:
            binding = min(caps, key=lambda name: caps[name])

        baseline_caps = (
            base_oil_cap,
            quantize(base_liquid_cap * oil_fraction, Q_DAILY),
            (
                quantize(base_water_cap * oil_fraction / water_fraction, Q_DAILY)
                if water_fraction > ZERO else baseline_daily
            ),
        )
        baseline_constrained = max(ZERO, min(baseline_daily, *baseline_caps))

        oil_year = quantize(constrained_daily * days, Q_OIL)
        baseline_year = quantize(baseline_constrained * days, Q_OIL)
        uplift_year = quantize(oil_year - baseline_year, Q_OIL)
        liquid_year = quantize(oil_year / oil_fraction, Q_OIL) if oil_fraction > ZERO else oil_year
        baseline_liquid_year = (
            quantize(baseline_year / oil_fraction, Q_OIL) if oil_fraction > ZERO else baseline_year
        )
        incremental_liquid_year = quantize(liquid_year - baseline_liquid_year, Q_OIL)
        utilization = constrained_daily / oil_fraction / liquid_cap if liquid_cap > ZERO and oil_fraction > ZERO else ONE
        max_liquid_utilization = max(max_liquid_utilization, utilization)

        price = quantize(inputs.oil_price.base_price_cny_per_kl * price_factor * price_multiplier, Q_PRICE)
        revenue = quantize(uplift_year * price / MILLION, Q_MONEY)
        opex = quantize(
            incremental_liquid_year * inputs.oil_price.variable_cost_cny_per_kl_liquid / MILLION, Q_MONEY
        )
        net_cash = quantize(revenue - opex, Q_MONEY)
        previous_cumulative = cumulative_cash
        cumulative_cash = quantize(cumulative_cash + net_cash, Q_MONEY)
        discount_factor *= discount_step
        npv = quantize(npv + net_cash / discount_factor, Q_MONEY)
        discounted_revenue = quantize(discounted_revenue + revenue / discount_factor, Q_MONEY)
        discounted_opex = quantize(discounted_opex + opex / discount_factor, Q_MONEY)

        if payback_years is None and cumulative_cash >= ZERO and net_cash > ZERO:
            shortfall = -previous_cumulative
            payback_years = quantize(Decimal(year - 1) + shortfall / net_cash, Q_YEARS)

        cumulative_oil = quantize(cumulative_oil + oil_year, Q_OIL)
        cumulative_baseline = quantize(cumulative_baseline + baseline_year, Q_OIL)
        cumulative_uplift = quantize(cumulative_uplift + uplift_year, Q_OIL)
        cumulative_revenue = quantize(cumulative_revenue + revenue, Q_MONEY)
        annual.append({
            "year": year,
            "water_cut_percent": decimal_text(water_cut),
            "oil_kl": decimal_text(oil_year),
            "baseline_oil_kl": decimal_text(baseline_year),
            "uplift_kl": decimal_text(uplift_year),
            "price_cny_per_kl": decimal_text(price),
            "incremental_revenue_m_cny": decimal_text(revenue),
            "incremental_opex_m_cny": decimal_text(opex),
            "net_cash_flow_m_cny": decimal_text(net_cash),
            "cumulative_cash_flow_m_cny": decimal_text(cumulative_cash),
            "binding_constraint": binding,
        })

        decline_factor *= decline_step
        price_factor *= escalation_step

    return {
        "annual": annual,
        "capex_m_cny": capex_total,
        "cumulative_oil_kl": cumulative_oil,
        "cumulative_baseline_oil_kl": cumulative_baseline,
        "uplift_kl": cumulative_uplift,
        "cumulative_incremental_revenue_m_cny": cumulative_revenue,
        "npv_m_cny": npv,
        "payback_years": payback_years,
        "discounted_revenue_m_cny": discounted_revenue,
        "discounted_opex_m_cny": discounted_opex,
        "max_liquid_utilization": max_liquid_utilization,
        "weighted_confidence_percent": (
            confidence_weighted / incremental_daily_base if incremental_daily_base > ZERO else None
        ),
        "horizon_water_cut_percent": annual[-1]["water_cut_percent"],
    }


def _risk(inputs: EvalInputs, definition: ScenarioDefinition, base: dict[str, Any]) -> dict[str, Any]:
    water_cut_risk = quantize(
        Decimal(base["horizon_water_cut_percent"]) / inputs.water_cut.ceiling_percent * HUNDRED, Q_SCORE
    )
    utilization_risk = quantize(min(ONE, base["max_liquid_utilization"]) * HUNDRED, Q_SCORE)
    confidence = base["weighted_confidence_percent"]
    confidence_risk = ZERO if confidence is None else quantize(HUNDRED - confidence, Q_SCORE)
    revenue = base["cumulative_incremental_revenue_m_cny"]
    capex_intensity_risk = (
        HUNDRED if revenue <= ZERO else quantize(min(HUNDRED, base["capex_m_cny"] / revenue * HUNDRED), Q_SCORE)
    )
    score = quantize(
        Decimal("0.3") * water_cut_risk
        + Decimal("0.3") * utilization_risk
        + Decimal("0.2") * confidence_risk
        + Decimal("0.2") * capex_intensity_risk,
        Q_SCORE,
    )
    classification = "low" if score < Decimal("35") else ("medium" if score < Decimal("70") else "high")
    binding = [
        {"year": row["year"], "constraint": row["binding_constraint"]}
        for row in base["annual"]
        if row["binding_constraint"] != "none"
    ]
    return {
        "score": decimal_text(score),
        "classification": classification,
        "components": {
            "water_cut_risk": decimal_text(water_cut_risk),
            "facility_utilization_risk": decimal_text(utilization_risk),
            "response_confidence_risk": decimal_text(confidence_risk),
            "capex_intensity_risk": decimal_text(capex_intensity_risk),
        },
        "binding_constraints": binding,
    }


def _case_summary(projection: dict[str, Any]) -> dict[str, Any]:
    return {
        "npv_m_cny": decimal_text(projection["npv_m_cny"]),
        "payback_years": None if projection["payback_years"] is None else decimal_text(projection["payback_years"]),
        "cumulative_oil_kl": decimal_text(projection["cumulative_oil_kl"]),
        "uplift_kl": decimal_text(projection["uplift_kl"]),
    }


def evaluate(inputs: EvalInputs, definition: ScenarioDefinition) -> dict[str, Any]:
    """评价方案并输出规范化结果，包含敏感性边界。"""

    down = ONE - PERTURBATION_PERCENT / HUNDRED
    up = ONE + PERTURBATION_PERCENT / HUNDRED
    base = _project(inputs, definition)
    sensitivity_cases = {
        "oil_price_down_20": _case_summary(_project(inputs, definition, price_multiplier=down)),
        "oil_price_up_20": _case_summary(_project(inputs, definition, price_multiplier=up)),
        "response_down_20": _case_summary(_project(inputs, definition, response_multiplier=down)),
        "capex_up_20": _case_summary(_project(inputs, definition, capex_multiplier=up)),
    }
    worst_case = min(sensitivity_cases, key=lambda name: Decimal(sensitivity_cases[name]["npv_m_cny"]))
    break_even_price: Decimal | None = None
    if base["discounted_revenue_m_cny"] > ZERO:
        multiplier = (base["capex_m_cny"] + base["discounted_opex_m_cny"]) / base["discounted_revenue_m_cny"]
        break_even_price = quantize(inputs.oil_price.base_price_cny_per_kl * multiplier, Q_PRICE)
    baseline_total = base["cumulative_baseline_oil_kl"]
    uplift_percent = (
        ZERO if baseline_total <= ZERO else quantize(base["uplift_kl"] / baseline_total * HUNDRED, Q_PERCENT)
    )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "horizon_years": definition.horizon_years,
        "capex_m_cny": decimal_text(base["capex_m_cny"]),
        "cumulative_oil_kl": decimal_text(base["cumulative_oil_kl"]),
        "cumulative_baseline_oil_kl": decimal_text(baseline_total),
        "uplift_kl": decimal_text(base["uplift_kl"]),
        "uplift_percent": decimal_text(uplift_percent),
        "npv_m_cny": decimal_text(base["npv_m_cny"]),
        "payback_years": None if base["payback_years"] is None else decimal_text(base["payback_years"]),
        "payback_beyond_horizon": base["payback_years"] is None,
        "risk": _risk(inputs, definition, base),
        "reserves_check": {
            "remaining_reserves_kl": decimal_text(inputs.reserves.remaining_reserves_kl),
            "cumulative_oil_kl": decimal_text(base["cumulative_oil_kl"]),
            "within_reserves": base["cumulative_oil_kl"] <= inputs.reserves.remaining_reserves_kl,
        },
        "sensitivity": {
            "perturbation_percent": decimal_text(PERTURBATION_PERCENT),
            "cases": sensitivity_cases,
            "worst_case": worst_case,
            "break_even_oil_price_cny_per_kl": (
                None if break_even_price is None else decimal_text(break_even_price)
            ),
        },
        "annual": base["annual"],
    }


def deviation_variance(
    evaluation_result: dict[str, Any], horizon_year: int, metrics: dict[str, str]
) -> dict[str, Any]:
    """对照已批准方案的评价结果与敏感性边界，计算实绩偏差。"""

    annual = evaluation_result["annual"]
    if not 1 <= horizon_year <= len(annual):
        raise ValueError("实绩年份超出已批准方案评价期")
    projected = annual[horizon_year - 1]
    perturbation = Decimal(evaluation_result["sensitivity"]["perturbation_percent"])

    def ratio(actual: Decimal, plan: Decimal) -> Decimal:
        if plan == ZERO:
            return ZERO if actual == ZERO else HUNDRED
        return quantize((actual - plan) / plan * HUNDRED, Q_PERCENT)

    oil_actual = Decimal(metrics["actual_oil_kl"])
    oil_plan = Decimal(projected["oil_kl"])
    oil_variance = ratio(oil_actual, oil_plan)
    price_actual = Decimal(metrics["actual_oil_price_cny_per_kl"])
    price_plan = Decimal(projected["price_cny_per_kl"])
    price_variance = ratio(price_actual, price_plan)
    capex_actual = Decimal(metrics["actual_capex_to_date_m_cny"])
    capex_plan = Decimal(evaluation_result["capex_m_cny"])
    capex_variance = ratio(capex_actual, capex_plan)
    water_actual = Decimal(metrics["actual_water_cut_percent"])
    water_plan = Decimal(projected["water_cut_percent"])
    water_variance = quantize(water_actual - water_plan, Q_PERCENT)

    rows = {
        "oil_kl": {
            "projected": decimal_text(oil_plan),
            "actual": decimal_text(oil_actual),
            "variance_percent": decimal_text(oil_variance),
            "within_sensitivity_boundary": abs(oil_variance) <= perturbation,
        },
        "oil_price_cny_per_kl": {
            "projected": decimal_text(price_plan),
            "actual": decimal_text(price_actual),
            "variance_percent": decimal_text(price_variance),
            "within_sensitivity_boundary": abs(price_variance) <= perturbation,
        },
        "capex_to_date_m_cny": {
            "projected": decimal_text(capex_plan),
            "actual": decimal_text(capex_actual),
            "variance_percent": decimal_text(capex_variance),
            "within_sensitivity_boundary": capex_variance <= perturbation,
        },
        "water_cut_percent": {
            "projected": decimal_text(water_plan),
            "actual": decimal_text(water_actual),
            "variance_points": decimal_text(water_variance),
            "within_sensitivity_boundary": abs(water_variance) <= WATER_CUT_BOUNDARY_POINTS,
        },
    }
    return {
        "horizon_year": horizon_year,
        "metrics": rows,
        "beyond_sensitivity_boundary": any(
            not row["within_sensitivity_boundary"] for row in rows.values()
        ),
    }
