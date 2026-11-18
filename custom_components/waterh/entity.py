"""Shared entity helpers for WaterH."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN
from .coordinator import WaterHCoordinator


def waterh_device_info(coordinator: WaterHCoordinator) -> DeviceInfo:
    """Device for the WaterH account."""
    profile = coordinator.data.profile if coordinator.data else {}
    name = " ".join(
        p for p in (profile.get("first_name"), profile.get("last_name")) if p
    )
    return DeviceInfo(
        identifiers={(DOMAIN, str(coordinator.user_id))},
        name=f"WaterH {name}".strip() if name else "WaterH",
        manufacturer="WaterH",
        model="Hydration account",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://www.waterh.com",
    )
