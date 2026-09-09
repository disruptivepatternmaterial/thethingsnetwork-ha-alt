"""The Things Network HA-Alt config flow."""

from collections.abc import Mapping
import logging
from typing import Any

from aiohttp import ClientError
from ttn_client import TTNAuthError
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONF_APP_ID, DOMAIN, TTN_API_HOST
from .storage import TTNStorageError, async_validate_credentials

_LOGGER = logging.getLogger(__name__)


class TTNFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User initiated config flow."""

        errors = {}
        if user_input is not None:
            # Normalize host: users often paste a URL; a bare hostname is
            # what goes into the storage API URL.
            host = str(user_input[CONF_HOST]).strip()
            host = host.removeprefix("https://").removeprefix("http://").rstrip("/")
            user_input = {**user_input, CONF_HOST: host or TTN_API_HOST}
            try:
                await async_validate_credentials(
                    self.hass,
                    user_input[CONF_HOST],
                    user_input[CONF_APP_ID],
                    user_input[CONF_API_KEY],
                )
            except TTNAuthError:
                _LOGGER.exception("Error authenticating with The Things Network")
                errors["base"] = "invalid_auth"
            except (ClientError, TimeoutError, TTNStorageError):
                # A mistyped application id answers 404, not 401. Reporting
                # that as invalid authentication sent the user off to reissue
                # a key that was never the problem.
                _LOGGER.exception("Could not read the storage API")
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unknown error occurred")
                errors["base"] = "unknown"

            if not errors:
                if self.source == SOURCE_REAUTH:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data=user_input
                    )
                await self.async_set_unique_id(user_input[CONF_APP_ID])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=str(user_input[CONF_APP_ID]),
                    data=user_input,
                )

        if not user_input:
            if self.source == SOURCE_REAUTH:
                user_input = self._get_reauth_entry().data
            else:
                user_input = {CONF_HOST: TTN_API_HOST}

        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_APP_ID): str,
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD, autocomplete="api_key"
                        )
                    ),
                }
            ),
            user_input,
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "instructions_url": "https://www.thethingsindustries.com/docs/integrations/adding-applications/"
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a flow initialized by a reauth event."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()
