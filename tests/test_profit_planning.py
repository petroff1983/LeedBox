from decimal import Decimal as D
from urllib.parse import parse_qs, urlsplit

import pytest

from leedbox.domain.profit_planning import advertising_plan, display_number, romi, telegram_plan
from leedbox.telegram.calculators import SCHEMAS, calculate, formatted, menu_rows, sharing_url


def test_advertising_matches_user_workbook_without_rounding_intermediates():
    result = advertising_plan(D(30000), D(1000), D(33), D(10), D(15))
    assert result.sales == 30
    assert display_number(result.leads) == "90,91"
    assert display_number(result.audience) == "909,09"
    assert display_number(result.spend) == "13 636,36"
    assert display_number(result.profit) == "16 363,64"
    assert result.income == result.profit + result.spend
    assert display_number(romi(result), places=0) == "120"


def test_customer_mediaplan_labels_and_expected_rounded_display():
    text = calculate(
        "ads",
        {
            "target": "30000",
            "margin": "1000",
            "sale_rate": "33",
            "lead_rate": "10",
            "click_cost": "15",
            "platform": "yandex",
        },
    )
    assert "МЕДИАПЛАН ДЛЯ ЗАКАЗЧИКА" in text and "Яндекс Директ" in text
    assert "Продажи: ≈ 30" in text and "Заявки: ≈ 91" in text and "Клики: ≈ 909" in text
    assert "Рекламный бюджет: 13 636,36 ₽" in text and "После рекламы: 16 363,64 ₽" in text
    assert "ROMI (окупаемость рекламы): 120%" in text
    assert "Чистая прибыль заказчика" not in text
    assert "без промежуточного округления" in text
    assert "3 продажи → 30%" in SCHEMAS["ads"][2].prompt


@pytest.mark.parametrize(
    "cost,profit,expected_romi", [("0", "3525000", None), ("150", "2775000", "370")]
)
def test_new_telegram_mediaplan_example(cost, profit, expected_romi):
    result = telegram_plan("subscribers", D(5000), D(cost), D(5), D(30), D(50000), D(6))
    assert result is not None and result.profit == D(profit)
    assert result.sales == 75 and result.leads == 250 and result.income == 3750000
    assert (
        None if romi(result) is None else display_number(romi(result), places=0)
    ) == expected_romi


def test_corrected_launch_example_does_not_misstate_profit():
    result = telegram_plan("goal", D(100000), D(150), D(5), D(30), D(50000), D(6))
    assert result is not None and result.audience == 181 and result.profit == 100455
    assert result.spend == 27150
    alternative = telegram_plan("subscribers", D(200), D(150), D(5), D(30), D(50000), D(6))
    assert alternative is not None and alternative.profit == 111000 and romi(alternative) == 370


def test_sharing_contains_only_prepared_report_and_encoded_bot_link():
    report = "Медиаплан для заказчика\nДоход: 10 000 ₽\nA & B # x"
    url = sharing_url("fixture_bot", report)
    assert url is not None and urlsplit(url).netloc == "t.me"
    assert parse_qs(urlsplit(url).query) == {"url": ["https://t.me/fixture_bot"], "text": [report]}
    assert sharing_url("", report) is None
    assert sharing_url("fixture_bot", "Д" * 2000) is None


def test_zero_advertising_spend_does_not_create_infinite_romi():
    result = advertising_plan(D(30000), D(1000), D(33), D(10), D(0))
    assert romi(result) is None


def test_telegram_matches_forward_example_and_does_not_promise_infinite_roi():
    result = telegram_plan("subscribers", D(5000), D(0), D(2), D(10), D(50000), D(6))
    assert result is not None
    assert (result.leads, result.sales, result.income, result.tax, result.profit) == (
        100,
        10,
        500000,
        30000,
        470000,
    )
    text = calculate(
        "tg_subscribers",
        {
            "amount": "5000",
            "subscriber_cost": "0",
            "lead_rate": "2",
            "sale_rate": "10",
            "price": "50000",
            "tax_rate": "6",
        },
    )
    assert "470 000,00 ₽" in text and "не рассчитывается" in text
    assert "Infinity" not in text and "Без себестоимости" in text


