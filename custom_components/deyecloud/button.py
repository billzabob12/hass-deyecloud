import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_BASE_URL,
    CONF_COMPANY_ID,
)
from .api import async_get_token, async_control_solar_sell

_LOGGER = logging.getLogger(__name__)


async def _async_fetch_station_list(session, token, base_url):
    """Fetch station list from DeyeCloud."""
    station_url = f"{base_url}/station/list"
    headers = {"Authorization": f"Bearer {token}"}

    async with session.post(station_url, headers=headers, json={}, timeout=10) as resp:
        resp.raise_for_status()
        data = await resp.json()

    if not data.get("success", True):
        raise Exception(f"Station list request failed: {data.get('msg')}")

    # DeyeCloud can return stationList: null for accounts without accessible
    # personal stations, especially installer/business accounts without companyId.
    return data.get("stationList") or []


async def _async_fetch_inverter_devices(session, token, base_url, station_ids):
    """Fetch inverter devices with pagination."""
    if not station_ids:
        return []

    device_url = f"{base_url}/station/device"
    headers = {"Authorization": f"Bearer {token}"}

    devices = []
    page = 1
    size = 100

    while True:
        payload = {
            "page": page,
            "size": size,
            "stationIds": station_ids,
        }

        async with session.post(device_url, headers=headers, json=payload, timeout=10) as resp:
            resp.raise_for_status()
            device_response = await resp.json()

        if not device_response.get("success", True):
            raise Exception(f"Device list request failed: {device_response.get('msg')}")

        # Avoid NoneType iteration if API returns deviceListItems: null.
        page_items = device_response.get("deviceListItems") or []
        devices.extend(page_items)

        total = device_response.get("total") or device_response.get("totalCount")
        if total is not None and len(devices) >= int(total):
            break

        if len(page_items) < size:
            break

        page += 1

    return [
        device
        for device in devices
        if device.get("deviceType") == "INVERTER" and device.get("deviceSn")
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solar Sell buttons dynamically."""
    config = entry.data

    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    app_id = config.get(CONF_APP_ID)
    app_secret = config.get(CONF_APP_SECRET)
    base_url = config.get(CONF_BASE_URL)
    company_id = config.get(CONF_COMPANY_ID)

    session = async_get_clientsession(hass)
    entities = []

    try:
        token = await async_get_token(
            session,
            username,
            password,
            app_id,
            app_secret,
            base_url,
            company_id,
        )

        stations_data = await _async_fetch_station_list(session, token, base_url)
        station_ids = [
            st.get("id") or st.get("stationId")
            for st in stations_data
            if st.get("id") or st.get("stationId")
        ]

        if not station_ids:
            _LOGGER.warning(
                "No DeyeCloud stations found for button setup. "
                "If this is an installer/business account, configure company_id."
            )

        inverter_devices = await _async_fetch_inverter_devices(
            session,
            token,
            base_url,
            station_ids,
        )

        for device in inverter_devices:
            sn = device["deviceSn"]

            entities.append(DeyeSolarSellButton(
                hass,
                username,
                password,
                app_id,
                app_secret,
                base_url,
                company_id,
                sn,
                "Enable",
                True,
                "mdi:solar-power",
            ))

            entities.append(DeyeSolarSellButton(
                hass,
                username,
                password,
                app_id,
                app_secret,
                base_url,
                company_id,
                sn,
                "Disable",
                False,
                "mdi:solar-power-variant-outline",
            ))

            _LOGGER.info("Created Solar Sell buttons for device: %s", sn)

    except Exception as exc:
        _LOGGER.error("Error setting up Deye buttons: %s", exc)

    async_add_entities(entities)


class DeyeSolarSellButton(ButtonEntity):
    """DeyeCloud Solar Sell control button."""

    def __init__(
        self,
        hass,
        username,
        password,
        app_id,
        app_secret,
        base_url,
        company_id,
        device_sn,
        action_name,
        is_enable,
        icon,
    ):
        self.hass = hass
        self._username = username
        self._password = password
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url
        self._company_id = company_id
        self._device_sn = device_sn
        self._is_enable = is_enable

        self._attr_name = f"Deye Solar Sell {action_name}"
        self._attr_unique_id = f"{device_sn}_solar_sell_{action_name.lower()}_btn"
        self._attr_icon = icon

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._device_sn)},
            "name": f"Deye Inverter {self._device_sn}",
            "manufacturer": "Deye",
            "model": "Inverter",
        }

    async def async_press(self) -> None:
        """Handle button press."""
        session = async_get_clientsession(self.hass)

        try:
            token = await async_get_token(
                session,
                self._username,
                self._password,
                self._app_id,
                self._app_secret,
                self._base_url,
                self._company_id,
            )

            await async_control_solar_sell(
                session,
                token,
                self._base_url,
                self._device_sn,
                self._is_enable,
            )

            _LOGGER.info(
                "Solar Sell %s command sent for device %s",
                "enable" if self._is_enable else "disable",
                self._device_sn,
            )

        except Exception as exc:
            _LOGGER.error("Failed to press button %s: %s", self.name, exc)
