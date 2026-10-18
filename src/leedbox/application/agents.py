"""Dzen agent orchestration, persisted quota reservations and wallet token packages."""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application import ai_tokens, credits, dzen_history, writing_library
from leedbox.application.purchasing import purchase_terms
from leedbox.application.sections import visible_sections
from leedbox.application.settings import setting
from leedbox.application.wallet import account as wallet_account
from leedbox.domain import image_prompts
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.dzen import channel_slug, period_start, validated_result
from leedbox.domain.writing_profile import BUILDER, validate_brief, validate_prompt
from leedbox.infrastructure.agents import PROVIDER_ERRORS, AgentError, deepseek_generate, dzen_posts
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AIOperation, WalletAccount, WalletEntry

OUTPUT_LIMIT = 2200


def search_price(db: Session, task: str) -> int:
    return (
        credits.price(db, task)
        if credits.enabled(db)
        else int(setting(db, "ai_" + task + "_tokens"))
    )


def ensure_search_margin(db: Session, purpose: str, tokens: int) -> None:
    """Do not sell an external API run below 2x its configured maximum API cost."""
    if purpose not in {"dzen_search", "content_search"}:
        return
    cost = Decimal(str(setting(db, "zenstat_request_cost_rub")))
    if purpose == "content_search":
        cost = min(
            Decimal("0.04"), Decimal(str(setting(db, "ai_content_apify_budget_usd")))
        ) * Decimal(str(setting(db, "ai_usd_rub")))
    if credits.enabled(db):
        price = credits.unit_price(db)
        tokens = credits.price(db, purpose)
    else:
        price = Decimal(str(setting(db, "ai_token_price_rub")))
    if Decimal(tokens) * price < cost * 2:
        raise ValueError("Цена платного анализа пока не настроена. Обратитесь к владельцу")


def payment_preview(
    db: Session, config: Settings, owner: int, bot: int, offer: dict[str, Any]
) -> tuple[bool, str]:
    test = test_mode(config)
    mode = ai_tokens.funding(db, config, owner, bot, test, offer)
    # The confirmation must not silently become paid after a trial is consumed elsewhere.
    offer["confirmed_billing"] = mode
    if mode == "unlimited":
        return True, "♾ Безлимитный доступ. Списаний с вашего баланса нет."
    if mode == "trial":
        return True, "🎁 Бесплатная попытка. Токены с баланса не списываются."
    ensure_search_margin(db, str(offer.get("purpose", "")), int(offer["maximum"]))
    balance = ai_tokens.available(db, owner, bot, test)
    if credits.is_credit(offer):
        credits.ensure_margin(db, offer)
        charge = credits.amount(offer)
        enough = balance >= charge
        return enough, (
            f"🪙 Остаток: {balance:,} кредитов.\nСтоимость: {charge} кредитов.\n"
            "Цена фиксирована; фактические токены покажем отдельно."
            + ("\nНе хватает кредитов. Пополните баланс." if not enough else "")
        )
    enough = balance >= offer["maximum"]
    if offer.get("purpose") in {"dzen_search", "youtube_search", "content_search"}:
        return enough, (
            f"🪙 Остаток: {balance:,} токенов.\n"
            f"Цена поиска: {offer['maximum']:,} токенов (фиксированная, не расход ИИ)."
            + ("\nНе хватает токенов. Пополните баланс." if not enough else "")
        )
    return enough, (
        f"🪙 Остаток: {balance:,} токенов. Резерв: до {offer['maximum']:,}.\n"
        "Это верхняя граница резерва, не цена.\n"
        "Спишем только фактический вход + выход API. Дневного лимита нет."
        + ("\nНе хватает токенов для резерва. Пополните баланс." if not enough else "")
    )


