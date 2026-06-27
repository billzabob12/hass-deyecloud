import logging
from datetime import timedelta, datetime, date
from dateutil.relativedelta import relativedelta
import hashlib
import asyncio

import aiohttp

from homeassistant.util import dt as dt_util
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
    UpdateFailed,
)

from .api import async_get_battery_config
from .battery_parameters import BATTERY_PARAMETER_DESCRIPTIONS, extract_battery_parameter

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_BASE_URL,
    CONF_START_MONTH,
    CONF_COMPANY_ID,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)
HISTORY_REFRESH_INTERVAL = timedelta(hours=6)
HISTORY_START_MONTH = "2024-01"

_RELATIVE_DAY_OFFSETS = {
    "today": 0,
    "yesterday": 1,
    "day_before": 2,
}

_DAILY_LABELS = {
    "day_before": "Day Before Yesterday",
    "yesterday": "Yesterday",
    "today": "Today",
}


_DAILY_ZERO_RECORD_KEYS = (
    "generationValue",
    "consumptionValue",
    "gridValue",
    "purchaseValue",
    "chargeValue",
    "dischargeValue",
)


def _empty_daily_record(day: str) -> dict:
    """Return an explicit zero daily record for a date.

    This is used right after midnight when DeyeCloud has not yet published
    the new day's daily record. It prevents Today sensors from carrying
    yesterday's final values into the new day or becoming Unknown.
    """
    record = {"date": day}
    for key in _DAILY_ZERO_RECORD_KEYS:
        record[key] = 0.0
    return record


# DeyeCloud can lag right after local midnight and may still return the
# previous daily bucket for a short time. During this window, never expose a
# previous-day aggregate as the new day's "Today" value, because Home
# Assistant may record it into the new day's Energy Dashboard statistics.
_MIDNIGHT_STALE_GUARD = timedelta(hours=2)
_FLOAT_EPSILON = 0.001


def _parse_api_date(value) -> date | None:
    """Parse a DeyeCloud date-like value into a date if possible."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    # Common API formats: YYYY-MM-DD, YYYY-MM-DD HH:MM:SS,
    # YYYY-MM-DDTHH:MM:SS..., YYYY/MM/DD.
    text = text.replace("/", "-")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _numeric_value(record: dict | None, key: str) -> float | None:
    """Return a numeric value from a daily/monthly record if possible."""
    if not record:
        return None
    try:
        value = record.get(key)
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _records_look_like_same_daily_bucket(record: dict | None, reference: dict | None) -> bool:
    """Detect a stale daily record that is likely copied from yesterday.

    Around midnight DeyeCloud may return yesterday's final values while Home
    Assistant is already on the new local date. If today's candidate has the
    same non-zero energy totals as yesterday, treat it as stale and publish 0
    until DeyeCloud exposes a real current-day bucket.
    """
    if not record or not reference:
        return False

    matched_non_zero_values = 0
    for key in _DAILY_ZERO_RECORD_KEYS:
        current = _numeric_value(record, key)
        previous = _numeric_value(reference, key)
        if current is None or previous is None:
            continue
        if previous > _FLOAT_EPSILON and abs(current - previous) <= _FLOAT_EPSILON:
            matched_non_zero_values += 1

    # One exact match can happen naturally; two or more across independent
    # energy counters strongly indicates the cloud returned the old bucket.
    return matched_non_zero_values >= 2


def _is_midnight_guard_window(now: datetime) -> bool:
    """Return True during the local post-midnight stale-data guard window."""
    start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
    return now - start < _MIDNIGHT_STALE_GUARD


def _select_daily_record(
    daily_items: list[dict],
    day: str,
    *,
    allow_undated_fallback: bool,
) -> dict | None:
    """Select only the record that actually belongs to the requested day."""
    target = datetime.strptime(day, "%Y-%m-%d").date()
    has_date_field = False

    for item in daily_items:
        item_date = _parse_api_date(
            item.get("date")
            or item.get("time")
            or item.get("timestamp")
            or item.get("collectionTime")
        )
        if item_date is None:
            continue
        has_date_field = True
        if item_date == target:
            return item

    # Some DeyeCloud responses may contain exactly one requested-day record
    # without a date field. This fallback is intentionally disabled for Today
    # during the post-midnight guard window to avoid mapping yesterday into
    # the new day.
    if allow_undated_fallback and not has_date_field and len(daily_items) == 1:
        return daily_items[0]

    return None


def _resolve_daily_date_key(date_key: str) -> str:
    """Convert relative day key to YYYY-MM-DD using HA timezone."""
    if date_key in _RELATIVE_DAY_OFFSETS:
        d = dt_util.now().date() - timedelta(days=_RELATIVE_DAY_OFFSETS[date_key])
        return d.isoformat()
    return date_key


def _sha256(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest().lower()


def _build_login_payload(login: str) -> dict[str, str]:
    """Build DeyeCloud login payload using either email or username.

    DeyeCloud token API supports login by mobile, email, or username. This
    integration has a single username/login config field, so choose the payload
    key based on the entered value.
    """
    login = login.strip()
    if "@" in login:
        return {"email": login}
    return {"username": login}


def _as_list(value):
    """Return value if it is a list, otherwise return an empty list."""
    return value if isinstance(value, list) else []


def _as_float_or_original(value):
    """Return numeric values as float, otherwise return original value."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _normalize_unit(unit: str | None) -> str | None:
    """Normalize common API units for Home Assistant."""
    if unit == "C":
        return "°C"
    return unit


