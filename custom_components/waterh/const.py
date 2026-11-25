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

# How drinks reach Home Assistant (chosen at setup):
# - bluetooth: Home Assistant connects to the bottle itself (no account, no app).
# - local: the WaterH Sync Android app sends drinks (no account).
# - cloud: drinks are read from the WaterH account the official app uploads to.
CONF_MODE: Final = "mode"
MODE_BLUETOOTH: Final = "bluetooth"
MODE_CLOUD: Final = "cloud"
MODE_LOCAL: Final = "local"
NO_ACCOUNT_MODES: Final = (MODE_BLUETOOTH, MODE_LOCAL)

# Options for the no-account modes.
# Bluetooth address of the bottle Home Assistant syncs ("" = Bluetooth sync off).
CONF_BOTTLE_ADDRESS: Final = "address"
# Minutes between Bluetooth syncs while the bottle is in range.
CONF_SYNC_INTERVAL: Final = "sync_interval"
DEFAULT_SYNC_INTERVAL_MINUTES: Final = 5
MIN_SYNC_INTERVAL_MINUTES: Final = 1
MAX_SYNC_INTERVAL_MINUTES: Final = 60
# Write Home Assistant's clock, goal and today's total to the bottle on sync.
CONF_UPDATE_BOTTLE: Final = "update_bottle"
# Accept drinks from the WaterH Sync Android app too (webhook).
CONF_ANDROID_APP: Final = "android_app"
# Local mode: goal until the user sets one, and how long drinks are kept on disk.
LOCAL_DEFAULT_GOAL_ML: Final = 2000
LOCAL_KEEP_DAYS: Final = 30
SOURCE_BOTTLE: Final = "waterh-bottle"
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
SERVICE_SET_REMINDER_LIGHT: Final = "set_reminder_light"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_AMOUNT: Final = "amount"
ATTR_UNIT: Final = "unit"
ATTR_DRINK_TYPE: Final = "drink_type"
ATTR_TIME: Final = "time"
ATTR_DRINK_ID: Final = "drink_id"

# Goal limits accepted by the integration (mL). Sanity bounds, not WaterH's.
GOAL_MIN_ML: Final = 500
GOAL_MAX_ML: Final = 10000

# States of the Bluetooth sync sensor.
SYNC_STATUS_WAITING: Final = "waiting"
SYNC_STATUS_SYNCING: Final = "syncing"
SYNC_STATUS_OK: Final = "ok"
SYNC_STATUS_NOT_IN_RANGE: Final = "not_in_range"
SYNC_STATUS_FAILED: Final = "failed"
SYNC_STATUSES: Final = [
    SYNC_STATUS_WAITING,
    SYNC_STATUS_SYNCING,
    SYNC_STATUS_OK,
    SYNC_STATUS_NOT_IN_RANGE,
    SYNC_STATUS_FAILED,
]