def writing_profile(db: Session, owner: int, bot: int, test: bool) -> AIOperation | None:
    """Completed ledger entries are immutable saved versions; no schema migration needed."""
    from sqlalchemy import select

    return db.scalar(
        select(AIOperation)
        .where(
            AIOperation.owner_user_id == owner,
            AIOperation.bot_id == bot,
            AIOperation.test_mode == test,
            AIOperation.status == "completed",
            AIOperation.snapshot["purpose"].astext == "writing_profile",
        )
        .order_by(AIOperation.id.desc())
        .limit(1)
    )


@credits.quoted
def quote_profile(
    db: Session, brief: dict[str, str], search_token: str | None = None
) -> dict[str, Any]:
    inputs = {
        "brief": validate_brief(brief),
        "base_template": str(setting(db, "ai_dzen_article_template"))
        .replace("{{title}}", "выбранный заголовок")
        .replace("{{inputs}}", "вводные автора"),
    }
    bound = len(BUILDER.encode()) + len(json.dumps(inputs, ensure_ascii=False).encode()) + 512
    if bound > 65000:
        raise ValueError("Слишком длинные вводные")
    return {
        "purpose": "writing_profile",
        "version": 2,
        **({"search_token": search_token} if search_token else {}),
        "model": "deepseek-flash",
        "system": BUILDER,
        "inputs": inputs,
        "maximum": bound + 5000,
        "output_limit": 5000,
        "rates": {
            "input": setting(db, "ai_input_usd_per_million"),
            "output": setting(db, "ai_output_usd_per_million"),
            "usd_rub": setting(db, "ai_usd_rub"),
        },
    }


def validate_packages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError("Не более десяти пакетов")
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "tokens", "price_rub", "enabled"}:
            raise ValueError("Пакет: id, tokens, price_rub (строка), enabled")
        if (
            not isinstance(item["id"], str)
            or not item["id"].isascii()
            or not item["id"].isalnum()
            or not 1 <= len(item["id"]) <= 20
            or item["id"] in seen
        ):
            raise ValueError("Уникальный id: 1–20 латинских букв и цифр")
        seen.add(item["id"])
        if (
            type(item["tokens"]) is not int
            or not 1 <= item["tokens"] <= 100000000
            or type(item["enabled"]) is not bool
        ):
            raise ValueError("Некорректное количество токенов или публикация пакета")
        from leedbox.domain.payments import payment_amount

        if not isinstance(item["price_rub"], str):
            raise ValueError("Цена пакета должна быть строкой")
        price = payment_amount(item["price_rub"], "RUB")
        if price > 100000:
            raise ValueError("Цена пакета: до 100000 ₽")
    return value


def test_mode(config: Settings) -> bool:
    return config.environment != "production"


def allowed(
    db: Session,
    owner: int,
    bot: int,
    test: bool,
    purpose: str = "dzen",
    config: Settings | None = None,
) -> None:
    # Agent usage is funded by its own trials/tokens, not by the core subscription.
    if config is not None:
        from leedbox.application.legal import require_expert

        require_expert(db, config, owner, bot)
    # reserve()/funding() still validate the expert and serialize all spending.
    enabled = (
        "ai_lead_magnet_enabled"
        if purpose in {"lead_magnet", "lead_magnet_revision"}
        else "ai_content_enabled"
        if purpose in {"content", "content_search"}
        else "ai_images_enabled"
        if purpose == "image_prompts"
        else "ai_dzen_enabled"
    )
    if "agents" not in visible_sections(db) or not setting(db, enabled):
        raise ValueError("Агент пока выключен владельцем")
    funds = db.get(WalletAccount, (owner, bot, test))
    if funds and funds.balance < 0:
        raise ValueError("После возврата платежа есть задолженность. Обратитесь к поддержке")


