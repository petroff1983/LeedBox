import re
from decimal import Decimal


def parse_number(raw: str, *, percentage: bool = False, integer: bool = False) -> Decimal:
    value = "".join(raw.lower().split()).replace(",", ".")
    if value.endswith("%"):
        if not percentage:
            raise ValueError("Проценты допустимы только в поле процента")
        value = value[:-1]
    factor = Decimal(1000) if value.endswith(("к", "k")) else Decimal(1)
    if factor != 1:
        value = value[:-1]
    if len(value) > 30 or not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise ValueError("Введите число: например 300 000, 300к или 1,5% для процента")
    result = Decimal(value) * factor
    if result > Decimal("1e15") or (percentage and result > 100):
        raise ValueError("Значение вне допустимого диапазона")
    if integer and result != result.to_integral_value():
        raise ValueError("Введите целое число")
    return result