def test_profit_goal_includes_traffic_and_reconciles_forward():
    result = telegram_plan("goal", D(100000), D(300), D(10), D(20), D(50000), D(6))
    assert result is not None
    assert result.audience == 157
    assert result.spend == 47100
    assert result.income == 157000
    assert result.tax == 9420
    assert result.profit == 100480
    previous = telegram_plan("subscribers", D(156), D(300), D(10), D(20), D(50000), D(6))
    assert previous is not None and previous.profit < 100000
    forward = telegram_plan("subscribers", result.audience, D(300), D(10), D(20), D(50000), D(6))
    assert forward == result


@pytest.mark.parametrize(
    "cost,lead,sale,tax", [(1000, 10, 20, 6), (300, 0, 20, 6), (0, 10, 0, 0), (0, 10, 20, 100)]
)
def test_unprofitable_goal_is_unattainable_not_a_fictitious_budget(cost, lead, sale, tax):
    assert telegram_plan("goal", D(100000), D(cost), D(lead), D(sale), D(50000), D(tax)) is None


def test_budget_buys_whole_subscribers_without_exceeding_limit():
    result = telegram_plan("budget", D(1000), D(300), D(10), D(20), D(50000), D(6))
    assert result is not None
    assert result.audience == 3 and result.spend == 900 and result.unused_budget == 100
    assert result.leads == D("0.3") and result.sales == D("0.06")
    assert result.profit == 1920


@pytest.mark.parametrize("amount", [D(0), D(1), D(299)])
def test_budget_below_one_subscriber_remains_unspent(amount):
    result = telegram_plan("budget", amount, D(300), D(10), D(20), D(50000), D(6))
    assert result is not None and result.audience == 0 and result.unused_budget == amount


def test_zero_rates_forward_and_loss_remain_visible():
    result = telegram_plan("subscribers", D(1000), D(300), D(0), D(20), D(50000), D(6))
    assert result is not None and result.income == 0 and result.profit == -300000


@pytest.mark.parametrize(
    "field,value",
    [(0, "0"), (1, "0"), (2, "0"), (3, "0"), (2, "101"), (4, "-1"), (4, "NaN"), (1, "Infinity")],
)
def test_advertising_rejects_invalid_inputs(field, value):
    values = list(map(D, ["30000", "1000", "33", "10", "15"]))
    values[field] = D(value)
    with pytest.raises(ValueError):
        advertising_plan(*values)


@pytest.mark.parametrize(
    "mode,amount,cost,price,tax",
    [
        ("budget", "1000", "0", "50000", "6"),
        ("subscribers", "1.5", "300", "50000", "6"),
        ("goal", "0", "300", "50000", "6"),
        ("goal", "1000", "300", "0", "6"),
        ("goal", "1000", "300", "50000", "101"),
    ],
)
def test_telegram_rejects_invalid_inputs(mode, amount, cost, price, tax):
    with pytest.raises(ValueError):
        telegram_plan(mode, D(amount), D(cost), D(10), D(20), D(price), D(tax))


def test_only_two_public_tools_and_safe_html():
    rows = menu_rows()
    tools = [data for row in rows for _, data in row if data not in {"menu", "calc:history"}]
    assert tools == ["calc:new:ads", "calc:telegram"]
    assert "&lt;script&gt;" in formatted("Title\n<script>")


def test_large_and_tiny_inputs_do_not_overflow_display():
    result = advertising_plan(D("1e15"), D("1e-20"), D("1e-20"), D("1e-20"), D("1e15"))
    assert display_number(result.spend).endswith(",00")
    assert display_number(D("-0.001")) == "0,00"


@pytest.mark.parametrize("tool", ["funnel", "campaign", "breakeven", "budget_cpl", "budget_cpc"])
def test_retired_calculators_are_not_recomputed(tool):
    with pytest.raises(ValueError, match="больше не поддерживается"):
        calculate(tool, {})
