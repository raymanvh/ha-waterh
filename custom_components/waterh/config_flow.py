"""Config flow for WaterH."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN_EXPIRES_AT,
    CONF_USER_ID,
    CONF_VOLUME_UNIT,
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    UNIT_AUTO,
    VOLUME_UNITS,
)

_LOGGER = logging.getLogger(__name__)

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

    async def async_step_user(
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
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error logging in to WaterH")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(str(profile["id"]))
                self._abort_if_unique_id_configured()
                # The password is only used to obtain tokens and is not stored.
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_USER_ID: int(profile["id"]),
                        **_token_data(tokens),
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
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
            except Exception:  # noqa: BLE001
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
        return self.async_show_form(step_id="init", data_schema=schema)