def stats(
    engine: Engine,
    config: Settings,
    owner: int,
    bot: int,
    raw: str,
    period: str,
    action_key: str,
    expected_price: int | None = None,
    expected_mode: str | None = None,
) -> dict[str, Any]:
    if not 1 <= len(action_key) <= 100:
        raise ValueError("Некорректный идентификатор поиска")
    slug = channel_slug(raw)
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    start = period_start(today, period).isoformat()
    key = config.zenstat_api_key.get_secret_value()
    if not key:
        raise ValueError("Zenstat пока не настроен")
    with Session(engine) as db, db.begin():
        test = test_mode(config)
        ai_tokens.account(db, owner, bot, test)
        allowed(db, owner, bot, test, config=config)
        snapshot = credits.quoted_value(
            db,
            {
                "purpose": "dzen_search",
                "model": "zenstat",
                "slug": slug,
                "start": start,
                "end": today.isoformat(),
                **(
                    {"provider_cost_rub": str(setting(db, "zenstat_request_cost_rub"))}
                    if credits.enabled(db)
                    else {}
                ),
            },
        )
        action = f"zen:{owner}:{action_key}"
        if len(action) > 100:
            raise ValueError("Слишком длинный идентификатор поиска")
        old = ai_tokens.previous(db, owner, bot, test, action)
        if old:
            if any(
                old.snapshot.get(k) != snapshot.get(k)
                for k in ("purpose", "model", "slug", "start", "end")
            ):
                raise ValueError("Параметры поиска изменились")
            if old.status == "completed" and old.result:
                return old.result
            raise ValueError(
                "Поиск уже запускался. Откройте историю или новый анализ. "
                "Автоматического повтора нет."
            )
        price = search_price(db, "dzen_search")
        if expected_price is not None and price != expected_price:
            raise ValueError("Цена поиска изменилась. Откройте анализ заново")
        mode = ai_tokens.funding(db, config, owner, bot, test, snapshot)
        if mode == "balance":
            ensure_search_margin(db, "dzen_search", price)
        if expected_mode is not None and mode != expected_mode:
            raise ValueError("Способ оплаты изменился. Откройте поиск заново для подтверждения")
        reserved = ai_tokens.reserve(db, owner, bot, test, action, price, snapshot, mode, "search")
        reserved.status = "sent"
        reserved.cost_rub = Decimal(
            str(snapshot.get("provider_cost_rub", setting(db, "zenstat_request_cost_rub")))
        )
        number = reserved.id
    try:
        posts = dzen_posts(key, slug, start, today.isoformat())
    except AgentError as problem:
        with Session(engine) as db, db.begin():
            funds = ai_tokens.account(db, owner, bot, test)
            row = db.get(AIOperation, number, with_for_update=True)
            assert row is not None
            if problem.status is not None and 400 <= problem.status < 500:
                ai_tokens.release_reservation(funds, row)
                row.status = "failed"
            else:
                row.status = "uncertain"
            from leedbox.application.provider_balance import notify

            notify(
                db,
                config,
                bot,
                "zenstat",
                "Ошибка Zenstat. Проверьте баланс пакета и доступность API у провайдера.",
            )
        raise
    report = {
        "slug": slug,
        "start": start,
        "end": today.isoformat(),
        "posts": posts,
        "checked_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),
    }
    with Session(engine) as db, db.begin():
        funds = ai_tokens.account(db, owner, bot, test)
        row = db.get(AIOperation, number, with_for_update=True)
        assert row is not None
        if row.status == "sent":
            ai_tokens.release_reservation(funds, row)
            if posts and row.billing_mode == "balance":
                ai_tokens.charge(funds, row, price)
            row.status, row.result = ("completed" if posts else "failed"), report
            row.balance_after = ai_tokens.balance_after(funds, row)
            row.cost_rub = Decimal(
                str(snapshot.get("provider_cost_rub", setting(db, "zenstat_request_cost_rub")))
            )
        dzen_history.save(db, owner, bot, test, action_key, report)
    return report


