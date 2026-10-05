"""二次开发情景治理的领域输入契约。

七类输入贡献（储量版本、井组响应、含水预测、设施瓶颈、停产窗口、资本支出、
油价假设）分别解析为规范化结构，配合内容摘要组成不可变输入快照。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .canonical import decimal_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

CATEGORIES = (
    "reserves_version",
    "well_group_response",
    "water_cut_forecast",
    "facility_bottleneck",
    "shutdown_window",
    "capex",
    "oil_price_assumption",
)

CATEGORY_LABELS = {
    "reserves_version": "储量版本",
    "well_group_response": "井组响应",
    "water_cut_forecast": "含水预测",
    "facility_bottleneck": "设施瓶颈",
    "shutdown_window": "停产窗口",
    "capex": "资本支出",
    "oil_price_assumption": "油价假设",
}

ZERO = Decimal("0")
HUNDRED = Decimal("100")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def integer_value(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailed(f"{field} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValidationFailed(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{field} 必须是对象")
    return value


class CanonicalContribution:
    """所有输入贡献解析结果的共同接口。"""

    def as_canonical(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ReservesVersion(CanonicalContribution):
    """储量版本：剩余可采储量、现状年产油与自然递减率。"""

    remaining_reserves_kl: Decimal
    baseline_annual_oil_kl: Decimal
    natural_decline_percent: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReservesVersion":
        return cls(
            remaining_reserves_kl=decimal_value(
                raw.get("remaining_reserves_kl"), "remaining_reserves_kl", minimum=Decimal("0.001")
            ),
            baseline_annual_oil_kl=decimal_value(
                raw.get("baseline_annual_oil_kl"), "baseline_annual_oil_kl", minimum=Decimal("0.001")
            ),
            natural_decline_percent=decimal_value(
                raw.get("natural_decline_percent"), "natural_decline_percent",
                minimum=ZERO, maximum=Decimal("50"),
            ),
        )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "remaining_reserves_kl": decimal_text(self.remaining_reserves_kl),
            "baseline_annual_oil_kl": decimal_text(self.baseline_annual_oil_kl),
            "natural_decline_percent": decimal_text(self.natural_decline_percent),
        }


@dataclass(frozen=True, slots=True)
class WellGroup:
    well_group: str
    incremental_daily_oil_kl_per_well: Decimal
    response_confidence_percent: Decimal
    max_infill_wells: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WellGroup":
        return cls(
            well_group=identifier(raw.get("well_group"), "well_group"),
            incremental_daily_oil_kl_per_well=decimal_value(
                raw.get("incremental_daily_oil_kl_per_well"),
                "incremental_daily_oil_kl_per_well",
                minimum=Decimal("0.001"),
            ),
            response_confidence_percent=decimal_value(
                raw.get("response_confidence_percent"), "response_confidence_percent",
                minimum=ZERO, maximum=HUNDRED,
            ),
            max_infill_wells=integer_value(raw.get("max_infill_wells"), "max_infill_wells", minimum=1, maximum=99),
        )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "well_group": self.well_group,
            "incremental_daily_oil_kl_per_well": decimal_text(self.incremental_daily_oil_kl_per_well),
            "response_confidence_percent": decimal_text(self.response_confidence_percent),
            "max_infill_wells": self.max_infill_wells,
        }


@dataclass(frozen=True, slots=True)
class WellGroupResponse(CanonicalContribution):
    """井组响应：各井组单井增产能力、响应置信度与井数上限。"""

    groups: tuple[WellGroup, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WellGroupResponse":
        rows = raw.get("groups")
        if not isinstance(rows, (list, tuple)) or not rows:
            raise ValidationFailed("groups 必须是非空数组")
        groups = tuple(WellGroup.from_dict(_mapping(item, "groups 元素")) for item in rows)
        names = [group.well_group for group in groups]
        if len(set(names)) != len(names):
            raise ValidationFailed("井组编号不能重复")
        return cls(groups=tuple(sorted(groups, key=lambda item: item.well_group)))

    def as_canonical(self) -> dict[str, Any]:
        return {"groups": [group.as_canonical() for group in self.groups]}


@dataclass(frozen=True, slots=True)
class WaterCutForecast(CanonicalContribution):
    """含水预测：当前含水、年度上升幅度与上限。"""

    current_water_cut_percent: Decimal
    annual_increase_points: Decimal
    ceiling_percent: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WaterCutForecast":
        current = decimal_value(
            raw.get("current_water_cut_percent"), "current_water_cut_percent", minimum=ZERO, maximum=HUNDRED
        )
        ceiling = decimal_value(
            raw.get("ceiling_percent"), "ceiling_percent", minimum=Decimal("1"), maximum=HUNDRED
        )
        if ceiling < current:
            raise ValidationFailed("ceiling_percent 不能小于当前含水")
        return cls(
            current_water_cut_percent=current,
            annual_increase_points=decimal_value(
                raw.get("annual_increase_points"), "annual_increase_points",
                minimum=ZERO, maximum=Decimal("10"),
            ),
            ceiling_percent=ceiling,
        )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "current_water_cut_percent": decimal_text(self.current_water_cut_percent),
            "annual_increase_points": decimal_text(self.annual_increase_points),
            "ceiling_percent": decimal_text(self.ceiling_percent),
        }


@dataclass(frozen=True, slots=True)
class FacilityBottleneck(CanonicalContribution):
    """设施瓶颈：液处理、水处理与油处理日能力。"""

    liquid_capacity_kl_per_day: Decimal
    water_treatment_kl_per_day: Decimal
    oil_processing_kl_per_day: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FacilityBottleneck":
        return cls(
            liquid_capacity_kl_per_day=decimal_value(
                raw.get("liquid_capacity_kl_per_day"), "liquid_capacity_kl_per_day", minimum=Decimal("0.001")
            ),
            water_treatment_kl_per_day=decimal_value(
                raw.get("water_treatment_kl_per_day"), "water_treatment_kl_per_day", minimum=Decimal("0.001")
            ),
            oil_processing_kl_per_day=decimal_value(
                raw.get("oil_processing_kl_per_day"), "oil_processing_kl_per_day", minimum=Decimal("0.001")
            ),
        )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "liquid_capacity_kl_per_day": decimal_text(self.liquid_capacity_kl_per_day),
            "water_treatment_kl_per_day": decimal_text(self.water_treatment_kl_per_day),
            "oil_processing_kl_per_day": decimal_text(self.oil_processing_kl_per_day),
        }


@dataclass(frozen=True, slots=True)
class ShutdownWindow(CanonicalContribution):
    """停产窗口：年度计划停产天数与具体窗口明细。"""

    planned_shutdown_days_per_year: int
    windows: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ShutdownWindow":
        days = integer_value(
            raw.get("planned_shutdown_days_per_year"), "planned_shutdown_days_per_year",
            minimum=0, maximum=120,
        )
        raw_windows = raw.get("windows", [])
        if not isinstance(raw_windows, (list, tuple)):
            raise ValidationFailed("windows 必须是数组")
        windows: list[dict[str, Any]] = []
        for item in raw_windows:
            row = _mapping(item, "windows 元素")
            start = date_text(row.get("start_date"), "windows.start_date")
            end = date_text(row.get("end_date"), "windows.end_date")
            if end < start:
                raise ValidationFailed("停产窗口结束日期不能早于开始日期")
            windows.append({
                "start_date": start,
                "end_date": end,
                "reason": required_text(row.get("reason"), "windows.reason", 128),
            })
        return cls(
            planned_shutdown_days_per_year=days,
            windows=tuple(sorted(windows, key=lambda item: (item["start_date"], item["end_date"]))),
        )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "planned_shutdown_days_per_year": self.planned_shutdown_days_per_year,
            "windows": [dict(window) for window in self.windows],
        }


@dataclass(frozen=True, slots=True)
class CapexAssumption(CanonicalContribution):
    """资本支出：单井造价、设施改造费用与不可预见费比例。"""

    well_cost_m_cny_per_well: Decimal
    facility_upgrade_cost_m_cny: Decimal
    contingency_percent: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapexAssumption":
        return cls(
            well_cost_m_cny_per_well=decimal_value(
                raw.get("well_cost_m_cny_per_well"), "well_cost_m_cny_per_well", minimum=Decimal("0.001")
            ),
            facility_upgrade_cost_m_cny=decimal_value(
                raw.get("facility_upgrade_cost_m_cny"), "facility_upgrade_cost_m_cny", minimum=ZERO
            ),
            contingency_percent=decimal_value(
                raw.get("contingency_percent"), "contingency_percent", minimum=ZERO, maximum=HUNDRED
            ),
        )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "well_cost_m_cny_per_well": decimal_text(self.well_cost_m_cny_per_well),
            "facility_upgrade_cost_m_cny": decimal_text(self.facility_upgrade_cost_m_cny),
            "contingency_percent": decimal_text(self.contingency_percent),
        }


@dataclass(frozen=True, slots=True)
class OilPriceAssumption(CanonicalContribution):
    """油价假设：基准油价、年递增、液量可变成本与折现率。"""

    base_price_cny_per_kl: Decimal
    annual_escalation_percent: Decimal
    variable_cost_cny_per_kl_liquid: Decimal
    discount_rate_percent: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OilPriceAssumption":
        return cls(
            base_price_cny_per_kl=decimal_value(
                raw.get("base_price_cny_per_kl"), "base_price_cny_per_kl", minimum=Decimal("0.01")
            ),
            annual_escalation_percent=decimal_value(
                raw.get("annual_escalation_percent"), "annual_escalation_percent",
                minimum=Decimal("-20"), maximum=Decimal("20"),
            ),
            variable_cost_cny_per_kl_liquid=decimal_value(
                raw.get("variable_cost_cny_per_kl_liquid"), "variable_cost_cny_per_kl_liquid", minimum=ZERO
            ),
            discount_rate_percent=decimal_value(
                raw.get("discount_rate_percent"), "discount_rate_percent",
                minimum=ZERO, maximum=Decimal("30"),
            ),
        )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "base_price_cny_per_kl": decimal_text(self.base_price_cny_per_kl),
            "annual_escalation_percent": decimal_text(self.annual_escalation_percent),
            "variable_cost_cny_per_kl_liquid": decimal_text(self.variable_cost_cny_per_kl_liquid),
            "discount_rate_percent": decimal_text(self.discount_rate_percent),
        }


_PARSERS = {
    "reserves_version": ReservesVersion.from_dict,
    "well_group_response": WellGroupResponse.from_dict,
    "water_cut_forecast": WaterCutForecast.from_dict,
    "facility_bottleneck": FacilityBottleneck.from_dict,
    "shutdown_window": ShutdownWindow.from_dict,
    "capex": CapexAssumption.from_dict,
    "oil_price_assumption": OilPriceAssumption.from_dict,
}


def parse_contribution(category: str, raw: object) -> CanonicalContribution:
    """按类别解析输入贡献，未知类别或结构不合法即拒绝。"""

    if category not in _PARSERS:
        raise ValidationFailed(f"未知输入贡献类别: {category}")
    return _PARSERS[category](_mapping(raw, f"{category} 内容"))


@dataclass(frozen=True, slots=True)
class InfillPlan:
    well_group: str
    count: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InfillPlan":
        return cls(
            well_group=identifier(raw.get("well_group"), "infill_wells.well_group"),
            count=integer_value(raw.get("count"), "infill_wells.count", minimum=1, maximum=99),
        )

    def as_canonical(self) -> dict[str, Any]:
        return {"well_group": self.well_group, "count": self.count}


@dataclass(frozen=True, slots=True)
class FacilityUpgrade:
    additional_liquid_kl_per_day: Decimal
    additional_water_treatment_kl_per_day: Decimal
    additional_oil_processing_kl_per_day: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FacilityUpgrade":
        upgrade = cls(
            additional_liquid_kl_per_day=decimal_value(
                raw.get("additional_liquid_kl_per_day"), "additional_liquid_kl_per_day", minimum=ZERO
            ),
            additional_water_treatment_kl_per_day=decimal_value(
                raw.get("additional_water_treatment_kl_per_day"),
                "additional_water_treatment_kl_per_day",
                minimum=ZERO,
            ),
            additional_oil_processing_kl_per_day=decimal_value(
                raw.get("additional_oil_processing_kl_per_day"),
                "additional_oil_processing_kl_per_day",
                minimum=ZERO,
            ),
        )
        if (
            upgrade.additional_liquid_kl_per_day == ZERO
            and upgrade.additional_water_treatment_kl_per_day == ZERO
            and upgrade.additional_oil_processing_kl_per_day == ZERO
        ):
            raise ValidationFailed("设施改造至少需要一项新增能力大于零")
        return upgrade

    def as_canonical(self) -> dict[str, Any]:
        return {
            "additional_liquid_kl_per_day": decimal_text(self.additional_liquid_kl_per_day),
            "additional_water_treatment_kl_per_day": decimal_text(self.additional_water_treatment_kl_per_day),
            "additional_oil_processing_kl_per_day": decimal_text(self.additional_oil_processing_kl_per_day),
        }


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    """二次开发方案定义：井网调整、设施改造与评价期。"""

    name: str
    infill_wells: tuple[InfillPlan, ...]
    facility_upgrade: FacilityUpgrade | None
    horizon_years: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScenarioDefinition":
        raw_wells = raw.get("infill_wells", [])
        if not isinstance(raw_wells, (list, tuple)):
            raise ValidationFailed("infill_wells 必须是数组")
        wells = tuple(InfillPlan.from_dict(_mapping(item, "infill_wells 元素")) for item in raw_wells)
        names = [plan.well_group for plan in wells]
        if len(set(names)) != len(names):
            raise ValidationFailed("方案中井组不能重复")
        raw_upgrade = raw.get("facility_upgrade")
        upgrade = None if raw_upgrade is None else FacilityUpgrade.from_dict(_mapping(raw_upgrade, "facility_upgrade"))
        definition = cls(
            name=required_text(raw.get("name"), "name", 128),
            infill_wells=tuple(sorted(wells, key=lambda item: item.well_group)),
            facility_upgrade=upgrade,
            horizon_years=integer_value(raw.get("horizon_years"), "horizon_years", minimum=1, maximum=30),
        )
        if not definition.infill_wells and definition.facility_upgrade is None:
            raise ValidationFailed("方案至少包含井网调整或设施改造之一")
        return definition

    def as_canonical(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "infill_wells": [plan.as_canonical() for plan in self.infill_wells],
            "facility_upgrade": None if self.facility_upgrade is None else self.facility_upgrade.as_canonical(),
            "horizon_years": self.horizon_years,
        }


@dataclass(frozen=True, slots=True)
class ActualMetrics:
    """投产后实绩：年产油、含水、累计资本支出与实际油价。"""

    actual_oil_kl: Decimal
    actual_water_cut_percent: Decimal
    actual_capex_to_date_m_cny: Decimal
    actual_oil_price_cny_per_kl: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ActualMetrics":
        return cls(
            actual_oil_kl=decimal_value(raw.get("actual_oil_kl"), "actual_oil_kl", minimum=ZERO),
            actual_water_cut_percent=decimal_value(
                raw.get("actual_water_cut_percent"), "actual_water_cut_percent", minimum=ZERO, maximum=HUNDRED
            ),
            actual_capex_to_date_m_cny=decimal_value(
                raw.get("actual_capex_to_date_m_cny"), "actual_capex_to_date_m_cny", minimum=ZERO
            ),
            actual_oil_price_cny_per_kl=decimal_value(
                raw.get("actual_oil_price_cny_per_kl"), "actual_oil_price_cny_per_kl", minimum=Decimal("0.01")
            ),
        )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "actual_oil_kl": decimal_text(self.actual_oil_kl),
            "actual_water_cut_percent": decimal_text(self.actual_water_cut_percent),
            "actual_capex_to_date_m_cny": decimal_text(self.actual_capex_to_date_m_cny),
            "actual_oil_price_cny_per_kl": decimal_text(self.actual_oil_price_cny_per_kl),
        }
