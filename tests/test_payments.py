import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from leedbox.domain.payments import (
    InvalidPayment,
    checkout_url,
    invoice_id,
    payment_amount,
    signature,
    validate_receipt,
    verify_result,
)
from leedbox.infrastructure.config import Settings


@pytest.mark.parametrize("value", ["0", "-1", "1e3", "NaN", "1,00", "1.001", " 1", "1\n"])
def test_payment_amount_rejects_unsafe_values(value):
    with pytest.raises(InvalidPayment):
        payment_amount(value, "RUB")


def test_stars_amounts_are_not_ruble_conversions():
    assert payment_amount("123", "XTR") == 123
    with pytest.raises(InvalidPayment):
        payment_amount("123.50", "XTR")


@pytest.mark.parametrize("value", ["0", "01", "-1", str(2**63), "1.0"])
def test_invoice_ids_are_canonical(value):
    with pytest.raises(InvalidPayment):
        invoice_id(value)


def test_signature_uses_exact_callback_amount_and_sorted_custom_fields():
    values = {"OutSum": "100.000000", "InvId": "5", "Shp_mode": "test", "Shp_bot": "123"}
    custom = {"Shp_bot": "123", "Shp_mode": "test"}
    values["SignatureValue"] = signature(["100.000000", "5"], "fixture-pass2", custom, "md5")
    assert verify_result(values, "fixture-pass2", "md5", custom) == (5, Decimal("100"))
    # Verified independently with .NET MD5 over the documented base string.
    assert values["SignatureValue"] == "00bc05d8a915e9b5761535b304070bcf"
    for change in ({"OutSum": "100.00"}, {"Shp_mode": "live"}, {"Shp_extra": "x"}):
        with pytest.raises(InvalidPayment):
            verify_result({**values, **change}, "fixture-pass2", "md5", custom)


def receipt(amount=100):
    return json.dumps(
        {
            "items": [
                {
                    "name": "Fixture service",
                    "quantity": 1,
                    "sum": amount,
                    "payment_object": "service",
                    "payment_method": "full_payment",
                    "tax": "none",
                }
            ]
        }
    )


def test_checkout_is_test_scoped_and_receipt_signed():
    params = parse_qs(
        urlsplit(
            checkout_url("fixture", Decimal("100"), 5, "fixture-pass1", "md5", True, 123, receipt())
        ).query
    )
    assert params["IsTest"] == ["1"] and params["Shp_mode"] == ["test"]
    assert "fixture-pass1" not in str(params)
    assert params["SignatureValue"][0] == signature(
        ["fixture", "100.00", "5", params["Receipt"][0]],
        "fixture-pass1",
        {"Shp_bot": "123", "Shp_mode": "test"},
        "md5",
    )
    with pytest.raises(InvalidPayment):
        checkout_url("fixture", Decimal("200"), 5, "fixture-pass1", "md5", True, 123, receipt())


def test_live_robokassa_disabled_outside_production():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://fixture@localhost/fixture_test",
            environment="test",
            robokassa_enabled=True,
            robokassa_test=False,
            robokassa_merchant="fixture",
            robokassa_password1="fixture-pass1",
            robokassa_password2="fixture-pass2",
            bot_token="123456:fixture_token_0123456789012345",
        )


@pytest.mark.parametrize("token", ["", "invalid", "0:fixture_token_0123456789012345"])
def test_robokassa_requires_bot_identity(token):
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://fixture@localhost/fixture_test",
            bot_token=token,
            robokassa_enabled=True,
            robokassa_merchant="fixture",
            robokassa_password1="fixture-pass1",
            robokassa_password2="fixture-pass2",
        )


@pytest.mark.parametrize(
    "change", [{"sum": "100.00"}, {"quantity": True}, {"tax": "vat99"}, {"cost": 101}]
)
def test_receipt_rejects_ambiguous_fiscal_fields(change):
    value = json.loads(receipt())
    value["items"][0].update(change)
    with pytest.raises(InvalidPayment):
        validate_receipt(json.dumps(value), Decimal("100"))


def test_receipt_rejects_duplicate_fields():
    value = receipt().replace('"sum": 100', '"sum": 99, "sum": 100')
    with pytest.raises(InvalidPayment):
        validate_receipt(value, Decimal("100"))