@credits.quoted
def quote_generation(
    db: Session,
    selected: dict[str, Any],
    expert: str,
    top_titles: list[str],
    search_token: str | None = None,
) -> dict[str, Any]:
    if not 1 <= len(expert) <= 1000:
        raise ValueError("Описание своего канала: 1–1000 символов")
    system = str(setting(db, "ai_dzen_prompt"))
    system += (
        "\nКонтракт количества имеет приоритет: ровно ПЯТЬ разных заголовков "
        "с id A, B, C, D, E; recommended_id — один из A/B/C/D/E. "
        "Допустимая длина 20–160 символов. Угол и объяснение каждого — кратко."
    )
    inputs = {
        "selected": selected,
        "expert": expert,
        "top_titles": top_titles[:4],
        "article_text": None,
    }
    # UTF-8 byte count is a deliberately conservative upper bound, not an exact tokenizer.
    input_bound = len(system.encode()) + len(json.dumps(inputs, ensure_ascii=False).encode()) + 512
    if input_bound > 18000:
        raise ValueError("Вводные слишком длинные для безопасного резерва")
    return {
        "model": "deepseek-flash",
        "variant_count": 5,
        "search_token": search_token,
        "system": system,
        "inputs": inputs,
        "maximum": input_bound + OUTPUT_LIMIT,
        "output_limit": OUTPUT_LIMIT,
        "article_template": setting(db, "ai_dzen_article_template"),
        "rates": {
            "input": setting(db, "ai_input_usd_per_million"),
            "output": setting(db, "ai_output_usd_per_million"),
            "usd_rub": setting(db, "ai_usd_rub"),
        },
    }


@credits.quoted
def quote_images(db: Session, article: str) -> dict[str, Any]:
    selected = image_prompts.headings(article)
    inputs = {
        "article": article.strip(),
        "headings": selected,
        "sections": [
            {"section_id": f"s{i}", "heading": heading} for i, heading in enumerate(selected, 1)
        ],
    }
    system = str(setting(db, "ai_images_prompt"))
    system += (
        '\nОбязательный контракт: только JSON {"images":[{"section_id":"s1","prompt":"scene"}]}. '
    )
    system += "Этот контракт заменяет прежние требования к длине и техническим параметрам. "
    system += "Вместо heading возвращай section_id из sections. Заголовки не переписывай. "
    system += "Для каждого section_id верни одно короткое английское описание "
    system += "сцены на 20–29 слов, без пояснений, флагов и перечисления запретов. "
    system += "Бот сам добавит 4:3 и no text; итоговый промпт будет не длиннее 40 слов."
    bound = len(system.encode()) + len(json.dumps(inputs, ensure_ascii=False).encode()) + 512
    if bound > 65000:
        raise ValueError("Статья слишком длинная для безопасного резерва")
    return {
        "purpose": "image_prompts",
        "version": 3,
        "model": "deepseek-flash",
        "system": system,
        "inputs": inputs,
        "maximum": bound + 2200,
        "output_limit": 2200,
        "rates": {
            "input": setting(db, "ai_input_usd_per_million"),
            "output": setting(db, "ai_output_usd_per_million"),
            "usd_rub": setting(db, "ai_usd_rub"),
        },
    }


