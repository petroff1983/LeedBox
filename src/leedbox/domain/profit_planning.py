"""User-supplied planning models. No intermediate rounding of expected flows."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext

D = Decimal


@dataclass(frozen=True)
class Forecast:
    audience: Decimal
    leads: Decimal
    sales: Decimal
    income: Decimal
    spend: Decimal
    tax: Decimal
    profit: Decimal
    unused_budget: Decimal = D(0)


def validate(*values: Decimal, rates: tuple[Decimal, ...] = ()) -> None:
    if any(not value.is_finite() or value < 0 for value in (*values, *rates)):
        raise ValueError("Значения должны быть конечными неотрицательными числами")
    if any(value > 100 for value in rates):
        raise ValueError("Конверсия и налог должны быть от 0 до 100%")


def advertising_plan(
    target: Decimal, margin: Decimal, sale_rate: Decimal, lead_rate: Decimal, click_cost: Decimal
) -> Forecast:
    """Excel Лист1 C3:G3: target is margin before advertising, NOT revenue."""
    validate(target, margin, click_cost, rates=(sale_rate, lead_rate))
    if target <= 0 or margin <= 0 or sale_rate <= 0 or lead_rate <= 0:
        raise ValueError("Для плана нужны положительные цель, маржа и обе конверсии")
    with localcontext() as ctx:
        ctx.prec = 120
        sales = target / margin
        leads = sales * 100 / sale_rate
        clicks = leads * 100 / lead_rate
        spend = clicks * click_cost
        return Forecast(clicks, leads, sales, target, spend, D(0), target - spend)


def telegram_plan(
    mode: str,
    amount: Decimal,
    subscriber_cost: Decimal,
    lead_rate: Decimal,
    sale_rate: Decimal,
    price: Decimal,
    tax_rate: Decimal,
) -> Forecast | None:
    validate(amount, subscriber_cost, price, rates=(lead_rate, sale_rate, tax_rate))
    if price <= 0:
        raise ValueError("Цена продукта должна быть больше нуля")
    if mode not in {"subscribers", "budget", "goal"}:
        raise ValueError("Неизвестный режим расчёта")
    if mode == "subscribers" and amount != amount.to_integral_value():
        raise ValueError("Количество подписчиков должно быть целым")
    if mode == "budget" and subscriber_cost <= 0:
        raise ValueError("Для расчёта от бюджета цена подписчика должна быть больше нуля")
    if mode == "goal" and amount <= 0:
        raise ValueError("Желаемая прибыль должна быть больше нуля")
    with localcontext() as ctx:
        ctx.prec = 120
        unit_profit = (
            lead_rate / 100 * sale_rate / 100 * price * (1 - tax_rate / 100) - subscriber_cost
        )
        if mode == "goal" and unit_profit <= 0:
            return None
        subscribers = amount
        if mode == "budget":
            subscribers = (amount / subscriber_cost).to_integral_value(rounding=ROUND_FLOOR)
        elif mode == "goal":
            subscribers = (amount / unit_profit).to_integral_value(rounding=ROUND_CEILING)
        leads = subscribers * lead_rate / 100
        sales = leads * sale_rate / 100
        revenue = sales * price
        tax = revenue * tax_rate / 100
        spend = subscribers * subscriber_cost
        return Forecast(
            subscribers,
            leads,
            sales,
            revenue,
            spend,
            tax,
            revenue - tax - spend,
            amount - spend if mode == "budget" else D(0),
        )


def display_number(value: Decimal, *, places: int = 2) -> str:
    with localcontext() as ctx:
        ctx.prec = max(120, value.adjusted() + places + 5)
        result = value.quantize(D(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if result == 0:
            result = abs(result)
        return f"{result:,.{places}f}".replace(",", " ").replace(".", ",")


def romi(forecast: Forecast) -> Decimal | None:
    """Profit after modeled costs divided by advertising spend; zero spend is undefined."""
    if forecast.spend == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 120
        return forecast.profit / forecast.spend * 100
