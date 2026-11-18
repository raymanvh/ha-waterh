# WaterH cloud API notes

Unofficial notes on the private API used by the WaterH Android app (`com.waterh`, version **3.5.1**, build 73), as used by this integration. Everything here was observed or decompiled in November 2026 and may change.

## Architecture

- **Base URL:** `https://api.waterh.com` (behind Cloudflare and Google Cloud).
- **Server:** a REST API that looks like Django REST Framework with django-oauth-toolkit: trailing-slash paths, `allow: GET, POST, HEAD, OPTIONS` headers, and standard OAuth2 token responses.
- **Auth:** OAuth2 bearer tokens. The app logs in with the **resource owner password grant** using a client ID and secret built into the app.
- **Client:** Retrofit + OkHttp + Moshi (Kotlin). Every request carries `X-App-Version: 3.5.1` and a `Time-Zone` header (IANA name, e.g. `America/Chicago`).
- **Other services the app talks to (not used here):** Firebase (project `absolute-bot-320419`), Sentry for crash reporting, and `media.waterh.com` for avatars.

## Authentication

### Log in (password grant)

```http
POST /account/oauth/token/
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "…",
  "grant_type": "password",
  "client_id": "<app client id>",
  "client_secret": "<app client secret>"
}
```

Response:

```json
{
  "access_token": "eyJ…",          // JWT
  "expires_in": 5184000,           // 60 days
  "token_type": "Bearer",
  "scope": "openid profile email",
  "refresh_token": "…",
  "user": { "id": 123456, "email": "…", "first_name": "…", "goal": 4347, … }
}
```

Use it as `Authorization: Bearer <access_token>`.

### Refresh

```http
POST /account/oauth/token/
Content-Type: application/json

{
  "grant_type": "refresh_token",
  "refresh_token": "…",
  "client_id": "<app client id>",
  "client_secret": "<app client secret>"
}
```

Same response shape as login. The integration saves the new refresh token every time.

### Social logins

`POST /account/oauth/google/` and `POST /account/oauth/facebook/` exchange a Google or Facebook ID token for the same kind of WaterH token. They need a Google or Facebook sign-in in the app's context, so the integration only supports email + password. Accounts created with Google can set a password with **Forgot password** (`POST /account/password/reset/`).

### Revoke

`POST /account/oauth/revoke/` exists but isn't used.

## Endpoints used by the integration

### `GET /account/self/`

Profile of the logged-in user. Fields that matter here:

| Field | Meaning |
| --- | --- |
| `id` | Numeric user ID, needed for the log endpoint. |
| `goal` | Daily goal in **mL** (`goalInMl` in the app). |
| `first_name`, `last_name`, `email`, `weight`, `height`, `dob`, `settings`, `kids` | Profile and app settings (not used). |

### `GET /account/{id}/log`

Hydration log for a time window.

| Query parameter | Meaning |
| --- | --- |
| `period` | `daily`, `monthly` or `yearly`: how the `metrics` array is grouped (`total_water`, `total`, `trunc_date` per bucket; not used by the integration). |
| `date_finished_from` | Window start, **Unix seconds**. The app uses local midnight. |
| `date_finished_to` | Window end, Unix seconds (the next local midnight for "today"). |

Header: `Time-Zone: America/Chicago`.

Response (`WaterLogResponseV3`, shortened; IDs and the `metrics` entry are illustrative):