def generate(
    engine: Engine,
    config: Settings,
    owner: int,
    bot: int,
    key: str,
    offer: dict[str, Any],
    *,
    require_confirmation: bool = False,
) -> int:
    api_key = config.deepseek_api_key.get_secret_value()
    if not api_key:
        raise ValueError("DeepSeek пока не настроен")
    test = test_mode(config)
    with Session(engine) as db, db.begin():
        ai_tokens.account(db, owner, bot, test)
        allowed(db, owner, bot, test, offer.get("purpose", "dzen"), config)
        old = ai_tokens.previous(db, owner, bot, test, key)
        if old:
            if old.snapshot != offer:
                raise ValueError("Подтверждение изменилось")
            if old.status == "completed":
                return old.id
            raise ValueError("Операция уже запущена или требует проверки. Повтор не списывается.")
        if offer.get("purpose") == "image_prompts":
            current = quote_images(db, offer["inputs"]["article"])
        elif offer.get("purpose") == "writing_profile":
            current = quote_profile(db, offer["inputs"]["brief"], offer.get("search_token"))
        else:
            current = quote_generation(
                db,
                offer["inputs"]["selected"],
                offer["inputs"]["expert"],
                offer["inputs"]["top_titles"],
                offer.get("search_token"),
            )
        if {k: v for k, v in offer.items() if k != "confirmed_billing"} != current:
            raise ValueError("Условия изменились. Откройте генерацию заново")
        mode = ai_tokens.funding(db, config, owner, bot, test, offer)
        if (require_confirmation or "confirmed_billing" in offer) and offer.get(
            "confirmed_billing"
        ) != mode:
            raise ValueError(
                "Способ оплаты изменился. Вернитесь к задаче и подтвердите новые условия. "
                "Списаний нет."
            )
        row = ai_tokens.reserve(db, owner, bot, test, key, offer["maximum"], offer, mode)
        row.status = "sent"
        number = row.id
    # Never hold DB transactions open during the paid provider call.
    try:
        payload = deepseek_generate(
            api_key, offer["model"], offer["system"], offer["inputs"], offer["output_limit"]
        )
        usage = payload.get("usage")
        ai_tokens.usage_counts(usage, offer["maximum"])
    except PROVIDER_ERRORS as problem:
        from leedbox.application import ai_failures

        with Session(engine) as db, db.begin():
            ai_tokens.account(db, owner, bot, test)
            uncertain = db.get(AIOperation, number, with_for_update=True)
            assert uncertain is not None
            rejected = ai_failures.record(db, config, uncertain, problem)
        if rejected:
            raise ValueError(
                "ИИ временно недоступен. Владелец уведомлён. "
                "Ваш баланс и бесплатная попытка сохранены."
            ) from None
        raise ValueError(
            f"Не удалось получить полный ответ ИИ (операция №{number}). "
            "Резерв сохранён для проверки владельцем; повторного списания нет."
        ) from None
    valid, result = False, None
    failure = "invalid_structure"
    finish_reason = "unknown"
    try:
        choice = payload["choices"][0]
        finish_reason = choice["finish_reason"]
        if finish_reason != "stop":
            failure = "output_truncated" if finish_reason == "length" else "provider_stopped"
            raise ValueError("Ответ не завершён")
        parsed_result = json.loads(choice["message"]["content"])
        if offer.get("purpose") == "image_prompts":
            result = image_prompts.validate_result(parsed_result, offer["inputs"]["headings"])
        elif offer.get("purpose") == "writing_profile":
            result = validate_prompt(parsed_result, offer["inputs"]["brief"])
        else:
            result = validated_result(parsed_result, offer.get("variant_count", 3))
        valid = True
    except (ValueError, KeyError, TypeError, IndexError) as problem:
        if failure == "invalid_structure":
            failure = (
                "invalid_json"
                if isinstance(problem, json.JSONDecodeError)
                else {
                    "Заголовок должен содержать 20–160 символов": "title_length",
                    "Заголовки должны отличаться": "duplicate_titles",
                    "Нужно пять заголовков": "variant_count",
                    "Некорректные варианты": "variant_ids",
                    "Неполный вариант": "missing_variant_fields",
                    "Некорректные поля анализа": "analysis_fields",
                    "Некорректная рекомендация": "recommendation",
                    "Промпт должен быть подробным: 2500–12000 символов": "prompt_length",
                    "Нет разделов или единственного поля заголовка": "prompt_sections",
                    "Неполное содержание разделов промпта": "prompt_content",
                    "В промпте остались внутренние переменные": "template_markers",
                    "Неверный формат промптов изображений": "image_structure",
                    "Промпт нужен к каждому выбранному заголовку": "image_count",
                    "Изменены заголовки или их порядок": "image_headings",
                    "Каждый промпт: 40–80 английских слов": "image_length",
                    "Нужны английский абзац, формат 4:3 и запрет надписей": "image_format",
                    "Сцены не должны повторяться": "image_duplicates",
                    "Неверный объём промпта картинки": "image_length",
                    "В промпте картинки нет описания сцены": "image_empty",
                    "Промпт картинки должен быть на английском": "image_format",
                    "Некорректные идентификаторы разделов": "image_section_ids",
                }.get(str(problem), "invalid_structure")
            )
    with Session(engine) as db, db.begin():
        assert isinstance(usage, dict)
        settled = ai_tokens.settle(db, owner, bot, test, number, usage, result, valid)
        valid = valid and settled.status == "completed" and settled.result is not None
        if valid and offer.get("purpose") == "writing_profile":
            writing_library.ensure(db, owner, bot, test)
        if not valid:
            # Safe diagnostics only: no provider body, user inputs or secrets in error metadata.
            settled.result = {
                "failure": failure,
                "finish_reason": (
                    finish_reason
                    if finish_reason in {"stop", "length", "content_filter"}
                    else "other"
                ),
            }
    if not valid:
        detail = {
            "output_truncated": "Ответ обрезан лимитом длины.",
            "provider_stopped": "Провайдер не завершил ответ.",
            "invalid_json": "ИИ вернул некорректный JSON.",
            "title_length": "Заголовок вышел за допустимую длину.",
            "duplicate_titles": "ИИ повторил заголовки.",
            "variant_count": "ИИ вернул неверное число заголовков.",
            "variant_ids": "ИИ неправильно обозначил варианты заголовков.",
            "missing_variant_fields": "Не заполнено объяснение варианта.",
            "analysis_fields": "Неполные поля сущностей или маркеров.",
            "recommendation": "Неверно указан рекомендуемый вариант.",
            "prompt_length": "Недостаточный или чрезмерный объём промпта.",
            "prompt_sections": "В промпте отсутствует раздел или поле заголовка.",
            "prompt_content": "ИИ вернул неполное содержание редакционных инструкций.",
            "template_markers": "В промпте остались внутренние переменные.",
            "image_structure": "Неверный JSON-формат промптов картинок.",
            "image_count": "Количество промптов не совпало с количеством заголовков.",
            "image_headings": "ИИ изменил заголовки или их порядок.",
            "image_length": "ИИ вернул чрезмерно большой промпт картинки.",
            "image_empty": "В ответе ИИ отсутствует описание сцены.",
            "image_section_ids": "ИИ не связал промпты с нужными разделами статьи.",
            "image_format": "Нарушены язык, формат абзаца или обязательные параметры картинки.",
            "image_duplicates": "ИИ вернул одинаковые промпты.",
        }.get(failure, "Ответ не прошёл проверку структуры.")
        raise ValueError(
            f"ИИ вернул неподходящий результат (операция #{number}). {detail} "
            "Пользовательские токены не списаны; резерв освобождён."
        )
    return number


