# WaterH for Home Assistant

<img src="custom_components/waterh/brand/icon.png" alt="" width="96" align="right">

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/raymanvh/ha-waterh/actions/workflows/validate.yml/badge.svg)](https://github.com/raymanvh/ha-waterh/actions/workflows/validate.yml)

Unofficial Home Assistant integration for [WaterH](https://www.waterh.com) smart water bottles (Boost, Vita, BoostLite and others). It reads your hydration log from the WaterH cloud, the same data the WaterH app shows, so your water intake appears in Home Assistant without Bluetooth or Health Connect.

> **Not affiliated with WaterH.** This uses WaterH's private app API, found by examining the official Android app. It can break at any time if WaterH changes their API. See [docs/API.md](docs/API.md) for how it works.

## Why

As of late 2026, WaterH has no Android Health Connect support, and its Fitbit integration no longer syncs. The bottle doesn't include drink data in its Bluetooth broadcasts. Drinks only reach the phone over a private connection and are then uploaded to WaterH's servers. Reading from the cloud is the most reliable way to get the data into Home Assistant.

## What you get

One device per WaterH account, with these sensors:

| Sensor | Description |
| --- | --- |
| **Water today** | Hydration logged since local midnight (bottle + manual), in your chosen unit (see Options). Attributes: `drinks`, `bottle_ml`, `manual_ml`, `liquid_total_ml`. |
| **Goal progress** | Water today ÷ daily goal, in %. |
| **Drinks today** | Number of log entries today. |
| **Last drink** | Size of the most recent drink. Attributes: `drink_id`, `source` (`manual` or the bottle model), `water_type`, `liquid_amount_ml`, `temperature`. |
| **Last drink time** | Timestamp of the most recent drink. |
| **Last water temperature** | Water temperature reported by the bottle (°C, disabled by default). |
| **Last update** | When Home Assistant last fetched data from WaterH (diagnostic). If this keeps advancing but a drink is missing, the phone hasn't uploaded it yet. |

Plus these controls:

| Control | Description |
| --- | --- |
| **Daily goal** | Your WaterH goal. Changing it updates the goal in the WaterH app. |
| **Drink amount** | How much **Log drink** adds (default 8 fl. oz. / 250 mL). Kept in Home Assistant only. |
| **Drink type** | Water, coffee, tea or juice, for **Log drink**. Kept in Home Assistant only. |
| **Log drink** | Adds a manual drink with the amount and type above, like the **+** button in the app. |
| **Volume unit** (Configuration) | Automatic, Same as the WaterH app, mL or fl. oz. The same setting as in the integration options; switching it reloads the integration. |

"Water today" uses WaterH's `water_amount` (hydration value). For plain water it equals the liquid volume. For other drink types the app may weight it differently; the raw volume is in `liquid_total_ml`.

Data updates every 5 minutes by default (adjustable from 1 to 60 minutes). The bottle only uploads when it syncs with the WaterH app on your phone, so a drink appears once the app has synced it, not the instant you drink.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=raymanvh&repository=ha-waterh&category=integration)

Click the button above, then **Download** and restart Home Assistant. Or add it by hand:

1. In HACS, open the menu (⋮) → **Custom repositories**.
2. Add `https://github.com/raymanvh/ha-waterh` with category **Integration**.
3. Search for **WaterH**, install it, and restart Home Assistant.

### Manual

Copy `custom_components/waterh` into your Home Assistant `config/custom_components/` folder and restart.

## Setup

**Settings → Devices & services → Add integration → WaterH**, then enter the email and password for your WaterH account.

- **Signed up with Google or Facebook?** Those accounts have no WaterH password. Use **Forgot password** in the WaterH app (or on the login screen) to set one, then use it here. You can still sign in with Google in the app.
- Your password is used once to get a login token and is **not stored**. Home Assistant keeps the token and refreshes it automatically. If WaterH ever rejects it, Home Assistant asks you to sign in again.
- Signing in from Home Assistant may sign other devices out of the WaterH app (not yet confirmed). If it happens, just log back in to the app.

### Options

- **Volume unit** (also on the device page): how volumes are shown and the default unit for `log_drink`. It applies to every WaterH volume entity, overriding any per-entity unit you set.
  - *Automatic*: follows Home Assistant's unit system (fl. oz. for US customary, mL for metric).
  - *Same as the WaterH app*: follows the unit set in the WaterH app.
  - *Milliliters* or *Fluid ounces*: always that unit.
- **Update interval**: minutes between updates (default 5).
- **OAuth client ID / secret (advanced)**: leave these empty. They're only needed if WaterH changes the values built into their app. See [If WaterH changes their app credentials](docs/API.md#if-waterh-changes-their-app-credentials).

## Actions

### `waterh.log_drink`

Adds a manual drink, like the **+** button in the WaterH app. Use it in automations and scripts (for example an NFC tag by the sink); on the device page, the **Log drink** button does the same thing.

| Field | Required | Description |
| --- | --- | --- |
| `amount` | yes | How much you drank. |
| `unit` | no | `ml` or `oz`. Defaults to the unit in the integration options. |
| `drink_type` | no | `water` (default), `coffee`, `tea` or `juice`. |
| `time` | no | When you drank it. Defaults to now. |
| `config_entry_id` | no | Only needed with more than one WaterH account. |

```yaml
action: waterh.log_drink
data:
  amount: 8
  unit: oz
  drink_type: coffee
```

### `waterh.delete_drink`

Removes a log entry by ID. The most recent drink's ID is the `drink_id` attribute of **Last drink**, so this works as an "undo":

```yaml
action: waterh.delete_drink
data:
  drink_id: "{{ state_attr('sensor.waterh_last_drink', 'drink_id') }}"
```

## Example automation

```yaml
automation:
  - alias: "Hydration reminder"
    triggers:
      - trigger: time_pattern
        hours: "/2"
    conditions:
      - condition: time
        after: "09:00:00"
        before: "21:00:00"
      - condition: numeric_state
        entity_id: sensor.waterh_goal_progress
        below: 50
    actions:
      - action: notify.notify
        data:
          message: >
            You're at {{ states('sensor.waterh_goal_progress') | int }}% of your water goal.
```

(Entity IDs depend on your account name; check the device page.)

## Troubleshooting

- **"Wrong email or password"**: check that you can log in to the WaterH app with email and password (not just Google). See the setup note above.
- **Values lag behind the app**: open the WaterH app so the bottle syncs, then wait for the next update.
- **Everything stopped working with login errors**: WaterH may have changed their app credentials. See [docs/API.md](docs/API.md#if-waterh-changes-their-app-credentials).
- Download **diagnostics** from the integration page when reporting a problem. Tokens, email and personal details are redacted.

## How it was built

The API was mapped by recording the WaterH Android app's traffic in an emulator (HTTP Toolkit) and decompiling the app (jadx) to confirm the endpoints and the built-in OAuth client credentials. Full notes are in [docs/API.md](docs/API.md).

## Disclaimer

This project is not endorsed by or affiliated with WaterH Inc. "WaterH" is their trademark. The integration only reads your own data with your own login, but using an unofficial API may go against WaterH's terms of service. Use it at your own risk.

## License

MIT
