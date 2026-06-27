import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import async_get_token, async_update_battery_parameter
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

MAX_CHARGE_CURRENT = "MAX_CHARGE_CURRENT"
MAX_DISCHARGE_CURRENT = "MAX_DISCHARGE_CURRENT"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DeyeCloud battery current number entities."""
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
                DeyeBatteryCurrentNumber(
                    hass,
                    username,
                    password,
                    app_id,
                    app_secret,
                    base_url,
                    company_id,
                    sn,
                    "Max Battery Charge Current",
                    MAX_CHARGE_CURRENT,
                    "mdi:battery-arrow-up",
                )
            )

            entities.append(
                DeyeBatteryCurrentNumber(
                    hass,
                    username,
                    password,
                    app_id,
                    app_secret,
                    base_url,
                    company_id,
                    sn,
                    "Max Battery Discharge Current",
                    MAX_DISCHARGE_CURRENT,
                    "mdi:battery-arrow-down",
                )
            )

    except Exception as exc:
        _LOGGER.error("Error setting up Deye battery current numbers: %s", exc)

    async_add_entities(entities)


class DeyeBatteryCurrentNumber(NumberEntity):
    """DeyeCloud battery current limit number."""

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
        name,
        parameter_type,
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
        self._parameter_type = parameter_type

        self._attr_name = f"Deye {name} {device_sn}"
        self._attr_unique_id = f"{device_sn}_{parameter_type.lower()}"
        self._attr_icon = icon

        self._attr_native_unit_of_measurement = "A"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 250
        self._attr_native_step = 1
        self._attr_mode = "box"

        # Unknown until HA sets it, unless you later add /config/battery readback.
        self._attr_native_value = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_sn)},
            "name": f"Deye Inverter {self._device_sn}",
            "manufacturer": "Deye",
            "model": "Inverter",
        }

    async def async_set_native_value(self, value: float) -> None:
        """Set Deye battery current limit."""
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

            response = await async_update_battery_parameter(
                session,
                token,
                self._base_url,
                self._device_sn,
                self._parameter_type,
                value,
            )

            _LOGGER.info(
                "Set %s to %sA for device %s: %s",
                self._parameter_type,
                value,
                self._device_sn,
                response,
            )

            self._attr_native_value = value
            self.async_write_ha_state()

        except Exception as exc:
            raise HomeAssistantError(
                f"Failed to set {self._parameter_type} to {value}A: {exc}"
            ) from exc
