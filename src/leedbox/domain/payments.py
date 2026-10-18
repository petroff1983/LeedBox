"""Payment protocol primitives. No HTTP calls, card data or invented tax settings."""

import hashlib
import hmac
import json
import re
from decimal import Decimal
from urllib.parse import quote, urlencode


class InvalidPayment(ValueError):
    """Safe generic error; never include signatures, passwords or callback bodies."""


def payment_amount(value: str, currency: str) -> Decimal:
    if not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,6})?", value):
        raise InvalidPayment("Invalid payment amount")
    amount = Decimal(value)
    if amount <= 0 or amount >= Decimal("1e16"):
        raise InvalidPayment("Invalid payment amount")
    step = Decimal("1") if currency == "XTR" else Decimal("0.01")
    if currency not in {"RUB", "XTR"} or amount != amount.quantize(step):
        raise InvalidPayment("Unsupported currency or amount precision")
    return amount.quantize(step)


def invoice_id(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
        raise InvalidPayment("Invalid invoice ID")
    return int(value)


def signature(parts: list[str], password: str, custom: dict[str, str], algorithm: str) -> str:
    if algorithm not in {"md5", "sha256"} or not password:
        raise InvalidPayment("Unsupported signature configuration")
    if any(not re.fullmatch(r"Shp_[A-Za-z0-9_]+", key) for key in custom):
        raise InvalidPayment("Invalid custom parameters")
    base = ":".join([*parts, password, *(f"{key}={custom[key]}" for key in sorted(custom))])
    return hashlib.new(algorithm, base.encode("utf-8")).hexdigest()


def verify_result(
    values: dict[str, str], password: str, algorithm: str, expected_custom: dict[str, str]
) -> tuple[int, Decimal]:
    try:
        raw_sum, raw_id, digest = values["OutSum"], values["InvId"], values["SignatureValue"]
    except KeyError:
        raise InvalidPayment("Missing payment fields") from None
    amount = payment_amount(raw_sum, "RUB")
    number = invoice_id(raw_id)
    custom = {key: value for key, value in values.items() if key.lower().startswith("shp_")}
    if custom != expected_custom:
        raise InvalidPayment("Incorrect payment scope")
    expected = signature([raw_sum, raw_id], password, custom, algorithm)
    if not re.fullmatch(r"[0-9a-fA-F]{" + str(len(expected)) + r"}", digest):
        raise InvalidPayment("Invalid payment signature")
    if not hmac.compare_digest(expected, digest.lower()):
        raise InvalidPayment("Invalid payment signature")
    return number, amount


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate receipt field")
        data[key] = value
    return data


def validate_receipt(receipt: str, amount: Decimal) -> str:
    if not 0 < len(receipt) <= 10000:
        raise InvalidPayment("A fiscal receipt is required")
    try:
        data = json.loads(receipt, parse_float=Decimal, object_pairs_hook=_unique_json_object)
        if "sno" in data and data["sno"] not in {
            "osn",
            "usn_income",
            "usn_income_outcome",
            "esn",
            "patent",
        }:
            raise ValueError
        items = data["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise ValueError
        total = Decimal("0")
        for item in items:
            if (
                not isinstance(item["name"], str)
                or not 0 < len(item["name"]) <= 128
                or item["payment_object"] != "service"
                or item["payment_method"] not in {"full_payment", "full_prepayment"}
                or item["tax"]
                not in {
                    "none",
                    "vat0",
                    "vat10",
                    "vat110",
                    "vat20",
                    "vat22",
                    "vat120",
                    "vat122",
                    "vat5",
                    "vat7",
                    "vat105",
                    "vat107",
                }
                or type(item["quantity"]) not in {int, Decimal}
                or type(item["sum"]) not in {int, Decimal}
                or Decimal(str(item["quantity"])) != 1
            ):
                raise ValueError
            # Quantity=1 quotes only. Sum is authoritative, never price_label parsing.
            total += payment_amount(str(item["sum"]), "RUB")
            if "cost" in item and (
                type(item["cost"]) not in {int, Decimal}
                or payment_amount(str(item["cost"]), "RUB") != Decimal(str(item["sum"]))
            ):
                raise ValueError
        if total != amount:
            raise ValueError
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise InvalidPayment("Invalid receipt or receipt total") from None
    return receipt


def checkout_url(
    merchant: str,
    amount: Decimal,
    number: int,
    password: str,
    algorithm: str,
    test_mode: bool,
    bot_id: int,
    receipt: str,
    method: str = "",
) -> str:
    validate_receipt(receipt, amount)
    raw_sum = format(amount, ".2f")
    raw_id = str(invoice_id(str(number)))
    encoded = quote(receipt, safe="")
    custom = {"Shp_bot": str(bot_id), "Shp_mode": "test" if test_mode else "live"}
    params = {
        "MerchantLogin": merchant,
        "OutSum": raw_sum,
        "InvId": raw_id,
        "Receipt": encoded,
        "SignatureValue": signature(
            [merchant, raw_sum, raw_id, encoded], password, custom, algorithm
        ),
        "Description": f"Service order {number}",
        "IsTest": "1" if test_mode else "0",
        "Culture": "ru",
        **custom,
    }
    if method:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", method):
            raise InvalidPayment("Invalid payment method")
        params["IncCurrLabel"] = method
    return "https://auth.robokassa.ru/Merchant/Index.aspx?" + urlencode(params)