def _validate_history_start_month(value: str | None) -> str:
    """Validate YYYY-MM start month."""
    if not value:
        return "2024-01"
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError:
        _LOGGER.warning("Invalid start month %s, falling back to 2024-01", value)
        return "2024-01"
    return value


async def _post_json(session: aiohttp.ClientSession, url: str, *, headers=None, payload=None, timeout=10):
    """POST JSON with one retry for temporary network/server errors."""
    last_exc = None
    for attempt in range(2):
        try:
            async with session.post(url, headers=headers, json=payload or {}, timeout=timeout) as resp:
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt == 1:
                break
            await asyncio.sleep(1)
    raise last_exc


async def _async_get_token(
    session: aiohttp.ClientSession,
    username,
    password,
    app_id,
    app_secret,
    base_url,
    company_id=None,
):
    url = f"{base_url}/account/token?appId={app_id}"
    _LOGGER.debug("Requesting token from API: %s", url)
    payload = {
        "appSecret": app_secret,
        **_build_login_payload(username),
        "password": _sha256(password),
    }

    if company_id:
        payload["companyId"] = str(company_id).strip()

    j = await _post_json(session, url, payload=payload, timeout=10)
    if not j.get("success"):
        _LOGGER.error("Token request failed: %s", j.get("msg"))
        raise Exception(f"Token request failed: {j.get('msg')}")

    _LOGGER.debug("Token request successful")
    return j["accessToken"]


async def _async_station_list(session, token, base_url):
    url = f"{base_url}/station/list"
    _LOGGER.debug("Fetching station list from API: %s", url)
    headers = {"Authorization": f"Bearer {token}"}

    j = await _post_json(session, url, headers=headers, payload={}, timeout=10)
    if not j.get("success", True):
        _LOGGER.error("Station list request failed: %s", j.get("msg"))
        raise Exception(f"Station list request failed: {j.get('msg')}")

    # DeyeCloud can return stationList: null for installer/business accounts
    # when companyId is missing or the account has no accessible stations.
    stations = _as_list(j.get("stationList"))
    _LOGGER.info("Received %d stations from API", len(stations))
    return stations


