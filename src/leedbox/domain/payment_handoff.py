"""Short-lived capability issued only after buyer acceptance; no card information."""

import hashlib
import hmac
import time

from leedbox.domain.payments import InvalidPayment, invoice_id


def ticket(number: int, secret: str, expires: int | None = None) -> str:
    body = str(number) + "." + str(expires or int(time.time()) + 900)
    digest = hmac.new(
        secret.encode(), ("leedbox-checkout:" + body).encode(), hashlib.sha256
    ).hexdigest()
    return body + "." + digest


def verify(token: str, secret: str) -> int:
    try:
        number, expires, digest = token.split(".")
        id = invoice_id(number)
        if len(expires) > 12 or not expires.isascii() or not expires.isdecimal():
            raise ValueError
        if not int(time.time()) < int(expires) <= int(time.time()) + 900:
            raise ValueError
        if not hmac.compare_digest(ticket(id, secret, int(expires)), token):
            raise ValueError
        return id
    except ValueError:
        raise InvalidPayment("Ссылка оплаты истекла или недействительна") from None
