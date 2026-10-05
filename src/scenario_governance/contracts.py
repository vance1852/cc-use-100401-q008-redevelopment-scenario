"""二次开发情景治理的严格数据契约。

输入快照固定七类输入：储量版本、井组响应、含水预测、设施瓶颈、停产窗口、
资本支出、油价假设。三类专业团队分别在 attestations 中对各自小节负责。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

RESERVE_BASES = {"proved", "probable", "proved_probable", "management"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
RISK_LEVELS = {"high", "medium", "low"}
MARKET_INDEXES = {"BRENT", "DUBAI", "OMAN", "CONTRACT", "INTERNAL", "CUSTOM"}
CAPEX_CATEGORIES = {"drilling_completion", "facility_retrofit", "subsea_pipeline",
                    "power_system", "abandonment", "other"}

# 快照小节 -> 唯一有权背书的专业角色
SECTION_ATTESTATION_ROLES: Mapping[str, str] = {
    "reserves": "reservoir",
    "well_groups": "reservoir",
    "water_cut": "reservoir",
    "facility_constraints": "facility",
    "outage_windows": "facility",
    "capex": "finance",
    "oil_price": "finance",
}


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def _text(value: object, path: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationError(f"{path} 不能超过 {maximum} 个字符")
    return result


def _optional_text(value: object, path: str, maximum: int = 512) -> str | None:
    if value is None:
        return None
    return _text(value, path, maximum)


def _identifier(value: object, path: str) -> str:
    result = _text(value, path, 64)
    if not _IDENTIFIER.fullmatch(result):
        raise ValidationError(f"{path} 格式不正确")
    return result


def _decimal(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationError(f"{path} 必须是有限数值")
    return result


def _bounded(value: object, path: str, *, minimum: Decimal | None = None,
             maximum: Decimal | None = None) -> Decimal:
    result = _decimal(value, path)
    if minimum is not None and result < minimum:
        raise ValidationError(f"{path} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationError(f"{path} 不能大于 {maximum}")
    return result


def _nonneg_int(value: object, path: str, *, minimum: int = 0, maximum: int = 10_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(f"{path} 必须是 {minimum} 到 {maximum} 的整数")
    return value


def _date(value: object, path: str) -> str:
    text = _text(value, path, 10)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{path} 必须是 YYYY-MM-DD 日期") from exc


def _sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValidationError(f"{path} 必须是 64 位十六进制 SHA-256")
    return value.lower()


def _yearly_series(value: object, path: str, horizon: int) -> tuple[Decimal, ...]:
    items = _sequence(value, path)
    if len(items) != horizon:
        raise ValidationError(f"{path} 长度必须等于评价年限 {horizon}")
    return tuple(
        _bounded(item, f"{path}[{index}]", minimum=Decimal(0))
        for index, item in enumerate(items)
    )


@dataclass(frozen=True, slots=True)
class ReservesRevision:
    revision_id: str
    basis: str
    recoverable_oil_10kt: Decimal
    as_of_date: str
    content_sha256: str
    note: str | None

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "ReservesRevision":
        data = _mapping(raw, path)
        basis = _text(data.get("basis"), f"{path}.basis", 32)
        if basis not in RESERVE_BASES:
            raise ValidationError(f"{path}.basis 不受支持")
        return cls(
            revision_id=_text(data.get("revision_id"), f"{path}.revision_id", 64),
            basis=basis,
            recoverable_oil_10kt=_bounded(
                data.get("recoverable_oil_10kt"), f"{path}.recoverable_oil_10kt",
                minimum=Decimal("0.0001")),
            as_of_date=_date(data.get("as_of_date"), f"{path}.as_of_date"),
            content_sha256=_sha256(data.get("content_sha256"), f"{path}.content_sha256"),
            note=_optional_text(data.get("note"), f"{path}.note"),
        )


@dataclass(frozen=True, slots=True)
class WellGroupResponse:
    well_group_id: str
    name: str
    response_delay_months: int
    incremental_oil_10kt_yearly: tuple[Decimal, ...]
    decline_rate_percent: Decimal
    confidence: str

    @classmethod
    def from_dict(cls, raw: object, path: str, horizon: int) -> "WellGroupResponse":
        data = _mapping(raw, path)
        confidence = _text(data.get("confidence"), f"{path}.confidence", 16)
        if confidence not in CONFIDENCE_LEVELS:
            raise ValidationError(f"{path}.confidence 必须是 high、medium 或 low")
        return cls(
            well_group_id=_identifier(data.get("well_group_id"), f"{path}.well_group_id"),
            name=_text(data.get("name"), f"{path}.name"),
            response_delay_months=_nonneg_int(
                data.get("response_delay_months", 0), f"{path}.response_delay_months", maximum=600),
            incremental_oil_10kt_yearly=_yearly_series(
                data.get("incremental_oil_10kt_yearly"),
                f"{path}.incremental_oil_10kt_yearly", horizon),
            decline_rate_percent=_bounded(
                data.get("decline_rate_percent", 0), f"{path}.decline_rate_percent",
                minimum=Decimal(0), maximum=Decimal(100)),
            confidence=confidence,
        )


@dataclass(frozen=True, slots=True)
class WaterCutForecast:
    initial_percent: Decimal
    yearly_percent: tuple[Decimal, ...]
    limit_percent: Decimal

    @classmethod
    def from_dict(cls, raw: object, path: str, horizon: int) -> "WaterCutForecast":
        data = _mapping(raw, path)
        initial = _bounded(data.get("initial_percent"), f"{path}.initial_percent",
                           minimum=Decimal(0), maximum=Decimal(100))
        limit = _bounded(data.get("limit_percent"), f"{path}.limit_percent",
                         minimum=Decimal(0), maximum=Decimal(100))
        yearly = tuple(
            _bounded(item, f"{path}.yearly_percent[{index}]",
                     minimum=Decimal(0), maximum=Decimal(100))
            for index, item in enumerate(_sequence(data.get("yearly_percent"), f"{path}.yearly_percent"))
        )
        if len(yearly) != horizon:
            raise ValidationError(f"{path}.yearly_percent 长度必须等于评价年限 {horizon}")
        return cls(initial_percent=initial, yearly_percent=yearly, limit_percent=limit)


@dataclass(frozen=True, slots=True)
class FacilityConstraint:
    facility_id: str
    name: str
    system: str
    capacity: Decimal
    capacity_unit: str
    peak_demand_percent: Decimal
    debottleneck_capex_item: str | None

    @property
    def is_bottleneck(self) -> bool:
        return self.peak_demand_percent >= Decimal(100)

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "FacilityConstraint":
        data = _mapping(raw, path)
        return cls(
            facility_id=_identifier(data.get("facility_id"), f"{path}.facility_id"),
            name=_text(data.get("name"), f"{path}.name"),
            system=_text(data.get("system"), f"{path}.system", 64),
            capacity=_bounded(data.get("capacity"), f"{path}.capacity",
                              minimum=Decimal("0.0001")),
            capacity_unit=_text(data.get("capacity_unit"), f"{path}.capacity_unit", 32),
            peak_demand_percent=_bounded(
                data.get("peak_demand_percent"), f"{path}.peak_demand_percent",
                minimum=Decimal(0), maximum=Decimal(10_000)),
            debottleneck_capex_item=_optional_text(
                data.get("debottleneck_capex_item"), f"{path}.debottleneck_capex_item", 64),
        )


@dataclass(frozen=True, slots=True)
class OutageWindow:
    window_id: str
    facility_id: str
    reason: str
    start_date: str
    end_date: str
    capacity_loss_percent: Decimal

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "OutageWindow":
        data = _mapping(raw, path)
        start = _date(data.get("start_date"), f"{path}.start_date")
        end = _date(data.get("end_date"), f"{path}.end_date")
        if date.fromisoformat(end) < date.fromisoformat(start):
            raise ValidationError(f"{path}.end_date 不能早于 start_date")
        return cls(
            window_id=_identifier(data.get("window_id"), f"{path}.window_id"),
            facility_id=_identifier(data.get("facility_id"), f"{path}.facility_id"),
            reason=_text(data.get("reason"), f"{path}.reason"),
            start_date=start,
            end_date=end,
            capacity_loss_percent=_bounded(
                data.get("capacity_loss_percent"), f"{path}.capacity_loss_percent",
                minimum=Decimal(0), maximum=Decimal(100)),
        )


@dataclass(frozen=True, slots=True)
class CapexItem:
    item_id: str
    name: str
    category: str
    year: int
    amount_wan_yuan: Decimal
    contingency_percent: Decimal

    @classmethod
    def from_dict(cls, raw: object, path: str, horizon: int) -> "CapexItem":
        data = _mapping(raw, path)
        category = _text(data.get("category"), f"{path}.category", 32)
        if category not in CAPEX_CATEGORIES:
            raise ValidationError(f"{path}.category 不受支持")
        return cls(
            item_id=_identifier(data.get("item_id"), f"{path}.item_id"),
            name=_text(data.get("name"), f"{path}.name"),
            category=category,
            year=_nonneg_int(data.get("year"), f"{path}.year", minimum=1, maximum=max(1, horizon)),
            amount_wan_yuan=_bounded(data.get("amount_wan_yuan"), f"{path}.amount_wan_yuan",
                                     minimum=Decimal(0)),
            contingency_percent=_bounded(
                data.get("contingency_percent", 0), f"{path}.contingency_percent",
                minimum=Decimal(0), maximum=Decimal(1000)),
        )


@dataclass(frozen=True, slots=True)
class OilPriceAssumption:
    market_index: str
    base_usd_bbl: Decimal
    low_usd_bbl: Decimal
    high_usd_bbl: Decimal
    escalation_percent: Decimal
    exchange_rate_cny_usd: Decimal
    barrels_per_tonne: Decimal
    sensitivity_band_percent: Decimal

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "OilPriceAssumption":
        data = _mapping(raw, path)
        market_index = _text(data.get("market_index"), f"{path}.market_index", 16).upper()
        if market_index not in MARKET_INDEXES:
            raise ValidationError(f"{path}.market_index 不受支持")
        base = _bounded(data.get("base_usd_bbl"), f"{path}.base_usd_bbl",
                        minimum=Decimal("0.01"))
        low = _bounded(data.get("low_usd_bbl"), f"{path}.low_usd_bbl",
                       minimum=Decimal("0.01"))
        high = _bounded(data.get("high_usd_bbl"), f"{path}.high_usd_bbl",
                        minimum=Decimal("0.01"))
        if not low <= base <= high:
            raise ValidationError(f"{path} 必须满足 low <= base <= high")
        return cls(
            market_index=market_index,
            base_usd_bbl=base,
            low_usd_bbl=low,
            high_usd_bbl=high,
            escalation_percent=_bounded(
                data.get("escalation_percent", 0), f"{path}.escalation_percent",
                minimum=Decimal(-50), maximum=Decimal(50)),
            exchange_rate_cny_usd=_bounded(
                data.get("exchange_rate_cny_usd"), f"{path}.exchange_rate_cny_usd",
                minimum=Decimal("0.0001")),
            barrels_per_tonne=_bounded(
                data.get("barrels_per_tonne"), f"{path}.barrels_per_tonne",
                minimum=Decimal("0.01")),
            sensitivity_band_percent=_bounded(
                data.get("sensitivity_band_percent"), f"{path}.sensitivity_band_percent",
                minimum=Decimal(0), maximum=Decimal(100)),
        )


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    """七类输入组成的不可变快照。"""

    snapshot_id: str
    asset_id: str
    title: str
    horizon_years: int
    start_date: str
    reserves: ReservesRevision
    well_groups: tuple[WellGroupResponse, ...]
    water_cut: WaterCutForecast
    facility_constraints: tuple[FacilityConstraint, ...]
    outage_windows: tuple[OutageWindow, ...]
    capex: tuple[CapexItem, ...]
    oil_price: OilPriceAssumption
    attestations: Mapping[str, str]

    @classmethod
    def from_dict(cls, raw: object) -> "InputSnapshot":
        data = _mapping(raw, "snapshot")
        horizon = _nonneg_int(data.get("horizon_years"), "snapshot.horizon_years",
                              minimum=1, maximum=30)
        well_groups = tuple(
            WellGroupResponse.from_dict(item, f"snapshot.well_groups[{index}]", horizon)
            for index, item in enumerate(_sequence(data.get("well_groups"), "snapshot.well_groups"))
        )
        if not well_groups:
            raise ValidationError("snapshot.well_groups 不能为空")
        facilities = tuple(
            FacilityConstraint.from_dict(item, f"snapshot.facility_constraints[{index}]")
            for index, item in enumerate(_sequence(
                data.get("facility_constraints", []), "snapshot.facility_constraints"))
        )
        outages = tuple(
            OutageWindow.from_dict(item, f"snapshot.outage_windows[{index}]")
            for index, item in enumerate(_sequence(
                data.get("outage_windows", []), "snapshot.outage_windows"))
        )
        capex = tuple(
            CapexItem.from_dict(item, f"snapshot.capex[{index}]", horizon)
            for index, item in enumerate(_sequence(data.get("capex", []), "snapshot.capex"))
        )
        if not capex:
            raise ValidationError("snapshot.capex 不能为空")
        _ensure_unique((item.well_group_id for item in well_groups), "snapshot.well_groups.well_group_id")
        _ensure_unique((item.facility_id for item in facilities), "snapshot.facility_constraints.facility_id")
        _ensure_unique((item.window_id for item in outages), "snapshot.outage_windows.window_id")
        _ensure_unique((item.item_id for item in capex), "snapshot.capex.item_id")
        debottleneck_refs = {
            item.debottleneck_capex_item
            for item in facilities
            if item.debottleneck_capex_item
        }
        capex_ids = {item.item_id for item in capex}
        unknown = sorted(debottleneck_refs - capex_ids)
        if unknown:
            raise ValidationError(f"设施瓶颈引用了不存在的资本支出项: {unknown}")
        outage_facilities = {item.facility_id for item in outages}
        facility_ids = {item.facility_id for item in facilities}
        missing = sorted(outage_facilities - facility_ids)
        if missing:
            raise ValidationError(f"停产窗口引用了未声明的设施: {missing}")
        attestations_raw = _mapping(data.get("attestations"), "snapshot.attestations")
        if set(attestations_raw) != set(SECTION_ATTESTATION_ROLES):
            raise ValidationError(
                "snapshot.attestations 必须覆盖且仅覆盖 "
                f"{sorted(SECTION_ATTESTATION_ROLES)}")
        attestations = {
            key: _identifier(value, f"snapshot.attestations.{key}")
            for key, value in attestations_raw.items()
        }
        return cls(
            snapshot_id=_identifier(data.get("snapshot_id"), "snapshot.snapshot_id"),
            asset_id=_identifier(data.get("asset_id"), "snapshot.asset_id"),
            title=_text(data.get("title"), "snapshot.title"),
            horizon_years=horizon,
            start_date=_date(data.get("start_date"), "snapshot.start_date"),
            reserves=ReservesRevision.from_dict(data.get("reserves"), "snapshot.reserves"),
            well_groups=well_groups,
            water_cut=WaterCutForecast.from_dict(
                data.get("water_cut"), "snapshot.water_cut", horizon),
            facility_constraints=facilities,
            outage_windows=outages,
            capex=capex,
            oil_price=OilPriceAssumption.from_dict(data.get("oil_price"), "snapshot.oil_price"),
            attestations=attestations,
        )


def _ensure_unique(values: Any, path: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValidationError(f"{path} 不能重复: {value}")
        seen.add(value)


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    risk_level: str
    risk_score: Decimal
    factors: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "RiskAssessment":
        data = _mapping(raw, path)
        risk_level = _text(data.get("risk_level"), f"{path}.risk_level", 16)
        if risk_level not in RISK_LEVELS:
            raise ValidationError(f"{path}.risk_level 必须是 high、medium 或 low")
        factors = tuple(
            _text(item, f"{path}.factors[{index}]", 512)
            for index, item in enumerate(_sequence(data.get("factors", []), f"{path}.factors"))
        )
        return cls(
            risk_level=risk_level,
            risk_score=_bounded(data.get("risk_score"), f"{path}.risk_score",
                                minimum=Decimal(0), maximum=Decimal(100)),
            factors=factors,
        )


@dataclass(frozen=True, slots=True)
class DevelopmentPlan:
    """建立在确定快照版本之上的二次开发方案。"""

    plan_id: str
    asset_id: str
    name: str
    snapshot_sha256: str
    parent_plan_id: str | None
    selected_well_groups: tuple[str, ...]
    selected_capex_items: tuple[str, ...]
    incremental_oil_10kt_yearly: tuple[Decimal, ...]
    opex_usd_bbl: Decimal
    discount_rate_percent: Decimal
    capex_overrun_percent: Decimal
    risk: RiskAssessment

    @classmethod
    def from_dict(cls, raw: object, snapshot: InputSnapshot | None = None) -> "DevelopmentPlan":
        data = _mapping(raw, "plan")
        horizon = snapshot.horizon_years if snapshot is not None else None
        raw_series = _sequence(
            data.get("incremental_oil_10kt_yearly"), "plan.incremental_oil_10kt_yearly")
        if horizon is not None and len(raw_series) != horizon:
            raise ValidationError(
                f"plan.incremental_oil_10kt_yearly 长度必须等于快照评价年限 {horizon}")
        if not raw_series:
            raise ValidationError("plan.incremental_oil_10kt_yearly 不能为空")
        production = tuple(
            _bounded(item, f"plan.incremental_oil_10kt_yearly[{index}]", minimum=Decimal(0))
            for index, item in enumerate(raw_series)
        )
        well_groups = tuple(
            _identifier(item, f"plan.selected_well_groups[{index}]")
            for index, item in enumerate(_sequence(
                data.get("selected_well_groups", []), "plan.selected_well_groups"))
        )
        capex_items = tuple(
            _identifier(item, f"plan.selected_capex_items[{index}]")
            for index, item in enumerate(_sequence(
                data.get("selected_capex_items", []), "plan.selected_capex_items"))
        )
        if not well_groups:
            raise ValidationError("plan.selected_well_groups 不能为空")
        if snapshot is not None:
            available_groups = {item.well_group_id for item in snapshot.well_groups}
            unknown_groups = sorted(set(well_groups) - available_groups)
            if unknown_groups:
                raise ValidationError(f"方案选择了快照中不存在的井组: {unknown_groups}")
            available_capex = {item.item_id for item in snapshot.capex}
            unknown_capex = sorted(set(capex_items) - available_capex)
            if unknown_capex:
                raise ValidationError(f"方案选择了快照中不存在的资本支出项: {unknown_capex}")
            if _identifier(data.get("asset_id"), "plan.asset_id") != snapshot.asset_id:
                raise ValidationError("方案所属油田与快照不一致")
        _ensure_unique(well_groups, "plan.selected_well_groups")
        _ensure_unique(capex_items, "plan.selected_capex_items")
        parent = data.get("parent_plan_id")
        return cls(
            plan_id=_identifier(data.get("plan_id"), "plan.plan_id"),
            asset_id=_identifier(data.get("asset_id"), "plan.asset_id"),
            name=_text(data.get("name"), "plan.name"),
            snapshot_sha256=_sha256(data.get("snapshot_sha256"), "plan.snapshot_sha256"),
            parent_plan_id=None if parent is None else _identifier(parent, "plan.parent_plan_id"),
            selected_well_groups=well_groups,
            selected_capex_items=capex_items,
            incremental_oil_10kt_yearly=production,
            opex_usd_bbl=_bounded(data.get("opex_usd_bbl"), "plan.opex_usd_bbl",
                                  minimum=Decimal(0)),
            discount_rate_percent=_bounded(
                data.get("discount_rate_percent"), "plan.discount_rate_percent",
                minimum=Decimal(0), maximum=Decimal(100)),
            capex_overrun_percent=_bounded(
                data.get("capex_overrun_percent", 0), "plan.capex_overrun_percent",
                minimum=Decimal(0), maximum=Decimal(1000)),
            risk=RiskAssessment.from_dict(data.get("risk"), "plan.risk"),
        )


@dataclass(frozen=True, slots=True)
class ActualPerformance:
    """投决后录入的实绩，按评价年归集，只能追加。"""

    plan_id: str
    period_year: int
    production_10kt: Decimal
    water_cut_percent: Decimal
    downtime_days: Decimal
    capex_spent_wan_yuan: Decimal
    price_usd_bbl: Decimal

    @classmethod
    def from_dict(cls, raw: object, horizon: int | None = None) -> "ActualPerformance":
        data = _mapping(raw, "actual")
        period_year = _nonneg_int(
            data.get("period_year"), "actual.period_year", minimum=1,
            maximum=30 if horizon is None else horizon)
        return cls(
            plan_id=_identifier(data.get("plan_id"), "actual.plan_id"),
            period_year=period_year,
            production_10kt=_bounded(data.get("production_10kt"), "actual.production_10kt",
                                     minimum=Decimal(0)),
            water_cut_percent=_bounded(data.get("water_cut_percent"), "actual.water_cut_percent",
                                       minimum=Decimal(0), maximum=Decimal(100)),
            downtime_days=_bounded(data.get("downtime_days"), "actual.downtime_days",
                                   minimum=Decimal(0), maximum=Decimal(366)),
            capex_spent_wan_yuan=_bounded(
                data.get("capex_spent_wan_yuan"), "actual.capex_spent_wan_yuan",
                minimum=Decimal(0)),
            price_usd_bbl=_bounded(data.get("price_usd_bbl"), "actual.price_usd_bbl",
                                   minimum=Decimal("0.01")),
        )