def operation(db: Session, owner: int, bot: int, test: bool, number: int) -> AIOperation:
    row = db.get(AIOperation, number)
    if row is None or (row.owner_user_id, row.bot_id, row.test_mode) != (owner, bot, test):
        raise ObjectUnavailable
    return row


def package_offer(db: Session, config: Settings, package_id: str) -> dict[str, Any]:
    if credits.enabled(db):
        raise ValueError("Пакеты отключены. Купите кредиты на выбранную сумму через «Мой аккаунт».")
    if not setting(db, "wallet_enabled") or not setting(db, "wallet_visible"):
        raise ValueError("Покупка с рублёвого баланса пока выключена")
    if config.robokassa_test != test_mode(config):
        raise ValueError("Режимы денежных и AI-балансов не совпадают")
    items = validate_packages(setting(db, "ai_token_packages"))
    item = next((item for item in items if item["id"] == package_id and item["enabled"]), None)
    if item is None:
        raise ValueError("Пакет пока недоступен")
    return {**item, "test": test_mode(config), **purchase_terms(db, config)}


def buy_package(
    db: Session, config: Settings, owner: int, bot: int, key: str, offer: dict[str, Any]
) -> AIOperation:
    test = test_mode(config)
    ai_tokens.account(db, owner, bot, test)
    old = ai_tokens.previous(db, owner, bot, test, key)
    snapshot = {"offer": offer}
    if old:
        if old.kind != "purchase" or old.snapshot != {**snapshot, "tokens": offer["tokens"]}:
            raise ValueError("Подтверждение относится к другой покупке")
        return old
    if offer != package_offer(db, config, offer["id"]):
        raise ValueError("Цена пакета изменилась")
    funds = wallet_account(db, owner, bot, test)
    price = Decimal(offer["price_rub"])
    if funds.balance < price:
        raise ValueError("Недостаточно рублей. Пополните рублёвый баланс")
    funds.balance -= price
    db.add(
        WalletEntry(
            owner_user_id=owner,
            bot_id=bot,
            test_mode=test,
            kind="purchase",
            amount=-price,
            balance_after=funds.balance,
            action_key="ai:" + key,
            snapshot={"purpose": "ai_tokens", **offer},
        )
    )
    return ai_tokens.grant(db, owner, bot, test, key, offer["tokens"], "purchase", snapshot)


