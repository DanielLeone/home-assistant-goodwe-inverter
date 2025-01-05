"""Update coordinator for Goodwe."""

from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import logging
from typing import Any

from goodwe import Inverter, InverterError, RequestFailedException, ProtocolCommand, UdpInverterProtocol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, CALLBACK_TYPE
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import (
    BaseCoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class GoodweUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Gather data for the energy device."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        inverter: Inverter,
    ) -> None:
        """Initialize update coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=entry.title,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.inverter: Inverter = inverter
        self._last_data: dict[str, Any] = {}
        self._polled_entities: dict[BaseCoordinatorEntity, datetime] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the inverter."""
        await self._update_polled_entities()

        try:
            self._last_data = self.data if self.data else {}
            return await self.inverter.read_runtime_data()
        except RequestFailedException as ex:
            # UDP communication with inverter is by definition unreliable.
            # It is rather normal in many environments to fail to receive
            # proper response in usual time, so we intentionally ignore isolated
            # failures and report problem with availability only after
            # consecutive streak of 3 of failed requests.
            if ex.consecutive_failures_count < 3:
                _LOGGER.debug(
                    "No response received (streak of %d)", ex.consecutive_failures_count
                )
                # return last known data
                return self._last_data
            # Inverter does not respond anymore (e.g. it went to sleep mode)
            _LOGGER.debug(
                "Inverter not responding (streak of %d)", ex.consecutive_failures_count
            )
            raise UpdateFailed(ex) from ex
        except InverterError as ex:
            raise UpdateFailed(ex) from ex

    async def _update_polled_entities(self) -> None:
        for entity, interval in list(self._polled_entities.items()):
            if interval:
                try:
                    await entity.async_update()
                except InverterError:
                    _LOGGER.debug("Failed to update entity %s", entity.name)

    def sensor_value(self, sensor: str) -> Any:
        """Answer current (or last known) value of the sensor."""
        val = self.data.get(sensor)
        return val if val is not None else self._last_data.get(sensor)

    def total_sensor_value(self, sensor: str) -> Any:
        """Answer current value of the 'total' (never 0) sensor."""
        val = self.data.get(sensor)
        return val if val else self._last_data.get(sensor)

    def reset_sensor(self, sensor: str) -> None:
        """Reset sensor value to 0.

        Intended for "daily" cumulative sensors (e.g. PV energy produced today),
        which should be explicitly reset to 0 at midnight if inverter is suspended.
        """
        self._last_data[sensor] = 0
        self.data[sensor] = 0

    def entity_state_polling(
        self, entity: BaseCoordinatorEntity, interval: int
    ) -> None:
        """Enable/disable polling of entity state."""
        if interval:
            self._polled_entities[entity] = interval
        else:
            self._polled_entities.pop(entity, None)


class GoodweUpdateCoordinatorWithWakeUp(GoodweUpdateCoordinator):
    _host: str
    _cancel_wakeup_interval: CALLBACK_TYPE | None = None

    def __init__(
            self,
            hass: HomeAssistant,
            entry: ConfigEntry,
            inverter: Inverter,
            host: str,
    ):
        super().__init__(hass=hass, entry=entry, inverter=inverter)
        self._host = host
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, lambda _: self._start_wakeup_interval())

    def _start_wakeup_interval(self):
        async def send_wakeup_packet(_) -> None:
            await self._send_wakeup_packet()

        self._cancel_wakeup_interval = async_track_time_interval(
            hass=self.hass,
            action=send_wakeup_packet,
            interval=timedelta(minutes=10),
            name="goodwe_inverter_send_wakeup_packet"
        )

        # send a wakeup packet on start as well just to be safe
        self._send_wakeup_packet()

    async def async_shutdown(self):
        if self._cancel_wakeup_interval:
            self._cancel_wakeup_interval()
            self._cancel_wakeup_interval = None

        return super().async_shutdown()

    async def _send_wakeup_packet(self) -> None:
        self.logger.debug("Sending wakeup packet to inverter on port 48899")
        command = ProtocolCommand("WIFIKIT-214028-READ".encode("utf-8"), lambda r: True)
        try:
            result = await command.execute(UdpInverterProtocol(host=self._host, port=48899, comm_addr=1, timeout=1))
            if result is not None:
                raw_data = result.response_data()
                self.logger.debug(f"Received response from wakeup packet: {repr(raw_data)}")
            else:
                self.logger.debug(f"No response received from wakeup packet")
        except asyncio.CancelledError:
            self.logger.debug(f"No valid response received to wakeup packet")
