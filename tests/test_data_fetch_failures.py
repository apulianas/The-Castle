from __future__ import annotations

import asyncio
import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from ravens_bot.espn import EspnApiError, EspnClient
from ravens_bot.injury_report import InjuryReportClient, InjuryReportError
from ravens_bot.official_inactives import OfficialInactivesClient, OfficialInactivesError
from ravens_bot.official_transactions import (
    OfficialTransactionsClient,
    OfficialTransactionsError,
)


@pytest.mark.parametrize(
    "client_type,method,error_type",
    [
        (InjuryReportClient, "fetch", InjuryReportError),
        (OfficialInactivesClient, "fetch_inactives", OfficialInactivesError),
        (
            OfficialTransactionsClient,
            "fetch_standard_elevations",
            OfficialTransactionsError,
        ),
    ],
)
@pytest.mark.parametrize("phase", ["connect", "read"])
def test_official_fetch_timeouts_use_the_clients_error_type(
    client_type, method, error_type, phase
) -> None:
    session = MagicMock()
    context = session.get.return_value
    response = context.__aenter__.return_value
    response.raise_for_status = MagicMock()
    timeout = TimeoutError("request timed out")
    if phase == "connect":
        context.__aenter__.side_effect = timeout
    else:
        response.text = AsyncMock(side_effect=timeout)
    client = client_type(session)
    args = () if method == "fetch" else (date(2026, 9, 13),)

    with pytest.raises(error_type) as raised:
        asyncio.run(getattr(client, method)(*args))

    assert raised.value.__cause__ is timeout


@pytest.mark.parametrize(
    "error",
    [
        json.JSONDecodeError("Expecting value", "<html>Unavailable</html>", 0),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
)
def test_espn_invalid_response_bodies_use_the_api_error_type(error) -> None:
    session = MagicMock()
    response = session.get.return_value.__aenter__.return_value
    response.status = 200
    response.json = AsyncMock(side_effect=error)
    client = EspnClient(session)

    with pytest.raises(EspnApiError) as raised:
        asyncio.run(client.fetch_roster())

    assert raised.value.__cause__ is error
    assert client._roster_cache.peek("roster") is None
