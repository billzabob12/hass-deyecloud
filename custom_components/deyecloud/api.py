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