async def _async_history(session, token, station_id, base_url):
    """Fetch monthly history from HISTORY_START_MONTH to current month."""
    url = f"{base_url}/station/history"
    headers = {"Authorization": f"Bearer {token}"}
    items: list[dict] = []

    start_dt = datetime.strptime(HISTORY_START_MONTH, "%Y-%m")
    start: date = start_dt.date().replace(day=1)
    end: date = dt_util.now().date().replace(day=1)

    _LOGGER.debug(
        "Fetching monthly history for station_id %s from %s to %s",
        station_id,
        start.strftime("%Y-%m"),
        end.strftime("%Y-%m"),
    )

    while start <= end:
        range_start: date = start
        range_end: date = min(range_start + relativedelta(months=11), end)

        payload = {
            "stationId": station_id,
            "granularity": 3,
            "startAt": range_start.strftime("%Y-%m"),
            "endAt": range_end.strftime("%Y-%m"),
        }

        j = await _post_json(session, url, headers=headers, payload=payload, timeout=10)
        if not j.get("success"):
            _LOGGER.error("Monthly history request failed for station_id %s: %s", station_id, j.get("msg"))
            raise Exception(f"History request failed: {j.get('msg')}")
        items.extend(_as_list(j.get("stationDataItems")))

        start = range_end + relativedelta(months=1)

    _LOGGER.debug("Received %d monthly records for station_id %s", len(items), station_id)
    return items


async def _async_daily_history(session, token, station_id, base_url, start_date, end_date):
    url = f"{base_url}/station/history"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "stationId": station_id,
        "granularity": 2,
        "startAt": start_date,
        "endAt": end_date,
    }
    _LOGGER.debug("Fetching daily data for station_id %s from %s to %s", station_id, start_date, end_date)

    j = await _post_json(session, url, headers=headers, payload=payload, timeout=10)
    if not j.get("success"):
        _LOGGER.error("Daily history request failed for station_id %s: %s", station_id, j.get("msg"))
        raise Exception(f"Daily history request failed: {j.get('msg')}")

    items = _as_list(j.get("stationDataItems"))
    _LOGGER.debug("Received %d daily records for station_id %s", len(items), station_id)
    return items


async def _async_get_device_list(session, token, base_url, stations):
    url = f"{base_url}/station/device"
    _LOGGER.debug("Fetching device list from API: %s", url)
    headers = {"Authorization": f"Bearer {token}"}
    station_ids = [
        st.get("id") or st.get("stationId")
        for st in _as_list(stations)
        if st.get("id") or st.get("stationId")
    ]
    if not station_ids:
        _LOGGER.warning("No stationIds available for request")
        return []

    page = 1
    size = 100
    devices = []

    while True:
        payload = {
            "page": page,
            "size": size,
            "stationIds": station_ids,
        }
        _LOGGER.debug("Sending device payload: %s", payload)

        j = await _post_json(session, url, headers=headers, payload=payload, timeout=10)
        if not j.get("success"):
            _LOGGER.error("Device list request failed: %s", j.get("msg"))
            raise Exception(f"Device list request failed: {j.get('msg')}")

        page_items = _as_list(j.get("deviceListItems"))
        devices.extend(page_items)

        # Stop when API returns fewer than requested. If the API exposes total, honor it too.
        total = j.get("total") or j.get("totalCount")
        if total is not None and len(devices) >= int(total):
            break
        if len(page_items) < size:
            break

        page += 1

    return [item["deviceSn"] for item in devices if item.get("deviceType") == "INVERTER" and item.get("deviceSn")]


async def _async_get_device_status(session, token, base_url, device_list):
    if not device_list:
        return []

    url = f"{base_url}/device/latest"
    _LOGGER.debug("Fetching device status from API: %s with devices: %s", url, device_list)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"deviceList": device_list}

    j = await _post_json(session, url, headers=headers, payload=payload, timeout=10)
    if not j.get("success"):
        _LOGGER.error("Device status request failed: %s", j.get("msg"))
        raise Exception(f"Device status request failed: {j.get('msg')}")

    _LOGGER.debug("Received device status: %s", j)
    return _as_list(j.get("deviceDataList"))