def token_offer(db: Session, config: Settings, amount: Decimal) -> dict[str, Any]:
    from decimal import ROUND_FLOOR

    from leedbox.domain.payments import payment_amount

    if not setting(db, "wallet_enabled") or not setting(db, "wallet_visible"):
        raise ValueError("Покупка токенов пока выключена владельцем")
    if config.robokassa_test != test_mode(config):
        raise ValueError("Режимы денежных и AI-балансов не совпадают")
    amount = payment_amount(str(amount), "RUB")
    minimum = Decimal(str(setting(db, "ai_token_purchase_min_rub")))
    credit = credits.enabled(db)
    price = credits.unit_price(db) if credit else Decimal(str(setting(db, "ai_token_price_rub")))
    if not minimum <= amount <= Decimal("100000"):
        raise ValueError(f"Введите сумму от {minimum:.2f} до 100 000 ₽")
    if not price.is_finite() or price <= 0:
        raise ValueError("Цена AI-токена не настроена")
    tokens = int((amount / price).to_integral_value(rounding=ROUND_FLOOR))
    if not 1 <= tokens <= 100_000_000:
        raise ValueError("Слишком большая сумма для текущей цены токена")
    return {
        "mode": "amount",
        **({"unit": "credits"} if credit else {}),
        **({"requested_rub": str(amount)} if credit else {}),
        "price_rub": str((price * tokens).quantize(Decimal("0.01"))) if credit else str(amount),
        "unit_price_rub": str(price),
        "tokens": tokens,
        "test": test_mode(config),
        **purchase_terms(db, config),
    }


def buy_tokens(
    db: Session, config: Settings, owner: int, bot: int, key: str, offer: dict[str, Any]
) -> AIOperation:
    test = test_mode(config)
    if not key or len(key) > 90:
        raise ValueError("Неверное подтверждение покупки")
    ai_tokens.account(db, owner, bot, test)
    old = ai_tokens.previous(db, owner, bot, test, key)
    unit = {"unit": "credits"} if offer.get("unit") == "credits" else {}
    snapshot = {"offer": offer, "tokens": offer["tokens"], **unit}
    if old:
        if old.kind != "purchase" or old.snapshot != snapshot:
            raise ValueError("Подтверждение относится к другой покупке")
        return old
    if offer != token_offer(db, config, Decimal(offer.get("requested_rub", offer["price_rub"]))):
        raise ValueError("Цена или условия изменились. Подтвердите покупку заново")
    funds = wallet_account(db, owner, bot, test)
    price = Decimal(offer["price_rub"])
    if funds.balance < price:
        raise ValueError("Недостаточно рублей. Пополните рублёвый баланс")
    funds.balance -= price
    db.add(
        WalletEntry(
            owner_user_id=owner,
            bot_id=bot,
            test_mode=test,
            kind="purchase",
            amount=-price,
            balance_after=funds.balance,
            action_key="ai:" + key,
            snapshot={"purpose": "ai_tokens", **offer},
        )
    )
    return ai_tokens.grant(
        db, owner, bot, test, key, offer["tokens"], "purchase", {"offer": offer, **unit}
    )
