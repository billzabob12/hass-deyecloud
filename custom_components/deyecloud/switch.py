import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import async_get_token, async_control_grid_charge
from .button import _async_fetch_station_list, _async_fetch_inverter_devices
from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_BASE_URL,
    CONF_COMPANY_ID,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DeyeCloud grid charge switches."""
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

        inverter_devices = await _async_fetch_inverter_devices(
            session,
            token,
            base_url,
            station_ids,
        )

        for device in inverter_devices:
            sn = device["deviceSn"]
            entities.append(
                DeyeGridChargeSwitch(
                    hass,
                    username,
                    password,
                    app_id,
                    app_secret,
                    base_url,
                    company_id,
                    sn,
                )
            )

    except Exception as exc:
        _LOGGER.error("Error setting up Deye grid charge switches: %s", exc)

    async_add_entities(entities)


class DeyeGridChargeSwitch(SwitchEntity):
    """DeyeCloud grid charge switch."""

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
    ):
        self.hass = hass
        self._username = username
        self._password = password
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url
        self._company_id = company_id
        self._device_sn = device_sn

        self._attr_name = f"Deye Grid Charge {device_sn}"
        self._attr_unique_id = f"{device_sn}_grid_charge"
        self._attr_icon = "mdi:battery-charging"
        self._attr_assumed_state = True
        self._attr_is_on = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_sn)},
            "name": f"Deye Inverter {self._device_sn}",
            "manufacturer": "Deye",
            "model": "Inverter",
        }

    @property
    def is_on(self):
        return self._attr_is_on

    async def _async_send(self, enable: bool) -> None:
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

            response = await async_control_grid_charge(
                session,
                token,
                self._base_url,
                self._device_sn,
                enable,
            )

            _LOGGER.info(
                "Grid charge %s command sent for device %s: %s",
                "enable" if enable else "disable",
                self._device_sn,
                response,
            )

            self._attr_is_on = enable
            self.async_write_ha_state()

        except Exception as exc:
            raise HomeAssistantError(
                f"Failed to {'enable' if enable else 'disable'} Deye grid charge: {exc}"
            ) from exc

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_send(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_send(False)
