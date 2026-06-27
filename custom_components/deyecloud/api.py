"""DeyeCloud API helpers."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class DeyeCloudApiError(Exception):
    """Base error for DeyeCloud API calls."""


class DeyeCloudOrderError(DeyeCloudApiError):
    """Raised when a DeyeCloud command order fails."""


class DeyeCloudOrderTimeout(DeyeCloudApiError):
    """Raised when a DeyeCloud command order does not complete in time."""


def _sha256(password: str) -> str:
    """Hash DeyeCloud password."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest().lower()


def _build_login_payload(login: str) -> dict[str, str]:
    """
    Build DeyeCloud login payload using either email or username.

    DeyeCloud token API supports login by mobile, email, or username.
    This integration has a single username/login config field, so choose
    the payload key based on the entered value.
    """
    login = login.strip()

    if "@" in login:
        return {"email": login}

    return {"username": login}


async def async_get_token(
    session: aiohttp.ClientSession,
    username,
    password,
    app_id,
    app_secret,
    base_url,
    company_id=None,
):
    """
    Get DeyeCloud access token.

    If company_id is provided, DeyeCloud returns a business/company token.
    This is needed for installer/business accounts.
    """
    url = f"{base_url}/account/token?appId={app_id}"

    payload = {
        "appSecret": app_secret,
        **_build_login_payload(username),
        "password": _sha256(password),
    }

    if company_id:
        payload["companyId"] = str(company_id).strip()

    async with session.post(url, json=payload, timeout=10) as resp:
        resp.raise_for_status()
        data = await resp.json()

    if not data.get("success"):
        raise DeyeCloudApiError(f"Token request failed: {data.get('msg') or data}")

    return data["accessToken"]


def _extract_order_id(data: dict[str, Any]):
    """Extract orderId from a DeyeCloud response."""
    if not isinstance(data, dict):
        return None

    nested = data.get("data") if isinstance(data.get("data"), dict) else {}

    return (
        data.get("orderId")
        or data.get("orderID")
        or data.get("order_id")
        or nested.get("orderId")
        or nested.get("orderID")
        or nested.get("order_id")
    )


def _extract_nested_data(data: dict[str, Any]) -> dict[str, Any]:
    """Return nested response data if available."""
    nested = data.get("data")

    if isinstance(nested, dict):
        return nested

    return {}


def _extract_order_status(data: dict[str, Any]):
    """Extract a possible DeyeCloud order status field."""
    if not isinstance(data, dict):
        return None

    nested = _extract_nested_data(data)

    for source in (data, nested):
        for key in (
            "status",
            "orderStatus",
            "executeStatus",
            "executionStatus",
            "commandStatus",
            "result",
            "state",
        ):
            if key in source:
                return source[key]

    return None


def _classify_order_result(data: dict[str, Any]) -> str:
    """
    Classify a DeyeCloud command result.

    Returns:
      pending
      success
      failed

    DeyeCloud's public samples show the order polling endpoint but do not
    clearly document every possible terminal status code, so this is defensive.
    Unknown success=True terminal-looking statuses are treated as success,
    but logged so the mapping can be refined.
    """
    if not isinstance(data, dict):
        return "pending"

    if data.get("success") is False:
        return "failed"

    status = _extract_order_status(data)
    status_text = str(status).strip().upper() if status is not None else ""
    code_text = str(data.get("code") or "").strip()
    msg_text = str(data.get("msg") or "").lower()

    pending_values = {
        "0",
        "100",
        "PENDING",
        "WAITING",
        "RUNNING",
        "PROCESSING",
        "SENT",
        "IN_PROGRESS",
    }

    success_values = {
        "1",
        "2",
        "200",
        "SUCCESS",
        "SUCCESSFUL",
        "DONE",
        "FINISHED",
        "COMPLETED",
        "EXECUTED",
    }

    failure_values = {
        "-1",
        "3",
        "4",
        "300",
        "400",
        "500",
        "FAILED",
        "FAIL",
        "FAILURE",
        "ERROR",
        "TIMEOUT",
        "TIMED_OUT",
        "REJECTED",
    }

    if status_text in pending_values:
        return "pending"

    if status_text in success_values:
        return "success"

    if status_text in failure_values:
        return "failed"

    if "fail" in msg_text or "error" in msg_text or "timeout" in msg_text:
        return "failed"

    # Some DeyeCloud responses use 1000000 for general success.
    if status is None and code_text == "1000000" and data.get("success") is True:
        return "success"

    # If the API returns an unknown status while success=True, assume it is
    # terminal success but log it for later refinement.
    if status is not None and data.get("success") is True:
        _LOGGER.warning(
            "Unknown DeyeCloud order status %s; treating as success. Full response: %s",
            status,
            data,
        )
        return "success"

    return "pending"