```json
{
  "data": [
    {
      "id": 132700001,
      "amount": 68,
      "water_amount": 68,
      "date_created": "2026-11-18T00:22:40.000000Z",
      "source_id": "waterh-boost-946ml",
      "mac_address": "aa:bb:cc:dd:ee:ff",
      "water_type": "water",
      "temperature": 10,
      "tds": 0
    },
    {
      "id": 132700002,
      "amount": 945,
      "water_amount": 945,
      "date_created": "2026-11-17T23:27:49.000000Z",
      "source_id": "manual",
      "mac_address": null,
      "water_type": "water",
      "temperature": null,
      "tds": null
    }
  ],
  "metrics": [
    { "total_water": 1216, "total": 1216, "trunc_date": "…" }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `amount` | Liquid volume in mL. |
| `water_amount` | Hydration value in mL. Equal to `amount` for water; the app may weight other drink types. |
| `date_created` | UTC timestamp of the drink. |
| `source_id` | `manual` for entries added in the app, otherwise the bottle model (`waterh-boost-946ml`, …). |
| `mac_address` | Bottle's Bluetooth MAC (null for manual entries). |
| `temperature` | Water temperature from the bottle, apparently °C. |
| `tds` | Total dissolved solids (bottles with a TDS sensor); 0 on bottles without one. |
| `water_type` | Drink type, `water` by default. |

The `data` array comes back newest first. The integration sorts it.

## Write endpoints used by the integration

All three calls below were captured from the app in November 2026. The integration's add and delete requests match the app's exactly. For the goal, the app's goal screen sends the whole profile, while the integration sends only `goal`. That follows the app's own partial-update pattern: its name editor sends only the changed field.

### Add a drink: `POST /account/{id}/log`

What the app sends when you tap **+**:

```json
{
  "data": [
    { "amount": 135, "date_created": "2026-11-18T00:43:28+00:00", "water_type": "water" }
  ],
  "ble_data": []
}
```

- `amount` is mL. `date_created` is UTC in `yyyy-MM-dd'T'HH:mm:ss+00:00` format (the app's `WaterLogDateAdapter`).
- `water_type` is one of `water`, `coffee`, `tea` or `juice`.
- Bottle syncs use the same endpoint and add `source_id`, `mac_address`, `tds` and `temperature`, plus raw `ble_data`.
- Response: **201** with the stored entry, which the server tags `"source_id": "manual"`.

### Set the goal: `PATCH /account/self/`

```json
{ "goal": 4347 }
```

`goal` is in mL. Response: **200** with the updated profile. The profile's `settings.unit` (`imperial` or `metric`) is the app's display unit, which the integration can follow.

### Delete a drink: `DELETE /v2/log/{id}`

Response: **204**. Captured from the app.

## Other endpoints seen in the app (not used)

From the app's Retrofit `ApiService` interface:

- Account: `GET /account/{id}`, `PATCH /account/self/`, `DELETE /account/self/`, `POST /account/registration/`, `POST /account/email-verify/`, `POST /account/id-verify`, `POST /account/password/change/`, `POST /account/password/reset/`, `POST /account/password/reset/pin/`
- Logging: `PATCH /v2/log/{id}` (edit an entry)
- Social: `/account/friend-request`, `/account/relationship/{id}`, `/account/search`, `/account/pinned`, `/account/{id}/nudge`, `/account/{id}/subscribe`, `/account/{id}/un-subscribe`, `/account/kid`
- Other: `GET /discover`, `GET/POST /reward/`, `reward/discount_code/`, `GET/POST/DELETE /filter/`, `GET /ai/hydrate-buddy` (rate-limited, returns 429), `GET config/ble/`, `GET config/beta/ble/`, `POST /ota-log/`, `POST /fcm`, `GET serverStatus`


## How this was reverse engineered

1. **Bluetooth first.** Home Assistant's Bluetooth scanners showed the bottle (`WaterH-Boost-946mL`) broadcasting only its name: no manufacturer or service data. The drink amounts aren't in the broadcasts, so passive Bluetooth tracking isn't possible.
2. **Traffic capture.** The app was installed in an Android Studio emulator running an Android 14 **Google APIs** image (rootable, unlike Google Play images). [HTTP Toolkit](https://httptoolkit.com)'s "Android device via ADB" mode installed a system-level CA certificate. The app does **no certificate pinning**, so all `api.waterh.com` traffic could be read in plain text after logging in.
3. **Decompiling.** The APK was decompiled with [jadx](https://github.com/skylot/jadx):
   - `com.waterh.BuildConfig` → `BASE_URL = "https://api.waterh.com"`, version 3.5.1.
   - `com.waterh.network.ApiService` → the full endpoint list above.
   - `com.waterh.data.payloads.LoginPayload` and `RefreshAccessTokenPayload` → default constructor values hold the OAuth `client_id` and `client_secret`, plus the `password` and `refresh_token` grant types.
   - `com.waterh.data.models.WaterDataV3`, `User`, and `com.waterh.data.responses.WaterLogResponseV3` → the response field names and units (`goal` is `goalInMl`).

### About the built-in client secret

The client ID and secret ship inside every copy of the app, so anyone with the APK can extract them. That's normal for mobile apps: OAuth treats installed apps as *public clients* that can't keep secrets ([RFC 8252 §8.5](https://datatracker.ietf.org/doc/html/rfc8252#section-8.5)). The values only identify "the WaterH app"; account access still needs the user's own email and password. They're included in this integration so users don't have to extract them.

## If WaterH changes their app credentials

If every login suddenly fails with "Wrong email or password" even though the password works in the app, WaterH may have rotated the client credentials in a new app version.

1. Get the current APK. Copy it from your phone (`adb shell pm path com.waterh`, then `adb pull` each path) or use the app file from an official update.
2. Decompile it: `jadx -d out base.apk`.
3. Search for the new values:
   ```bash
   grep -rn "LoginPayload(String" out/sources/com/waterh/data/payloads/LoginPayload.java
   ```
   The constructor's default arguments are the `client_id` (40 characters) and `client_secret` (128 characters).
4. In Home Assistant, open **WaterH → Configure** and paste them into the **OAuth client ID / secret** fields. This takes effect immediately, without waiting for an integration update.
5. Please open an issue (or a pull request updating `const.py`) so everyone gets the fix.

If logins still fail, check HTTP Toolkit to see whether the login request itself changed (new endpoint, extra headers, or certificate pinning added).
