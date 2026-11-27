"""Individual drinks as plain dicts, for the Recent drinks sensor and the get_drinks action."""

from __future__ import annotations

from typing import Any

from homeassistant.util import dt as dt_util

from .api import WaterHDrink
from .const import SOURCE_MANUAL
from .units import from_ml


def drink_record(drink: WaterHDrink, unit: str) -> dict[str, Any]:
    """One drink at the time it was taken (local time), in the display unit and mL."""
    return {
        "id": drink.id,
        "time": dt_util.as_local(drink.created).replace(microsecond=0).isoformat(),
        "amount": from_ml(drink.water_amount, unit),
        "ml": drink.water_amount,
        "type": drink.water_type or "water",
        "source": "manual" if drink.source_id == SOURCE_MANUAL else "bottle",
    }