async def async_get_order_result(
    session: aiohttp.ClientSession,
    token,
    base_url,
    order_id,
):
    """Get DeyeCloud command execution result."""
    url = f"{base_url}/order/{order_id}"
    headers = {"Authorization": f"Bearer {token}"}

    async with session.get(url, headers=headers, timeout=10) as resp:
        resp.raise_for_status()
        return await resp.json()


async def async_wait_for_order_result(
    session: aiohttp.ClientSession,
    token,
    base_url,
    order_id,
    *,
    timeout_seconds: int = 90,
    poll_interval_seconds: int = 3,
):
    """Poll DeyeCloud until command order succeeds, fails, or times out."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_result = None

    while asyncio.get_running_loop().time() < deadline:
        result = await async_get_order_result(
            session,
            token,
            base_url,
            order_id,
        )

        last_result = result
        classification = _classify_order_result(result)

        _LOGGER.debug(
            "DeyeCloud order %s poll result: %s classified as %s",
            order_id,
            result,
            classification,
        )

        if classification == "success":
            return result

        if classification == "failed":
            raise DeyeCloudOrderError(
                f"DeyeCloud order {order_id} failed: {result}"
            )

        await asyncio.sleep(poll_interval_seconds)

    raise DeyeCloudOrderTimeout(
        f"DeyeCloud order {order_id} did not complete within "
        f"{timeout_seconds}s. Last result: {last_result}"
    )


async def async_post_control(
    session: aiohttp.ClientSession,
    token,
    base_url,
    path: str,
    payload: dict[str, Any],
    *,
    wait_for_result: bool = True,
    timeout_seconds: int = 90,
    poll_interval_seconds: int = 3,
):
    """POST a DeyeCloud control command and optionally poll its orderId."""
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
        resp.raise_for_status()
        data = await resp.json()

    if not data.get("success", True):
        raise DeyeCloudOrderError(
            f"DeyeCloud control request failed: {data.get('msg') or data}"
        )

    if not wait_for_result:
        return data

    order_id = _extract_order_id(data)

    if not order_id:
        raise DeyeCloudOrderError(
            f"DeyeCloud command was accepted but no orderId was returned: {data}"
        )

    order_result = await async_wait_for_order_result(
        session,
        token,
        base_url,
        order_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )

    data["order_result"] = order_result
    return data


async def async_control_solar_sell(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
    is_enable,
    *,
    wait_for_result: bool = True,
):
    """Send Solar Sell control command."""
    return await async_post_control(
        session,
        token,
        base_url,
        "/order/sys/solarSell/control",
        {
            "action": "on" if is_enable else "off",
            "deviceSn": device_sn,
        },
        wait_for_result=wait_for_result,
    )


async def async_control_grid_charge(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
    is_enable: bool,
    *,
    wait_for_result: bool = True,
):
    """Enable or disable Deye grid charge mode."""
    return await async_post_control(
        session,
        token,
        base_url,
        "/order/battery/modeControl",
        {
            "deviceSn": device_sn,
            "batteryModeType": "GRID_CHARGE",
            "action": "on" if is_enable else "off",
        },
        wait_for_result=wait_for_result,
    )


async def async_update_battery_parameter(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
    parameter_type: str,
    value: float,
    *,
    wait_for_result: bool = True,
):
    """
    Set Deye battery parameter value.

    Deye's official Python sample uses the misspelled key 'paramterType'.
    This helper tries that first. If the API rejects it, it falls back to the
    corrected spelling 'parameterType'.
    """
    last_exc = None

    for field_name in ("paramterType", "parameterType"):
        payload = {
            "deviceSn": device_sn,
            field_name: parameter_type,
            "value": value,
        }

        try:
            return await async_post_control(
                session,
                token,
                base_url,
                "/order/battery/parameter/update",
                payload,
                wait_for_result=wait_for_result,
            )
        except Exception as exc:
            last_exc = exc
            _LOGGER.warning(
                "DeyeCloud battery parameter update failed using %s=%s. "
                "Will %s. Error: %s",
                field_name,
                parameter_type,
                "try fallback spelling" if field_name == "paramterType" else "raise",
                exc,
            )

    raise last_exc
