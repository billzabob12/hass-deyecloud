import hashlib
import aiohttp


def _sha256(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest().lower()


def _build_login_payload(login: str) -> dict[str, str]:
    """Build DeyeCloud login payload using either email or username.

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
    """Get DeyeCloud access token.

    If company_id is provided, DeyeCloud returns a business/company token.
    This is needed for installer/business accounts. Without company_id,
    DeyeCloud returns a personal-user token.
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
        j = await resp.json()
        if not j.get("success"):
            raise Exception(f"Token request failed: {j.get('msg')}")
        return j["accessToken"]


async def async_control_solar_sell(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
    is_enable,
):
    """Send Solar Sell control command."""
    url = f"{base_url}/order/sys/solarSell/control"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    action = "on" if is_enable else "off"

    payload = {
        "action": action,
        "deviceSn": device_sn,
    }

    async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
        resp.raise_for_status()
        return await resp.json()

async def async_post_control(
    session: aiohttp.ClientSession,
    token,
    base_url,
    path: str,
    payload: dict,
):
    """POST a DeyeCloud control command."""
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
        resp.raise_for_status()
        data = await resp.json()

    if not data.get("success", True):
        raise Exception(f"DeyeCloud control failed: {data.get('msg') or data}")

    return data


async def async_control_grid_charge(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
    is_enable: bool,
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
    )


async def async_update_battery_parameter(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
    parameter_type: str,
    value: float,
):
    """Set Deye battery parameter value."""
    return await async_post_control(
        session,
        token,
        base_url,
        "/order/battery/parameter/update",
        {
            "deviceSn": device_sn,
            # Deye's official sample uses the misspelled field name:
            "paramterType": parameter_type,
            "value": value,
        },
    )


async def async_get_order_result(
    session: aiohttp.ClientSession,
    token,
    base_url,
    order_id,
):
    """Get DeyeCloud command result."""
    url = f"{base_url}/order/{order_id}"
    headers = {"Authorization": f"Bearer {token}"}

    async with session.get(url, headers=headers, timeout=10) as resp:
        resp.raise_for_status()
        return await resp.json()
