"""DeyeCloud number entities."""

import logging
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import async_get_token, async_update_battery_parameter
from .button import _async_fetch_inverter_devices, _async_fetch_station_list
from .const import (
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_BASE_URL,
    CONF_COMPANY_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

MAX_CHARGE_CURRENT = "MAX_CHARGE_CURRENT"
MAX_DISCHARGE_CURRENT = "MAX_DISCHARGE_CURRENT"
GRID_CHARGE_AMPERE = "GRID_CHARGE_AMPERE"


@dataclass(frozen=True)
class DeyeBatteryNumberDescription:
    """Description for a Deye battery number entity."""

    key: str
    name: str
    parameter_types: tuple[str, ...]
    icon: str
    min_value: float = 0
    max_value: float = 250
    step: float = 1


NUMBER_DESCRIPTIONS: tuple[DeyeBatteryNumberDescription, ...] = (
    DeyeBatteryNumberDescription(
        key="max_battery_charge_current",
        name="Max Battery Charge Current",
        parameter_types=(MAX_CHARGE_CURRENT,),
        icon="mdi:battery-arrow-up",
        min_value=0,
        max_value=250,
        step=1,
    ),
    DeyeBatteryNumberDescription(
        key="max_battery_discharge_current",
        name="Max Battery Discharge Current",
        parameter_types=(MAX_DISCHARGE_CURRENT,),
        icon="mdi:battery-arrow-down",
        min_value=0,
        max_value=250,
        step=1,
    ),
    DeyeBatteryNumberDescription(
        key="grid_charge_current",
        name="Grid Charge Current",
        parameter_types=(GRID_CHARGE_AMPERE,),
        icon="mdi:transmission-tower-import",
        min_value=0,
        max_value=250,
        step=1,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DeyeCloud battery/grid current number entities."""
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
            station.get("id") or station.get("stationId")
            for station in stations_data
            if station.get("id") or station.get("stationId")
        ]

        if not station_ids:
            _LOGGER.warning(
                "No DeyeCloud stations found for number setup. "
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

            for description in NUMBER_DESCRIPTIONS:
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
                        description,
                    )
                )

            _LOGGER.info("Created battery/grid current number entities for device: %s", sn)

    except Exception as exc:
        _LOGGER.error("Error setting up Deye battery/grid current numbers: %s", exc)

    async_add_entities(entities)


class DeyeBatteryCurrentNumber(NumberEntity):
    """DeyeCloud battery/grid current limit number."""

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
        description: DeyeBatteryNumberDescription,
    ):
        self.hass = hass
        self._username = username
        self._password = password
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url
        self._company_id = company_id
        self._device_sn = device_sn
        self._description = description

        self._attr_name = f"Deye {description.name} {device_sn}"
        self._attr_unique_id = f"{device_sn}_{description.key}"
        self._attr_icon = description.icon

        self._attr_native_unit_of_measurement = "A"
        self._attr_native_min_value = description.min_value
        self._attr_native_max_value = description.max_value
        self._attr_native_step = description.step
        self._attr_mode = "box"

        # Unknown until this integration sends a command.
        # Later this could be initialised from DeyeCloud /config/battery.
        self._attr_native_value = None

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._device_sn)},
            "name": f"Deye Inverter {self._device_sn}",
            "manufacturer": "Deye",
            "model": "Inverter",
        }

    async def async_set_native_value(self, value: float) -> None:
        """Set Deye battery/grid current value and wait for confirmation."""
        session = async_get_clientsession(self.hass)
        last_exc = None

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

            for parameter_type in self._description.parameter_types:
                try:
                    response = await async_update_battery_parameter(
                        session,
                        token,
                        self._base_url,
                        self._device_sn,
                        parameter_type,
                        value,
                        wait_for_result=True,
                    )

                    _LOGGER.info(
                        "Confirmed %s set to %sA for device %s using %s: %s",
                        self._description.name,
                        value,
                        self._device_sn,
                        parameter_type,
                        response,
                    )

                    self._attr_native_value = value
                    self.async_write_ha_state()
                    return

                except Exception as exc:
                    last_exc = exc
                    _LOGGER.warning(
                        "Failed to set %s for device %s using parameter type %s: %s",
                        self._description.name,
                        self._device_sn,
                        parameter_type,
                        exc,
                    )

            raise last_exc

        except Exception as exc:
            raise HomeAssistantError(
                f"Failed to set {self._description.name} to {value}A "
                f"for {self._device_sn}: {exc}"
            ) from exc