class DeyeCloudCoordinator(DataUpdateCoordinator):
    """Coordinator for Deye Cloud data updates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        super().__init__(
            hass,
            _LOGGER,
            name="Deye Cloud",
            update_interval=SCAN_INTERVAL,
        )
        self.entry = entry
        self.session = async_get_clientsession(hass)
        self.token = None
        self.token_expiry = None
        self._history_cache: dict[str, list[dict]] = {}
        self._history_last_update = None

    async def _async_update_data(self) -> dict:
        """Fetch data from API."""
        username = self.entry.data[CONF_USERNAME]
        password = self.entry.data[CONF_PASSWORD]
        app_id = self.entry.data[CONF_APP_ID]
        app_secret = self.entry.data[CONF_APP_SECRET]
        base_url = self.entry.data[CONF_BASE_URL]
        company_id = self.entry.data.get(CONF_COMPANY_ID)

        now_utc = dt_util.utcnow()
        if not self.token or not self.token_expiry or self.token_expiry <= now_utc:
            try:
                self.token = await _async_get_token(
                    self.session,
                    username,
                    password,
                    app_id,
                    app_secret,
                    base_url,
                    company_id,
                )
                # Keep conservative expiry. If API provides expiresIn, replace this with API value.
                self.token_expiry = dt_util.utcnow() + timedelta(minutes=25)
                _LOGGER.debug("Token refreshed, valid until %s", self.token_expiry)
            except Exception as exc:
                raise UpdateFailed(f"Token refresh failed: {exc}") from exc

        try:
            stations = await _async_station_list(self.session, self.token, base_url)
            if not stations:
                raise UpdateFailed("No stations found")
        except Exception as exc:
            raise UpdateFailed(f"Error fetching stations: {exc}") from exc

        station_tasks = []
        for station in stations:
            raw_station_id = station.get("id") or station.get("stationId")
            if raw_station_id:
                station_id = str(raw_station_id)
                station_tasks.append(self._async_update_station_data(self.session, station_id, base_url, station))

        station_data = {}
        results = await asyncio.gather(*station_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                _LOGGER.error("Error updating station data: %s", result)
            elif result:
                station_id, data = result
                station_data[station_id] = data

        return station_data

    async def _get_monthly_history_cached(self, session, station_id, base_url):
        """Return monthly history, refreshing cache only periodically."""
        now = dt_util.now()
        cached_history = self._history_cache.get(station_id, [])
        current_month_present = any(
            record.get("year") == now.year and record.get("month") == now.month
            for record in cached_history
        )
        needs_refresh = (
            station_id not in self._history_cache
            or self._history_last_update is None
            or now - self._history_last_update > HISTORY_REFRESH_INTERVAL
            # At month rollover, refresh sooner than the normal 6-hour cache,
            # but avoid hammering the API every minute if the cloud has not
            # published the new month yet.
            or (
                not current_month_present
                and now - self._history_last_update > timedelta(minutes=10)
            )
        )

        if needs_refresh:
            self._history_cache[station_id] = await _async_history(session, self.token, station_id, base_url)
            self._history_last_update = now

        return self._history_cache.get(station_id, [])

    async def _async_update_station_data(self, session, station_id, base_url, station_info):
        """Fetch data for a single station."""
        previous_station_data = (self.data or {}).get(station_id, {})
        previous_daily = previous_station_data.get("daily", {})

        data = {
            "info": station_info,
            "history": [],
            "daily": dict(previous_daily),
            "devices": {},
            "battery_config": {},
        }

        # Monthly history should not break daily/device updates if it fails.
        try:
            data["history"] = await self._get_monthly_history_cached(session, station_id, base_url)
        except Exception as exc:
            _LOGGER.error("Error updating monthly history for station %s: %s", station_id, exc)
            data["history"] = self._history_cache.get(station_id, [])

        # If DeyeCloud has not published the new current-month bucket yet,
        # expose an explicit 0 record instead of Unknown/stale cache data.
        now_local = dt_util.now()
        if not any(
            record.get("year") == now_local.year and record.get("month") == now_local.month
            for record in data["history"]
        ):
            current_month_record = {"year": now_local.year, "month": now_local.month}
            for key in _DAILY_ZERO_RECORD_KEYS:
                current_month_record[key] = 0.0
            data["history"] = [*data["history"], current_month_record]

        # Fetch daily data.
        #
        # DeyeCloud behaves inconsistently here:
        # - Today often needs endAt = next day to expose the in-progress bucket.
        # - Some accounts/API regions do not return older daily buckets reliably
        #   when each day is requested as an isolated one-day range. In v2.0.1
        #   this made all "Day Before Yesterday" sensors Unknown on first
        #   refresh because there was no previous coordinator cache to preserve.
        #
        # Therefore fetch a small rolling window first, then use per-day requests
        # only as a fallback. Strict date matching is still kept so stale
        # yesterday data is never mapped into Today.
        try:
            today_date = dt_util.now().date()
            days = [
                today_date - timedelta(days=2),
                today_date - timedelta(days=1),
                today_date,
            ]

            range_daily_items = []
            try:
                range_daily_items = await _async_daily_history(
                    session,
                    self.token,
                    station_id,
                    base_url,
                    days[0].isoformat(),
                    (today_date + timedelta(days=1)).isoformat(),
                )
            except Exception as exc:
                _LOGGER.debug(
                    "Daily history rolling-window request failed for station %s: %s",
                    station_id,
                    exc,
                )

            for d in days:
                day = d.isoformat()
                next_day = d + timedelta(days=1)
                next_day_str = next_day.isoformat()

                now = dt_util.now()
                in_midnight_guard = d == today_date and _is_midnight_guard_window(now)

                matched_item = _select_daily_record(
                    range_daily_items,
                    day,
                    allow_undated_fallback=False,
                )

                daily_items = []
                if matched_item is None:
                    # Fallback request: this is the format that returns Today
                    # data on normal days and also covers accounts where the
                    # rolling-window endpoint is sparse.
                    try:
                        daily_items = await _async_daily_history(
                            session, self.token, station_id, base_url, day, next_day_str
                        )
                    except Exception as exc:
                        _LOGGER.debug(
                            "Daily history primary request failed for station %s day %s: %s",
                            station_id,
                            day,
                            exc,
                        )

                    # Same-day fallback for month-end or API edge cases.
                    if not daily_items:
                        try:
                            daily_items = await _async_daily_history(
                                session, self.token, station_id, base_url, day, day
                            )
                        except Exception as exc:
                            _LOGGER.debug(
                                "Daily history fallback request failed for station %s day %s: %s",
                                station_id,
                                day,
                                exc,
                            )

                    if not daily_items:
                        if d == today_date:
                            # At midnight DeyeCloud may not have a valid record
                            # for the new day yet. Today should start from 0,
                            # not from yesterday's final value and not as
                            # Unknown.
                            data["daily"][day] = _empty_daily_record(day)
                        # Otherwise keep same-date cached value if available.
                        continue

                    matched_item = _select_daily_record(
                        daily_items,
                        day,
                        allow_undated_fallback=not in_midnight_guard,
                    )

                if matched_item is not None and d == today_date and in_midnight_guard:
                    yesterday_key = (today_date - timedelta(days=1)).isoformat()
                    yesterday_record = data["daily"].get(yesterday_key)
                    if _records_look_like_same_daily_bucket(matched_item, yesterday_record):
                        _LOGGER.debug(
                            "Ignoring stale DeyeCloud daily record for station %s day %s during midnight guard",
                            station_id,
                            day,
                        )
                        matched_item = _empty_daily_record(day)

                if matched_item is not None:
                    data["daily"][day] = matched_item
                elif d == today_date:
                    # API returned only older/foreign/undated records. Do not map
                    # them into Today, otherwise HA can record yesterday's total
                    # into the new Energy Dashboard day.
                    data["daily"][day] = _empty_daily_record(day)
                # Else keep same-date cached value if available.
        except Exception as exc:
            _LOGGER.error("Error updating daily history for station %s: %s", station_id, exc)

        # Device updates should still run even if history fails.
        try:
            device_sns = await _async_get_device_list(session, self.token, base_url, [station_info])
            if device_sns:
                device_status = await _async_get_device_status(session, self.token, base_url, device_sns)
                for device in device_status:
                    sn = device.get("deviceSn")
                    if sn:
                        data["devices"][str(sn)] = device
        
                battery_config_results = await asyncio.gather(
                    *[
                        async_get_battery_config(session, self.token, base_url, device_sn)
                        for device_sn in device_sns
                    ],
                    return_exceptions=True,
                )
        
                for device_sn, result in zip(device_sns, battery_config_results):
                    if isinstance(result, Exception):
                        _LOGGER.warning(
                            "Error updating battery config for device %s: %s",
                            device_sn,
                            result,
                        )
                        continue
        
                    data["battery_config"][str(device_sn)] = result
        
        except Exception as exc:
            _LOGGER.error("Error updating devices for station %s: %s", station_id, exc)

        return (station_id, data)


class DeyeCloudSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Deye Cloud Sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DeyeCloudCoordinator,
        sensor_type: str,
        name: str,
        unique_id: str,
        unit: str | None = None,
        device_class: str | None = None,
        state_class: str | None = None,
        extra_attributes: dict | None = None,
        station_id: str | None = None,
        date_key: str | None = None,
        metric_key: str | None = None,
        device_sn: str | None = None,
        device_key: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._sensor_type = sensor_type
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_native_unit_of_measurement = _normalize_unit(unit)
        if device_class:
            self._attr_device_class = device_class
        if state_class:
            self._attr_state_class = state_class
        self._extra_attributes = extra_attributes or {}
        self._station_id = str(station_id) if station_id is not None else None
        self._date_key = date_key
        self._metric_key = metric_key
        self._device_sn = str(device_sn) if device_sn is not None else None
        self._device_key = device_key

    @property
    def native_value(self):
        """Return the sensor value."""
        if not self.coordinator.data or not self._station_id:
            return None

        station_data = self.coordinator.data.get(self._station_id)
        if not station_data:
            return None

        try:
            if self._sensor_type == "monthly_raw":
                year, month = map(int, self._date_key.split("_"))
                for record in station_data.get("history", []):
                    if record.get("year") == year and record.get("month") == month:
                        return _as_float_or_original(record.get("generationValue"))

            elif self._sensor_type == "monthly_metric":
                if self._date_key == "current":
                    target = dt_util.now()
                else:
                    target = dt_util.now() - relativedelta(months=1)

                for record in station_data.get("history", []):
                    if record.get("year") == target.year and record.get("month") == target.month:
                        return _as_float_or_original(record.get(self._metric_key))

            elif self._sensor_type == "daily":
                date_str = _resolve_daily_date_key(self._date_key)
                daily_data = station_data.get("daily", {}).get(date_str, {})
                return _as_float_or_original(daily_data.get(self._metric_key))

            elif self._sensor_type == "device":
                device_data = station_data.get("devices", {}).get(self._device_sn, {})
                for data_item in device_data.get("dataList") or []:
                    if data_item.get("key") == self._device_key:
                        return _as_float_or_original(data_item.get("value"))

        except (KeyError, ValueError, TypeError) as exc:
            _LOGGER.error("Error extracting value for %s: %s", self.unique_id, exc)

        return None

    @property
    def device_info(self):
        """Return device information."""
        if self._device_sn:
            return {
                "identifiers": {(DOMAIN, self._device_sn)},
                "name": f"Deye Inverter {self._device_sn}",
                "manufacturer": "Deye",
                "model": "Inverter",
            }

        if self._station_id:
            return {
                "identifiers": {(DOMAIN, f"station_{self._station_id}")},
                "name": f"Deye Station {self._station_id}",
                "manufacturer": "Deye",
                "model": "Station",
            }

        return None

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        attrs = self._extra_attributes.copy()

        if self._station_id:
            attrs["station_id"] = self._station_id

        if self._date_key:
            if self._sensor_type == "monthly_raw":
                attrs["year"] = int(self._date_key.split("_")[0])
                attrs["month"] = int(self._date_key.split("_")[1])
            elif self._sensor_type == "monthly_metric":
                if self._date_key == "current":
                    target = dt_util.now()
                else:
                    target = dt_util.now() - relativedelta(months=1)
                attrs["year"] = target.year
                attrs["month"] = target.month
                attrs["metric_key"] = self._metric_key
            elif self._sensor_type == "daily":
                attrs["relative_day"] = self._date_key
                attrs["date"] = _resolve_daily_date_key(self._date_key)

        if self._device_sn:
            attrs["device_sn"] = self._device_sn

        return attrs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up Deye Cloud sensors from a config entry."""
    _LOGGER.info("Setting up DeyeCloud integration")

    global HISTORY_START_MONTH
    HISTORY_START_MONTH = _validate_history_start_month(entry.data.get(CONF_START_MONTH, "2024-01"))
    _LOGGER.debug("HISTORY_START_MONTH set to: %s", HISTORY_START_MONTH)

    coordinator = DeyeCloudCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entities = []

    _MONTHLY_METRICS = [
        ("generationValue", "Solar Generation"),
        ("consumptionValue", "Monthly Consumption"),
        ("gridValue", "Monthly Grid Export"),
        ("purchaseValue", "Monthly Grid Import"),
        ("chargeValue", "Monthly Battery Charge"),
        ("dischargeValue", "Monthly Battery Discharge"),
    ]

    _DAILY_METRICS = [
        ("generationValue", "Solar Generation"),
        ("consumptionValue", "Daily Consumption"),
        ("gridValue", "Daily Grid Export"),
        ("purchaseValue", "Daily Grid Import"),
        ("chargeValue", "Daily Battery Charge"),
        ("dischargeValue", "Daily Battery Discharge"),
    ]

    for station_id, station_data in coordinator.data.items():
        station_id = str(station_id)

        # Historical monthly generation sensors. Note: this still only creates entities
        # for months available at setup time, preserving the original behavior.
        for record in station_data.get("history", []):
            y = record.get("year")
            m = record.get("month")
            if not y or not m:
                continue

            month_name = datetime(year=y, month=m, day=1).strftime("%b %Y")
            name = f"Deye {station_id} {month_name}"
            uid = f"{station_id}_raw_{y}_{m:02d}"

            entities.append(DeyeCloudSensor(
                coordinator=coordinator,
                sensor_type="monthly_raw",
                name=name,
                unique_id=uid,
                unit="kWh",
                device_class="energy",
                # Historical monthly period total. Kept as total because this
                # entity represents a fixed period value, not a live meter.
                state_class="total",
                station_id=station_id,
                date_key=f"{y}_{m}",
                extra_attributes=record,
            ))

        # Current and last month metrics.
        for metric_key, metric_name in _MONTHLY_METRICS:
            name = f"{metric_name} {station_id}"
            uid = f"{station_id}_{metric_key}_current_month"
            entities.append(DeyeCloudSensor(
                coordinator=coordinator,
                sensor_type="monthly_metric",
                name=name,
                unique_id=uid,
                unit="kWh",
                device_class="energy",
                # Current-month DeyeCloud values reset at month boundary and
                # then increase again. total_increasing lets HA statistics
                # treat that decrease as a new meter cycle instead of a
                # negative energy delta.
                state_class="total_increasing",
                station_id=station_id,
                date_key="current",
                metric_key=metric_key,
                extra_attributes={"metric": metric_name},
            ))

            name = f"{metric_name} Last Month {station_id}"
            uid = f"{station_id}_{metric_key}_last_month"
            entities.append(DeyeCloudSensor(
                coordinator=coordinator,
                sensor_type="monthly_metric",
                name=name,
                unique_id=uid,
                unit="kWh",
                device_class="energy",
                # Keep state_class for backward compatibility. Home Assistant
                # may already have long-term statistics for this legacy entity;
                # removing state_class triggers repair warnings. Do not use this
                # snapshot sensor as an Energy Dashboard source.
                state_class="total",
                station_id=station_id,
                date_key="last",
                metric_key=metric_key,
                extra_attributes={"metric": metric_name},
            ))

        # Daily metrics use relative keys so they roll over automatically at day change.
        for rel_key, label in _DAILY_LABELS.items():
            for metric_key, metric_name in _DAILY_METRICS:
                name = f"{metric_name} {label} {station_id}"
                uid = f"{station_id}_{metric_key}_{rel_key}"

                entities.append(DeyeCloudSensor(
                    coordinator=coordinator,
                    sensor_type="daily",
                    name=name,
                    unique_id=uid,
                    unit="kWh",
                    device_class="energy",
                    # Today is the live resettable meter and must be
                    # total_increasing. Keep a state_class on legacy historical
                    # snapshot entities as well, because Home Assistant may
                    # already have long-term statistics for these entity IDs;
                    # removing it creates repair warnings. Do not use Yesterday
                    # or Day Before sensors as Energy Dashboard sources.
                    state_class="total_increasing" if rel_key == "today" else "total",
                    station_id=station_id,
                    date_key=rel_key,
                    metric_key=metric_key,
                    extra_attributes={"relative_day": rel_key},
                ))

        # Device status sensors.
        for device_sn, device_data in station_data.get("devices", {}).items():
            device_sn = str(device_sn)
            for data_item in device_data.get("dataList") or []:
                key = data_item.get("key")
                if not key:
                    continue

                name = f"{key} {device_sn}"
                uid = f"device_{device_sn}_{key}"

                unit = _normalize_unit(data_item.get("unit", ""))
                unit_device_class = None
                unit_state_class = None

                if unit == "kWh":
                    unit_device_class = "energy"
                    # Safer default unless a key is known to be a lifetime counter.
                    unit_state_class = "total"
                elif unit == "W":
                    unit_device_class = "power"
                    unit_state_class = "measurement"
                elif unit == "V":
                    unit_device_class = "voltage"
                    unit_state_class = "measurement"
                elif unit == "A":
                    unit_device_class = "current"
                    unit_state_class = "measurement"
                elif unit == "%":
                    unit_device_class = "battery"
                    unit_state_class = "measurement"
                elif unit == "°C":
                    unit_device_class = "temperature"
                    unit_state_class = "measurement"
                elif unit == "Hz":
                    unit_device_class = "frequency"
                    unit_state_class = "measurement"

                entities.append(DeyeCloudSensor(
                    coordinator=coordinator,
                    sensor_type="device",
                    name=name,
                    unique_id=uid,
                    unit=unit,
                    device_class=unit_device_class,
                    state_class=unit_state_class,
                    station_id=station_id,
                    device_sn=device_sn,
                    device_key=key,
                    extra_attributes={
                        "device_type": device_data.get("deviceType"),
                        "device_state": device_data.get("deviceState"),
                        "collection_time": device_data.get("collectionTime"),
                    },
                ))

    async_add_entities(entities)
    _LOGGER.info("DeyeCloud integration setup completed with %d sensors", len(entities))
    return True
