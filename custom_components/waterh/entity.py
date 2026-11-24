"""Shared entity helpers for WaterH."""

from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import (
    CONNECTION_BLUETOOTH,
    DeviceEntryType,
    DeviceInfo,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WaterHError
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
        model=(
            "Bottle via Home Assistant Bluetooth"
            if coordinator.ble is not None
            else "WaterH Sync (local)"
            if coordinator.is_local
            else "Hydration account"
        ),
        # A Bluetooth-synced bottle is a real device; the account and app modes are services.
        entry_type=None if coordinator.ble is not None else DeviceEntryType.SERVICE,
        connections=(
            {(CONNECTION_BLUETOOTH, coordinator.ble.address)}
            if coordinator.ble is not None
            else set()
        ),
        configuration_url=None if coordinator.is_local else "https://www.waterh.com",
    )


class WaterHReminderEntity(CoordinatorEntity[WaterHCoordinator]):
    """Base for the bottle reminder settings (Bluetooth through Home Assistant only).

    Shows the reminder as chosen in Home Assistant, or as the bottle last
    reported it; unavailable until the bottle has been read once.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WaterHCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.user_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = waterh_device_info(coordinator)

    @property
    def available(self) -> bool:
        return self.coordinator.ble is not None and self.coordinator.reminder is not None

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        # True until the bottle has the change made in Home Assistant.
        return {"waiting_for_bottle": self.coordinator.reminder_pending}

    async def _async_change(self, **changes: int) -> None:
        try:
            await self.coordinator.async_set_reminder(**changes)
        except WaterHError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_reminder_failed",
                translation_placeholders={"error": str(err)},
            ) from err
