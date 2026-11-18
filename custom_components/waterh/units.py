"""Volume unit helpers."""

from __future__ import annotations

from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .const import (
    CONF_VOLUME_UNIT,
    ML_PER_FL_OZ,
    UNIT_APP,
    UNIT_AUTO,
    UNIT_ML,
    UNIT_OZ,
)


def resolve_unit(
    hass: HomeAssistant, option: str | None, profile: dict | None = None
) -> str:
    """Return UNIT_ML or UNIT_OZ for an option value.

    auto: Home Assistant's unit system. app: the WaterH app's unit setting
    (profile settings.unit is "imperial" or "metric").
    """
    if option in (UNIT_ML, UNIT_OZ):
        return option
    if option == UNIT_APP and profile:
        app_unit = ((profile.get("settings") or {}).get("unit") or "").lower()
        if app_unit == "imperial":
            return UNIT_OZ
        if app_unit == "metric":
            return UNIT_ML
    return UNIT_OZ if hass.config.units is US_CUSTOMARY_SYSTEM else UNIT_ML


def entry_unit(hass: HomeAssistant, entry) -> str:
    """Resolved display unit for a loaded config entry."""
    coordinator = getattr(entry, "runtime_data", None)
    profile = coordinator.data.profile if coordinator and coordinator.data else None
    return resolve_unit(hass, entry.options.get(CONF_VOLUME_UNIT, UNIT_AUTO), profile)


def ha_unit(unit: str) -> str:
    """Home Assistant unit string for UNIT_ML / UNIT_OZ."""
    return UnitOfVolume.FLUID_OUNCES if unit == UNIT_OZ else UnitOfVolume.MILLILITERS


def to_ml(amount: float, unit: str) -> int:
    """Convert an amount in UNIT_ML / UNIT_OZ to whole millilitres."""
    return round(amount * ML_PER_FL_OZ) if unit == UNIT_OZ else round(amount)


def from_ml(amount_ml: float, unit: str) -> float:
    """Convert millilitres to UNIT_ML / UNIT_OZ."""
    return round(amount_ml / ML_PER_FL_OZ, 1) if unit == UNIT_OZ else float(amount_ml)
