"""Constants for the WaterH integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "waterh"

API_BASE_URL: Final = "https://api.waterh.com"

# OAuth client credentials shipped inside the official WaterH Android app
# (com.waterh 3.5.1, LoginPayload / RefreshAccessTokenPayload defaults).
# They identify the app, not a user. See docs/API.md for how they were found
# and what to do if WaterH ever rotates them.
DEFAULT_CLIENT_ID: Final = "Qm2VE4FOkzHLHaeYg2JEl4K8BJXYs5iv9ESb5uIX"
DEFAULT_CLIENT_SECRET: Final = (
    "Su4fJaRREK9EudcPY7dipTlv6vbPGOiJsB4aSupdEWyAmYq7VqiNhKeyttDR5yIBfugSLQRL"
    "LiQz2Vw0d4UAOw6gcmIfOaLK4rmjbRVwBVefqPwEBxDIMXQuq42TWJ4s"
)

# The app sends its version on every request; mirror it so requests look
# like a supported client.
APP_VERSION: Final = "3.5.1"

CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_TOKEN_EXPIRES_AT: Final = "token_expires_at"
CONF_USER_ID: Final = "user_id"
CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_SCAN_INTERVAL: Final = "scan_interval"

DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)
MIN_SCAN_INTERVAL_MINUTES: Final = 1
MAX_SCAN_INTERVAL_MINUTES: Final = 60

SOURCE_MANUAL: Final = "manual"

# Display unit for volumes (option). "auto" follows Home Assistant's unit system.
CONF_VOLUME_UNIT: Final = "volume_unit"
# Webhook the WaterH Sync Android app posts bottle status to.
CONF_WEBHOOK_ID: Final = "webhook_id"
# Set once the app has reported, so bottle entities are only created for app users.
CONF_APP_REPORTED: Final = "app_reported"
UNIT_AUTO: Final = "auto"
UNIT_APP: Final = "app"
UNIT_ML: Final = "ml"
UNIT_OZ: Final = "oz"
VOLUME_UNITS: Final = [UNIT_AUTO, UNIT_APP, UNIT_ML, UNIT_OZ]

ML_PER_FL_OZ: Final = 29.5735295625

# Drink types the WaterH app offers (WaterLogPayloadKt).
DRINK_TYPES: Final = ["water", "coffee", "tea", "juice"]

SERVICE_LOG_DRINK: Final = "log_drink"
SERVICE_DELETE_DRINK: Final = "delete_drink"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_AMOUNT: Final = "amount"
ATTR_UNIT: Final = "unit"
ATTR_DRINK_TYPE: Final = "drink_type"
ATTR_TIME: Final = "time"
ATTR_DRINK_ID: Final = "drink_id"

# Goal limits accepted by the integration (mL). Sanity bounds, not WaterH's.
GOAL_MIN_ML: Final = 500
GOAL_MAX_ML: Final = 10000
