"""Config flow for WaterH."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD, CONF_WEBHOOK_ID
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    WaterHApiClient,
    WaterHAuthError,
    WaterHError,
    WaterHTokens,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ANDROID_APP,
    CONF_BOTTLE_ADDRESS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_MODE,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SYNC_INTERVAL,
    CONF_TOKEN_EXPIRES_AT,
    CONF_UPDATE_BOTTLE,
    CONF_USER_ID,
    CONF_VOLUME_UNIT,
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SYNC_INTERVAL_MINUTES,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MAX_SYNC_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    MIN_SYNC_INTERVAL_MINUTES,
    MODE_BLUETOOTH,
    MODE_CLOUD,
    MODE_LOCAL,
    UNIT_APP,
    UNIT_AUTO,
    VOLUME_UNITS,
)
from .settings import (
    android_app_enabled,
    bottle_address,
    entry_mode,
    is_no_account,
    sync_interval,
    update_bottle,
)
from .webhook import webhook_urls, webhook_urls_for_id

_LOGGER = logging.getLogger(__name__)

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
# Value used in the options form's bottle picker for "Bluetooth sync off".
ADDRESS_OFF = "off"


def _discovered_bottles(hass) -> dict[str, str]:
    """WaterH bottles Home Assistant can currently hear and connect to: address -> name."""
    if "bluetooth" not in hass.config.components:
        return {}
    from homeassistant.components import bluetooth

    return {
        info.address.upper(): info.name
        for info in bluetooth.async_discovered_service_info(hass, connectable=True)
        if "waterh" in (info.name or "").lower()
    }

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


def _token_data(tokens: WaterHTokens) -> dict[str, Any]:
    return {
        CONF_ACCESS_TOKEN: tokens.access_token,
        CONF_REFRESH_TOKEN: tokens.refresh_token,
        CONF_TOKEN_EXPIRES_AT: tokens.expires_at,
    }


class WaterHConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WaterH."""

    VERSION = 1

    async def _async_login(
        self,
        email: str,
        password: str,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str = DEFAULT_CLIENT_SECRET,
    ) -> tuple[WaterHTokens, dict[str, Any]]:
        client = WaterHApiClient(
            async_get_clientsession(self.hass),
            client_id,
            client_secret,
            time_zone=self.hass.config.time_zone,
        )
        tokens = await client.async_login(email, password)
        profile = await client.async_get_self()
        return tokens, profile

    def _address_in_use(self, address: str) -> bool:
        return any(bottle_address(e) == address for e in self._async_current_entries())

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how drinks reach Home Assistant. Bluetooth is first and recommended."""
        return self.async_show_menu(
            step_id="user", menu_options=["pick_bluetooth", MODE_LOCAL, MODE_CLOUD]
        )

    # ---- Bluetooth through Home Assistant -------------------------------

    async def async_step_bluetooth(self, discovery_info) -> ConfigFlowResult:
        """Home Assistant heard a WaterH bottle."""
        address = discovery_info.address.upper()
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()
        if self._address_in_use(address):
            return self.async_abort(reason="already_configured")
        self._address = address
        self._bottle_name = discovery_info.name or "WaterH bottle"
        self.context["title_placeholders"] = {"name": self._bottle_name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_pick_bluetooth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a bottle Home Assistant can hear, or type its address."""
        if "bluetooth" not in self.hass.config.components:
            return self.async_abort(reason="no_bluetooth")
        errors: dict[str, str] = {}
        found = {
            address: name
            for address, name in _discovered_bottles(self.hass).items()
            if not self._address_in_use(address)
        }
        if user_input is not None:
            address = (user_input.get(CONF_BOTTLE_ADDRESS) or "").strip().upper()
            if not address:
                errors["base"] = "no_bottle_found" if not found else "pick_bottle"
            elif not MAC_RE.match(address):
                errors[CONF_BOTTLE_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                if self._address_in_use(address):
                    return self.async_abort(reason="already_configured")
                self._address = address
                self._bottle_name = found.get(address) or "WaterH bottle"
                return await self.async_step_bluetooth_confirm()

        options = [
            SelectOptionDict(value=address, label=f"{name} ({address})")
            for address, name in found.items()
        ]
        return self.async_show_form(
            step_id="pick_bluetooth",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_BOTTLE_ADDRESS): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            description_placeholders={"count": str(len(options))},
            errors=errors,
        )

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain what Bluetooth mode does, name it, and create the entry."""
        if user_input is not None:
            name = user_input[CONF_NAME].strip() or "Bottle"
            return self.async_create_entry(
                title=f"{name} (Bluetooth)",
                data={
                    CONF_MODE: MODE_BLUETOOTH,
                    CONF_NAME: name,
                    CONF_BOTTLE_ADDRESS: self._address,
                    # Ready in case the Android app is turned on later in the options.
                    CONF_WEBHOOK_ID: webhook.async_generate_id(),
                },
                options={
                    CONF_SYNC_INTERVAL: DEFAULT_SYNC_INTERVAL_MINUTES,
                    CONF_UPDATE_BOTTLE: True,
                    CONF_ANDROID_APP: False,
                },
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default="Bottle"): TextSelector()}),
            description_placeholders={
                "bottle": f"{self._bottle_name} ({self._address})",
                "interval": str(DEFAULT_SYNC_INTERVAL_MINUTES),
            },
        )

    # ---- WaterH Sync Android app ----------------------------------------

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Android app: name the bottle; no WaterH account needed."""
        if user_input is not None:
            self._local_name = user_input[CONF_NAME].strip() or "Bottle"
            self._local_webhook_id = webhook.async_generate_id()
            await self.async_set_unique_id(f"local_{self._local_webhook_id}")
            return await self.async_step_local_finish()
        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default="Bottle"): TextSelector()}),
        )

    async def async_step_local_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the webhook URL to paste into the app, then create the entry."""
        if user_input is not None:
            return self.async_create_entry(
                title=f"{self._local_name} (local)",
                data={
                    CONF_MODE: MODE_LOCAL,
                    CONF_NAME: self._local_name,
                    CONF_WEBHOOK_ID: self._local_webhook_id,
                },
            )
        return self.async_show_form(
            step_id="local_finish",
            data_schema=vol.Schema({}),
            description_placeholders={
                "webhook_url": webhook_urls_for_id(self.hass, self._local_webhook_id)
            },
        )

    # ---- WaterH account (official app) ----------------------------------

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for WaterH email and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            try:
                tokens, profile = await self._async_login(
                    email, user_input[CONF_PASSWORD]
                )
            except WaterHAuthError:
                errors["base"] = "invalid_auth"
            except WaterHError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error logging in to WaterH")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(str(profile["id"]))
                self._abort_if_unique_id_configured()
                # The password is only used to obtain tokens and is not stored.
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_MODE: MODE_CLOUD,
                        CONF_EMAIL: email,
                        CONF_USER_ID: int(profile["id"]),
                        **_token_data(tokens),
                    },
                )
        return self.async_show_form(
            step_id="cloud", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication when the refresh token stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client_id = entry.options.get(CONF_CLIENT_ID) or DEFAULT_CLIENT_ID
            client_secret = (
                entry.options.get(CONF_CLIENT_SECRET) or DEFAULT_CLIENT_SECRET
            )
            try:
                tokens, profile = await self._async_login(
                    entry.data[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    client_id,
                    client_secret,
                )
            except WaterHAuthError:
                errors["base"] = "invalid_auth"
            except WaterHError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error re-authenticating WaterH")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(str(profile["id"]))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates=_token_data(tokens)
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    )
                }
            ),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> WaterHOptionsFlow:
        """Return the options flow."""
        return WaterHOptionsFlow()


class WaterHOptionsFlow(OptionsFlow):
    """Polling interval and (advanced) OAuth client override."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if is_no_account(self.config_entry):
            return await self.async_step_sync(user_input)
        if user_input is not None:
            cleaned = {
                CONF_VOLUME_UNIT: user_input.get(CONF_VOLUME_UNIT, UNIT_AUTO),
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
            }
            for key in (CONF_CLIENT_ID, CONF_CLIENT_SECRET):
                value = (user_input.get(key) or "").strip()
                if value:
                    cleaned[key] = value
            return self.async_create_entry(data=cleaned)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_VOLUME_UNIT,
                    default=options.get(CONF_VOLUME_UNIT, UNIT_AUTO),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=VOLUME_UNITS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_VOLUME_UNIT,
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(
                        CONF_SCAN_INTERVAL,
                        int(DEFAULT_SCAN_INTERVAL.total_seconds() // 60),
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_MINUTES,
                        max=MAX_SCAN_INTERVAL_MINUTES,
                        step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_CLIENT_ID,
                    description={"suggested_value": options.get(CONF_CLIENT_ID)},
                ): TextSelector(),
                vol.Optional(
                    CONF_CLIENT_SECRET,
                    description={"suggested_value": options.get(CONF_CLIENT_SECRET)},
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "webhook_url": webhook_urls(self.hass, self.config_entry)
            },
        )

    async def async_step_sync(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """No-account modes: where drinks come from, how often, and the display unit."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = (user_input.get(CONF_BOTTLE_ADDRESS) or ADDRESS_OFF).strip()
            address = "" if raw.lower() == ADDRESS_OFF else raw.upper()
            app = bool(user_input.get(CONF_ANDROID_APP))
            if address and not MAC_RE.match(address):
                errors[CONF_BOTTLE_ADDRESS] = "invalid_address"
            elif address and "bluetooth" not in self.hass.config.components:
                errors[CONF_BOTTLE_ADDRESS] = "no_bluetooth"
            elif not address and not app:
                errors["base"] = "no_source"
            else:
                return self.async_create_entry(
                    data={
                        CONF_BOTTLE_ADDRESS: address,
                        CONF_ANDROID_APP: app,
                        CONF_SYNC_INTERVAL: int(user_input[CONF_SYNC_INTERVAL]),
                        CONF_UPDATE_BOTTLE: bool(user_input.get(CONF_UPDATE_BOTTLE)),
                        CONF_VOLUME_UNIT: user_input.get(CONF_VOLUME_UNIT, UNIT_AUTO),
                    }
                )

        current = bottle_address(entry)
        bottles = _discovered_bottles(self.hass)
        if current and current not in bottles:
            bottles[current] = "WaterH bottle"
        address_options = [SelectOptionDict(value=ADDRESS_OFF, label="Off")] + [
            SelectOptionDict(value=address, label=f"{name} ({address})")
            for address, name in bottles.items()
        ]
        options = entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BOTTLE_ADDRESS, default=current or ADDRESS_OFF
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=address_options,
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_SYNC_INTERVAL,
                    default=int(sync_interval(entry).total_seconds() // 60),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SYNC_INTERVAL_MINUTES,
                        max=MAX_SYNC_INTERVAL_MINUTES,
                        step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_UPDATE_BOTTLE, default=update_bottle(entry)
                ): BooleanSelector(),
                vol.Required(
                    CONF_ANDROID_APP, default=android_app_enabled(entry)
                ): BooleanSelector(),
                vol.Required(
                    CONF_VOLUME_UNIT,
                    default=options.get(CONF_VOLUME_UNIT, UNIT_AUTO),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[u for u in VOLUME_UNITS if u != UNIT_APP],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_VOLUME_UNIT,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="sync",
            data_schema=schema,
            description_placeholders={
                "webhook_url": webhook_urls(self.hass, entry),
                "mode": entry_mode(entry),
            },
            errors=errors,
        )
