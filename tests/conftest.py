"""Shared fixtures for WaterH tests."""

from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

TOKEN_RESPONSE = {
    "access_token": "access-1",
    "expires_in": 5184000,
    "token_type": "Bearer",
    "scope": "openid profile email",
    "refresh_token": "refresh-1",
    "user": {"id": 123456, "email": "user@example.com"},
}

SELF_RESPONSE = {
    "id": 123456,
    "email": "user@example.com",
    "first_name": "Test User",
    "goal": 4347,
}


def log_response(now_iso_list: list[tuple[str, int, str]]) -> dict:
    return {
        "data": [
            {
                "amount": amount,
                "water_amount": amount,
                "date_created": created,
                "source_id": source,
                "tds": 0 if source != "manual" else None,
                "temperature": 10 if source != "manual" else None,
                "mac_address": "aa:bb:cc:dd:ee:ff" if source != "manual" else None,
                "water_type": "water",
                "id": 1000 + i,
            }
            for i, (created, amount, source) in enumerate(now_iso_list)
        ],
        "metrics": [],
    }


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations in all tests."""
    yield
