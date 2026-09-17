"""Restore-now button: manual trigger for debugging, without a service call
or a dashboard button the user has to build themselves."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LckConfigEntry
from .const import DOMAIN, SERVICE_RESTORE_NOW


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LckConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([LckRestoreNowButton(entry)])


class LckRestoreNowButton(ButtonEntity):
    """Runs a restore pass immediately, same as the restore_now service."""

    _attr_has_entity_name = True
    _attr_translation_key = "restore_now"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: LckConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_restore_now"

    async def async_press(self) -> None:
        # Routed through the service (rather than calling
        # job._async_run_impl directly) so both trigger paths share the same
        # error handling (HomeAssistantError on failure).
        await self.hass.services.async_call(
            DOMAIN, SERVICE_RESTORE_NOW, {}, blocking=True
        )
