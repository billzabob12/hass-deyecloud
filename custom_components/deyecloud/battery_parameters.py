"""Shared DeyeCloud battery parameter helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MAX_CHARGE_CURRENT = "MAX_CHARGE_CURRENT"
MAX_DISCHARGE_CURRENT = "MAX_DISCHARGE_CURRENT"
GRID_CHARGE_AMPERE = "GRID_CHARGE_AMPERE"


@dataclass(frozen=True)
class DeyeBatteryParameterDescription:
    """Description for a Deye battery parameter."""

    key: str
    name: str
    parameter_types: tuple[str, ...]
    icon: str
    unit: str = "A"
    min_value: float = 0
    max_value: float = 250
    step: float = 1


BATTERY_PARAMETER_DESCRIPTIONS: tuple[DeyeBatteryParameterDescription, ...] = (
    DeyeBatteryParameterDescription(
        key="max_battery_charge_current",
        name="Max Battery Charge Current",
        parameter_types=(MAX_CHARGE_CURRENT,),
        icon="mdi:battery-arrow-up",
    ),
    DeyeBatteryParameterDescription(
        key="max_battery_discharge_current",
        name="Max Battery Discharge Current",
        parameter_types=(MAX_DISCHARGE_CURRENT,),
        icon="mdi:battery-arrow-down",
    ),
    DeyeBatteryParameterDescription(
        key="grid_charge_current",
        name="Grid Charge Current",
        parameter_types=(GRID_CHARGE_AMPERE,),
        icon="mdi:transmission-tower-import",
    ),
)


_PARAMETER_ALIASES = {
    MAX_CHARGE_CURRENT: (
        "MAX_CHARGE_CURRENT",
        "maxChargeCurrent",
        "max_charge_current",
        "maxChargeAmpere",
        "maxACharge",
        "chargeCurrent",
        "chargeCurrentLimit",
    ),
    MAX_DISCHARGE_CURRENT: (
        "MAX_DISCHARGE_CURRENT",
        "maxDischargeCurrent",
        "max_discharge_current",
        "maxDischargeAmpere",
        "maxADischarge",
        "dischargeCurrent",
        "dischargeCurrentLimit",
    ),
    GRID_CHARGE_AMPERE: (
        "GRID_CHARGE_AMPERE",
        "gridChargeAmpere",
        "grid_charge_ampere",
        "gridChargeCurrent",
        "grid_charge_current",
        "gridChargeA",
        "gridChargeCurrentLimit",
    ),
}


_IDENTITY_KEYS = (
    "paramterType",
    "parameterType",
    "parameter_type",
    "type",
    "key",
    "name",
    "code",
    "field",
    "paramName",
    "parameterName",
)

_VALUE_KEYS = (
    "value",
    "val",
    "parameterValue",
    "paramValue",
    "setValue",
    "settingValue",
    "dataValue",
    "currentValue",
)


def _normalise_key(value: Any) -> str:
    """Normalise keys for loose DeyeCloud response matching."""
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def _to_number(value: Any) -> float | int | None:
    """Convert a DeyeCloud value to a number if possible."""
    if value is None or value == "":
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return value

    text = str(value).strip()
    if not text:
        return None

    # Remove common unit suffixes.
    text = text.replace("A", "").replace("a", "").strip()

    try:
        as_float = float(text)
    except ValueError:
        return None

    if as_float.is_integer():
        return int(as_float)

    return as_float


def _alias_set(parameter_types: tuple[str, ...]) -> set[str]:
    """Return all known aliases for the requested Deye parameter types."""
    aliases: set[str] = set()

    for parameter_type in parameter_types:
        aliases.add(_normalise_key(parameter_type))

        for alias in _PARAMETER_ALIASES.get(parameter_type, ()):
            aliases.add(_normalise_key(alias))

    return aliases


def _find_parameter_value(obj: Any, aliases: set[str]) -> Any:
    """Recursively find a parameter value in a loose DeyeCloud config response."""
    if isinstance(obj, list):
        for item in obj:
            found = _find_parameter_value(item, aliases)
            if found is not None:
                return found

        return None

    if not isinstance(obj, dict):
        return None

    # Direct key match, e.g. {"gridChargeAmpere": 25}
    for key, value in obj.items():
        if _normalise_key(key) in aliases:
            return value

    # List-item style, e.g. {"paramterType": "GRID_CHARGE_AMPERE", "value": 25}
    identity_matches = False

    for key in _IDENTITY_KEYS:
        if key in obj and _normalise_key(obj[key]) in aliases:
            identity_matches = True
            break

    if identity_matches:
        for key in _VALUE_KEYS:
            if key in obj:
                return obj[key]

    # Recurse into nested data/config structures.
    for value in obj.values():
        found = _find_parameter_value(value, aliases)
        if found is not None:
            return found

    return None


def extract_battery_parameter(
    config_response: dict[str, Any] | None,
    parameter_types: tuple[str, ...],
) -> float | int | None:
    """Extract a battery parameter from a DeyeCloud /config/battery response."""
    if not config_response:
        return None

    aliases = _alias_set(parameter_types)
    raw_value = _find_parameter_value(config_response, aliases)

    return _to_number(raw_value)
