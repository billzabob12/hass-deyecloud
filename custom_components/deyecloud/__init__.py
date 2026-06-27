"""DeyeCloud integration."""

from __future__ import annotations

# Import platform modules at module load time.
#
# Home Assistant 2024.7+ can warn when platform modules are first imported
# from inside async_forward_entry_setups(), because importlib/import_module
# may do blocking disk I/O inside the event loop.
#
# Keeping these imports here pre-loads the platform modules when the integration
# itself is loaded, so async_forward_entry_setups() can reuse already-imported
# modules.
from . import button as _button  # noqa: F401
from . import number as _number  # noqa: F401
from . import sensor as _sensor  # noqa: F401
from . import switch as _switch  # noqa: F401

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.NUMBER,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the DeyeCloud integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DeyeCloud from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a DeyeCloud config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reload the DeyeCloud config entry."""
    unload_ok = await async_unload_entry(hass, entry)

    if not unload_ok:
        return False

    return await async_setup_entry(hass, entry)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
