"""Status sensor: result of the last restore pass, visible on dashboards.

One diagnostic sensor whose state is the number of entities patched in the
current/last run, with the full run stats, the degraded flag and the rolling
run history as attributes — the same data as the diagnostics download, but
without having to download anything.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LckConfigEntry
from .const import EVENT_RESTORED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LckConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([LckStatusSensor(entry)])


class LckStatusSensor(SensorEntity):
    """Number of entities restored in the last pass, stats as attributes."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "entities"

    def __init__(self, entry: LckConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_status"

    async def async_added_to_hass(self) -> None:
        # EVENT_RESTORED fires after every pass (pass 0, retries, final) —
        # exactly the moments the stats below change.
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_RESTORED, self._on_restored)
        )

    @callback
    def _on_restored(self, _event: Event) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return int(self._entry.runtime_data.stats.get("patched_total", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        job = self._entry.runtime_data
        return {
            "last_run": dict(job.stats),
            "degraded": job._degraded,
            "run_history": [dict(r) for r in job.run_history],
        }
