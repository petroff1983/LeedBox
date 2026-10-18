"""Fixed HTTPS provider endpoints; bounded responses, no redirects or raw error logging."""

import base64
import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

from leedbox.domain.payments import InvalidPayment, payment_amount, signature, validate_receipt
from leedbox.infrastructure.config import Settings


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def transport(url: str, data: bytes | None = None) -> bytes:
    try:
        req = Request(url, data=data, headers={"Content-Type": "text/plain; charset=utf-8"})
        with build_opener(NoRedirect()).open(req, timeout=15) as response:
            body = response.read(65537)
        if len(body) > 65536:
            raise ValueError
        return bytes(body)
    except Exception:
        raise InvalidPayment("Не удалось получить подтверждение провайдера") from None


def operation(config: Settings, number: int) -> dict[str, str]:
    if not config.robokassa_enabled or config.robokassa_test:
        raise InvalidPayment("OpStateExt недоступен для тестовых платежей Robokassa")
    query = urlencode(
        {
            "MerchantLogin": config.robokassa_merchant,
            "InvoiceID": number,
            "Signature": signature(
                [config.robokassa_merchant, str(number)],
                config.robokassa_password2.get_secret_value(),
                {},
                config.robokassa_hash,
            ),
        }
    )
    raw = transport(
        "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt?" + query
    )
    if b"<!" in raw or b"\x00" in raw:
        raise InvalidPayment("Некорректный ответ провайдера")
    try:
        root = ElementTree.fromstring(raw)
        for node in root.iter():
            node.tag = node.tag.rsplit("}", 1)[-1]

        def value(path: str) -> str:
            values = root.findall(path)
            if len(values) != 1 or not values[0].text:
                raise ValueError
            return values[0].text

        if value("Result/Code") != "0":
            raise ValueError
        result = {
            "state": value("State/Code"),
            "amount": value("Info/OutSum"),
            "currency": value("Info/OutCurrLabel"),
            "op_key": value("Info/OpKey"),
        }
        fields = root.findall("UserFields/Field")
        custom = {value.findtext("Name"): value.findtext("Value") for value in fields}
        if len(custom) != len(fields):
            raise ValueError
        if custom.get("Shp_bot") is None or custom.get("Shp_mode") != "live":
            raise ValueError
        result["bot_id"] = str(custom["Shp_bot"])
        if not 0 < len(result["op_key"]) <= 200:
            raise ValueError
        payment_amount(result["amount"], "RUB")
        return result
    except (ValueError, ElementTree.ParseError):
        raise InvalidPayment("Неполный или неоднозначный ответ провайдера") from None


def refund(config: Settings, op_key: str, receipt: str, amount: Decimal) -> str:
    password = config.robokassa_password3.get_secret_value()
    if not password or config.robokassa_test:
        raise InvalidPayment("Для возврата Robokassa нужен Password3 и основной режим")
    validate_receipt(receipt, amount)
    # Numeric amounts are emitted from Decimal, never binary floating-point.
    items = json.loads(receipt, parse_float=Decimal)["items"]
    invoice_items = []
    for item in items:
        rest = {
            "Name": item["name"],
            "Quantity": 1,
            "Tax": item["tax"],
            "PaymentMethod": item["payment_method"],
            "PaymentObject": item["payment_object"],
        }
        invoice_items.append(
            json.dumps(rest, ensure_ascii=False, separators=(",", ":"))[:-1]
            + ',"Cost":'
            + format(Decimal(item["sum"]), "f")
            + "}"
        )
    payload = (
        '{"OpKey":' + json.dumps(op_key) + ',"InvoiceItems":[' + ",".join(invoice_items) + "]}"
    )

    def encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    unsigned = encode(b'{"alg":"HS256","typ":"JWT"}') + "." + encode(payload.encode())
    token = (
        unsigned
        + "."
        + encode(hmac.new(password.encode(), unsigned.encode(), hashlib.sha256).digest())
    )
    response = json.loads(
        transport("https://services.robokassa.ru/RefundService/Refund/Create", token.encode())
    )
    if response.get("success") is not True:
        raise InvalidPayment("Провайдер не подтвердил создание возврата")
    from uuid import UUID

    return str(UUID(response["requestId"]))


def refund_state(reference: str, amount: Decimal) -> str:
    from uuid import UUID

    reference = str(UUID(reference))
    response = json.loads(
        transport(
            "https://services.robokassa.ru/RefundService/Refund/GetState?"
            + urlencode({"id": reference})
        ),
        parse_float=Decimal,
    )
    if response.get("requestId") != reference or Decimal(str(response.get("amount"))) != amount:
        raise InvalidPayment("Возврат не совпадает с заявкой или суммой")
    if response.get("label") not in {"finished", "processing", "canceled"}:
        raise InvalidPayment("Неизвестное состояние возврата")
    return str(response["label"])
