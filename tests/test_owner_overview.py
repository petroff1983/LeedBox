from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from test_admin import admin_settings

from leedbox.application.owner_overview import integrations, money, window
from leedbox.infrastructure.models import Setting


def test_report_windows_and_money():
    now = datetime(2026, 10, 14, 12, tzinfo=UTC)
    report = window("7", "test", now)
    assert report.start == now - timedelta(days=7)
    assert report.previous.end == report.start
    assert report.previous.start == now - timedelta(days=14)
    assert report.test
    assert window("today", "live", now).start.hour == 0
    assert money(Decimal("1234.5678"), 4) == "1\u00a0234,5678"
    assert window("all", "live", now).start.year == 1970
    for period, mode in (("invalid", "test"), ("7", "wrong"), ("-1", "live")):
        with pytest.raises(ValueError):
            window(period, mode, now)


@pytest.mark.parametrize(
    "data, expected, attention",
    [
        ({}, "Нет свежей проверки", True),
        (
            {
                "checked_at": "2026-10-14T11:00:00+00:00",
                "balances": {"USD": "5.00"},
                "available": True,
            },
            "Баланс проверен",
            False,
        ),
        (
            {
                "checked_at": "2026-10-14T11:00:00+00:00",
                "balances": {"USD": "0.01"},
                "available": True,
            },
            "Пора пополнить",
            True,
        ),
        (
            {
                "checked_at": "2026-10-13T11:00:00+00:00",
                "balances": {"USD": "5.00"},
                "available": True,
            },
            "Нет свежей проверки",
            True,
        ),
        (
            {"checked_at": "2026-10-14T11:00:00+00:00", "error": "balance_unavailable"},
            "Проверка не удалась",
            True,
        ),
        (
            {
                "checked_at": "2026-10-14T11:00:00+00:00",
                "balances": {"USD": "NaN"},
                "available": True,
            },
            "Проверка не удалась",
            True,
        ),
        (
            {"checked_at": "2026-10-14T11:00:00", "balances": {"USD": "5.00"}, "available": True},
            "Нет свежей проверки",
            True,
        ),
    ],
)
def test_provider_status_is_honest_and_read_only(data, expected, attention):
    from pydantic import SecretStr

    config = admin_settings(deepseek_api_key=SecretStr("synthetic-never-displayed"))
    db = MagicMock()
    db.get.side_effect = lambda model, key: (
        Setting(key=key, value=data) if key.startswith("provider_status:") else None
    )
    rows = integrations(db, config, 123456, datetime(2026, 10, 14, 12, tzinfo=UTC))
    assert rows[0]["status"] == expected
    assert rows[0]["attention"] is attention
    assert "synthetic-never-displayed" not in str(rows)
    assert "Нет проверки polling" in rows[4]["status"]
    db.add.assert_not_called()
    db.execute.assert_not_called()
