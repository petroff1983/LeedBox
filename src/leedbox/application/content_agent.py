"""Durable three-call pipeline with a single up-front reservation and checkpoints."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from leedbox.application import agents, ai_failures, ai_tokens, credits
from leedbox.application.settings import setting
from leedbox.application.versions import fingerprint
from leedbox.domain import content_agent as domain
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure import apify
from leedbox.infrastructure.agents import (
    PROVIDER_ERRORS,
    TRANSPORT_ERRORS,
    AgentError,
    deepseek_generate,
    rejected_request,
)
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AIOperation, ContentTask, User

TERMINAL = {"completed", "failed", "uncertain", "cancelled"}
STATUSES = {
    "draft": "Материалы ещё загружаются",
    "queued": "Запускаю процесс…",
    "waiting": "Собираю публикации конкурента…",
    "completed": "Готово — 3 поста",
    "cancelled": "Задача отменена",
    "failed": "Задача остановлена. Генерация не списана; сбор, если завершился, оплачен отдельно.",
    "uncertain": "Нет подтверждённого ответа API. Повтора нет; резерв требует проверки владельцем.",
}


def owner_id(db: Session, telegram: int) -> int:
    value = db.scalar(select(User.id).where(User.telegram_id == telegram))
    if value is None:
        raise ObjectUnavailable
    return value


def owned(
    db: Session, config: Settings, owner: int, bot: int, token: str, *, locked: bool = False
) -> ContentTask:
    row = db.get(ContentTask, token, with_for_update=locked)
    if row is None or (row.owner_user_id, row.bot_id, row.test_mode) != (
        owner,
        bot,
        agents.test_mode(config),
    ):
        raise ObjectUnavailable
    return row


def latest(
    db: Session, config: Settings, owner: int, bot: int, kind: str = "content"
) -> list[ContentTask]:
    return list(
        db.scalars(
            select(ContentTask)
            .where(
                ContentTask.owner_user_id == owner,
                ContentTask.bot_id == bot,
                ContentTask.test_mode == agents.test_mode(config),
                func.coalesce(ContentTask.payload["kind"].astext, "content") == kind,
            )
            .order_by(ContentTask.created_at.desc())
            .limit(10)
        )
    )


def create(
    db: Session, config: Settings, owner: int, bot: int, chat: int, mode: str
) -> ContentTask:
    agents.allowed(db, owner, bot, agents.test_mode(config), "content", config)
    if mode not in {"manual", "link"}:
        raise ValueError("Неизвестный способ загрузки")
    if mode == "link" and not config.apify_api_token.get_secret_value():
        raise ValueError("Сбор по ссылке пока не настроен. Можно загрузить посты вручную.")
    ai_tokens.account(db, owner, bot, agents.test_mode(config))
    active = db.scalar(
        select(ContentTask)
        .where(
            ContentTask.owner_user_id == owner,
            ContentTask.bot_id == bot,
            ContentTask.test_mode == agents.test_mode(config),
            ContentTask.payload["mode"].astext == mode,
            ContentTask.status.not_in(TERMINAL),
        )
        .order_by(ContentTask.created_at.desc())
        .limit(1)
    )
    if active:
        if (
            mode == "manual"
            and active.status == "draft"
            and not active.operation_id
            and not active.steps
            and "patterns_version" not in active.payload
        ):
            change(active, patterns_version=2)
        return active  # Never silently discard an unfinished upload or launch a parallel job.
    row = ContentTask(
        token=uuid4().hex[:16],
        owner_user_id=owner,
        bot_id=bot,
        test_mode=agents.test_mode(config),
        chat_id=chat,
        status="draft",
        stage=0,
        revision=1,
        payload={
            "mode": mode,
            "workflow": "competitor" if mode == "link" else "patterns",
            "phase": "materials",
            "parts": [],
            "days": 30,
            "period_selected": False,
            "target": "telegram",
            "brief": domain.AUTO_BRIEF,
            "patterns_version": 2,
        },
        steps=[],
        provider={},
        delivered=False,
    )
    db.add(row)
    db.flush()
    return row


def change(row: ContentTask, **values: Any) -> None:
    if row.status != "draft":
        raise ValueError("Задача уже запущена. Откройте её результат или создайте новую.")
    row.payload = {**row.payload, **values}
    row.revision += 1


def add_part(
    row: ContentTask, identity: int, text: str, *, source_platform: str | None = None
) -> None:
    if row.payload.get("phase") != "materials" or row.payload.get("mode") != "manual":
        raise ValueError("Сначала выберите ручную загрузку")
    text = text.strip()
    if not text:
        raise ValueError("Пришлите текст, пересланную публикацию или файл .txt/.md.")
    parts = row.payload.get("parts", [])
    if any(p["id"] == str(identity) for p in parts):
        return
    incoming = {"id": str(identity), "text": text}
    if source_platform in domain.ACTIVE_PLATFORMS:
        incoming["source_platform"] = source_platform
    if len(parts) >= 50 or domain.byte_size(parts + [incoming]) > domain.MAX_BYTES:
        raise ValueError(
            "Для одной задачи — до 50 сообщений и 20 КБ текста. Уже принятые материалы сохранены."
        )
    change(row, parts=parts + [incoming])


@credits.quoted
def quote(db: Session, row: ContentTask) -> dict[str, Any]:
    payload = row.payload
    if payload.get("workflow") == "competitor":
        if payload.get("source_platform") != "telegram":
            raise ValueError(
                "Теперь анализируем только Telegram. Пришлите ссылку на Telegram-канал."
            )
        domain.source_url(payload.get("url", ""), payload.get("source_platform", ""))
        price = agents.search_price(db, "content_search")
        return {
            "purpose": "content_search",
            "workflow": "competitor",
            "version": 2,
            "model": "apify",
            "maximum": price,
            "search_price": price,
            "revision": row.revision,
            "source_mode": "link",
            "url": payload["url"],
            "days": payload["days"],
            "source_platform": payload["source_platform"],
            "apify_budget": str(
                min(Decimal("0.04"), Decimal(str(setting(db, "ai_content_apify_budget_usd"))))
            ),
            "collection_limit": 20,
            "rates": {"usd_rub": setting(db, "ai_usd_rub")},
        }
    domain.brief_text(payload.get("brief", ""))
    if payload.get("target") not in domain.ACTIVE_PLATFORMS:
        raise ValueError("Выберите площадку публикации")
    if payload["mode"] == "manual" and not payload.get("parts"):
        raise ValueError("Сначала пришлите посты")
    if payload["mode"] == "link":
        if payload.get("source_platform") != "telegram":
            raise ValueError("Threads отключён. Пришлите ссылку на Telegram-канал.")
        domain.source_url(payload.get("url", ""), payload.get("source_platform", ""))
    if payload.get("patterns_version") == 2:
        return patterns_quote(db, row)
    systems = [
        domain.COMMON
        + str(setting(db, "ai_content_" + stage + "_prompt"))
        + "\nОбязательный контракт результата: "
        + domain.CONTRACTS[index]
        for index, stage in enumerate(("plan", "draft", "edit"))
    ]
    source_bound = (
        domain.byte_size(payload.get("parts", []))
        if payload["mode"] == "manual"
        else domain.MAX_BYTES
    )
    maximum = source_bound + sum(len(s.encode()) for s in systems) + 44000 + sum(domain.LIMITS)
    if maximum > 100000:
        raise ValueError("Инструкция или материалы слишком велики для безопасного резерва")
    return {
        "purpose": "content",
        "version": 1,
        "model": "deepseek-flash",
        "maximum": maximum,
        "systems": systems,
        "revision": row.revision,
        "target": payload["target"],
        "brief": payload["brief"],
        "source_mode": payload["mode"],
        "search_price": int(setting(db, "ai_content_search_tokens"))
        if payload["mode"] == "link"
        else 0,
        "apify_budget": str(setting(db, "ai_content_apify_budget_usd")),
        "rates": {
            "input": setting(db, "ai_input_usd_per_million"),
            "output": setting(db, "ai_output_usd_per_million"),
            "usd_rub": setting(db, "ai_usd_rub"),
        },
    }


def patterns_quote(db: Session, row: ContentTask) -> dict[str, Any]:
    payload = row.payload
    generation = payload.get("generation_only", False)
    requests = payload.get("requests", [])
    topics = domain.requested_topics(requests)
    if generation and not 1 <= len(topics) <= domain.MAX_POSTS:
        raise ValueError("Сначала укажите темы и количество постов")
    sources = domain.source_materials(payload["parts"])
    limit = min(12000, max(1800, len(topics) * 1200))
    limits = [1800, limit, limit]
    systems = [
        domain.COMMON
        + str(setting(db, "ai_content_plan_prompt"))
        + domain.ANALYZE
        + domain.ANALYSIS_CONTRACT,
        domain.COMMON
        + domain.batch_instruction(str(setting(db, "ai_content_draft_prompt")), len(topics))
        + domain.generation_contract(requests),
        domain.COMMON
        + domain.batch_instruction(str(setting(db, "ai_content_edit_prompt")), len(topics))
        + domain.generation_contract(requests),
    ]
    stages = [1, 2] if generation else [0]
    # UTF-8 bytes conservatively bound tokens, including a validated maximum-size draft.
    base = (
        domain.byte_size(
            {
                "sources": sources,
                **domain.generation_inputs(
                    payload["brief"], payload["target"], payload.get("analysis", {}), requests
                ),
            }
        )
        + 4096
    )
    maximum = sum(base + len(systems[i].encode()) + limits[i] for i in stages)
    if generation:
        maximum += len(topics) * 11000 + 1024
    if maximum > 250000:
        raise ValueError("Материалы или инструкция слишком велики. Уменьшите партию постов.")
    return {
        "purpose": "content",
        "version": 2,
        "content_phase": "generation" if generation else "analysis",
        "max_repairs": 1 if generation else 0,
        "model": "deepseek-flash",
        "maximum": maximum,
        "systems": systems,
        "limits": limits,
        "revision": row.revision,
        "target": payload["target"],
        "brief": payload["brief"],
        "source_mode": "manual",
        "search_price": 0,
        "requests": requests,
        "rates": {
            "input": setting(db, "ai_input_usd_per_million"),
            "output": setting(db, "ai_output_usd_per_million"),
            "usd_rub": setting(db, "ai_usd_rub"),
        },
    }


def generation_task(
    db: Session, config: Settings, parent: ContentTask, raw: str, identity: str
) -> ContentTask:
    """A new, separately billed batch reuses an immutable, already paid analysis."""
    if parent.payload.get("generation_only"):
        parent = owned(
            db,
            config,
            parent.owner_user_id,
            parent.bot_id,
            parent.payload["analysis_token"],
            locked=True,
        )
    if parent.status != "completed" or parent.payload.get("phase") != "request":
        raise ValueError("Сначала дождитесь анализа исходных постов")
    requests = domain.generation_request(raw)
    active = db.scalar(
        select(ContentTask)
        .where(
            ContentTask.owner_user_id == parent.owner_user_id,
            ContentTask.bot_id == parent.bot_id,
            ContentTask.test_mode == parent.test_mode,
            ContentTask.payload["analysis_token"].astext == parent.token,
            ContentTask.status.not_in(TERMINAL),
        )
        .order_by(ContentTask.created_at.desc())
        .limit(1)
    )
    if active:
        return active
    duplicate = db.scalar(
        select(ContentTask)
        .where(
            ContentTask.owner_user_id == parent.owner_user_id,
            ContentTask.bot_id == parent.bot_id,
            ContentTask.test_mode == parent.test_mode,
            ContentTask.payload["analysis_token"].astext == parent.token,
            ContentTask.payload["request_identity"].astext == identity,
        )
        .limit(1)
    )
    if duplicate:
        return duplicate
    analysis = parent.steps[0]["result"]
    row = ContentTask(
        token=uuid4().hex[:16],
        owner_user_id=parent.owner_user_id,
        bot_id=parent.bot_id,
        test_mode=parent.test_mode,
        chat_id=parent.chat_id,
        status="draft",
        stage=1,
        revision=1,
        payload={
            **parent.payload,
            "target": "telegram",
            "phase": "confirm",
            "generation_only": True,
            "analysis_token": parent.token,
            "analysis": analysis,
            "requests": requests,
            "request_identity": identity,
        },
        steps=[
            {
                "stage": 0,
                "result": analysis,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        ],
        provider={},
        delivered=False,
    )
    db.add(row)
    db.flush()
    return row


def launch(
    db: Session, config: Settings, owner: int, bot: int, token: str, confirmation: str
) -> ContentTask:
    ai_tokens.account(db, owner, bot, agents.test_mode(config))
    row = owned(db, config, owner, bot, token, locked=True)
    if row.status != "draft":
        return row  # An old callback is never a second purchase.
    if row.payload.get("workflow") == "competitor" and not row.payload.get("period_selected"):
        raise ValueError("Сначала пришлите ссылку и выберите период поиска")
    agents.allowed(db, owner, bot, row.test_mode, "content", config)
    if (
        row.payload.get("workflow") != "competitor"
        and not config.deepseek_api_key.get_secret_value()
    ):
        raise ValueError("DeepSeek пока не настроен владельцем")
    offer = quote(db, row)
    mode = ai_tokens.funding(db, config, owner, bot, row.test_mode, offer)
    if fingerprint({**offer, "billing": mode})[:12] != confirmation:
        raise ValueError("Материалы, инструкция или цена изменились. Обновите экран задачи.")
    if offer["source_mode"] == "link" and not config.apify_api_token.get_secret_value():
        raise ValueError("Сбор по ссылке пока не настроен")
    if mode == "balance" and offer["source_mode"] == "link":
        agents.ensure_search_margin(db, "content_search", int(offer["search_price"]))
    if offer.get("workflow") == "competitor":
        search = ai_tokens.reserve(
            db,
            owner,
            bot,
            row.test_mode,
            "competitor:" + token,
            offer["maximum"],
            offer,
            mode,
            "search",
        )
        row.operation_id = row.search_operation_id = search.id
        row.payload = {**row.payload, "offer": offer}
        row.status, row.stage, row.ready_at = "queued", -1, datetime.now(UTC)
        return row
    reserved = ai_tokens.reserve(
        db, owner, bot, row.test_mode, "content:" + token, offer["maximum"], offer, mode
    )
    row.operation_id = reserved.id
    if offer["search_price"]:
        search = ai_tokens.reserve(
            db,
            owner,
            bot,
            row.test_mode,
            "content-search:" + token,
            offer["search_price"],
            {
                "purpose": "content_search",
                "model": "apify",
                "price": offer["search_price"],
                "rates": offer["rates"],
            },
            mode,
            "search",
        )
        row.search_operation_id = search.id
    row.payload = {**row.payload, "offer": offer}
    row.status, row.stage = (
        "queued",
        (-1 if offer["source_mode"] == "link" else 1 if row.payload.get("generation_only") else 0),
    )
    row.ready_at = datetime.now(UTC)
    return row


def period_launch(
    db: Session, config: Settings, row: ContentTask, days: int, digest: str
) -> ContentTask:
    """Explicit period selection is the purchase confirmation, never an extra click."""
    if row.status != "draft":
        return row
    if row.payload.get("workflow") != "competitor" or not 1 <= days <= 365:
        raise ValueError("Укажите период от 1 до 365 дней")
    offer = quote(db, row)
    mode = ai_tokens.funding(db, config, row.owner_user_id, row.bot_id, row.test_mode, offer)
    if fingerprint({**offer, "billing": mode})[:12] != digest:
        raise ValueError("Ссылка или стоимость изменились. Откройте подборку заново.")
    change(row, days=days, period_selected=True)
    offer = quote(db, row)
    digest = fingerprint({**offer, "billing": mode})[:12]
    return launch(db, config, row.owner_user_id, row.bot_id, row.token, digest)


def sums(row: ContentTask) -> dict[str, int]:
    incoming = sum(step.get("usage", {}).get("prompt_tokens", 0) for step in row.steps)
    outgoing = sum(step.get("usage", {}).get("completion_tokens", 0) for step in row.steps)
    return {
        "prompt_tokens": incoming,
        "completion_tokens": outgoing,
        "total_tokens": incoming + outgoing,
    }


def finish(db: Session, row: ContentTask, valid: bool, status: str = "failed") -> None:
    if row.payload.get("kind") == "lead_magnet":
        from leedbox.application.lead_magnet_agent import finish as finish_lm

        finish_lm(db, row, valid, status)
        return
    if row.payload.get("workflow") == "competitor":
        if row.search_operation_id:
            search = db.get(AIOperation, row.search_operation_id, with_for_update=True)
            if search and search.status not in {"completed", "failed"}:
                search.cost_usd = sum(
                    (
                        Decimal(r["cost_usd"])
                        for r in row.provider.get("runs", [])
                        if "cost_usd" in r
                    ),
                    Decimal(0),
                )
                search.cost_rub = search.cost_usd * Decimal(
                    row.payload["offer"]["rates"]["usd_rub"]
                )
                ai_tokens.release_reservation(
                    ai_tokens.account(db, row.owner_user_id, row.bot_id, row.test_mode), search
                )
                search.status = "failed"
        row.status = "completed" if valid else status
        return
    if row.operation_id:
        op = db.get(AIOperation, row.operation_id, with_for_update=True)
        if op and op.status not in {"completed", "failed"}:
            usage = sums(row)
            if usage["total_tokens"]:
                final = (
                    {
                        **row.steps[-1].get("result", {}),
                        "analysis": row.steps[0]["result"],
                        "sources": row.payload.get("parts", []),
                        "source_mode": row.payload.get("mode"),
                        "checked": row.provider.get("checked"),
                        "analysis_only": row.payload.get("patterns_version") == 2
                        and not row.payload.get("generation_only"),
                    }
                    if valid
                    else None
                )
                ai_tokens.settle(
                    db, row.owner_user_id, row.bot_id, row.test_mode, op.id, usage, final, valid
                )
            else:
                funds = ai_tokens.account(db, row.owner_user_id, row.bot_id, row.test_mode)
                ai_tokens.release_reservation(funds, op)
                op.status = "failed"
    if row.search_operation_id:
        search = db.get(AIOperation, row.search_operation_id, with_for_update=True)
        if search and search.status not in {"completed", "failed"}:
            search.cost_usd = sum(
                (Decimal(r["cost_usd"]) for r in row.provider.get("runs", []) if "cost_usd" in r),
                Decimal(0),
            )
            search.cost_rub = search.cost_usd * Decimal(row.payload["offer"]["rates"]["usd_rub"])
            funds = ai_tokens.account(db, row.owner_user_id, row.bot_id, row.test_mode)
            ai_tokens.release_reservation(funds, search)
            search.status = "failed"
    row.status = "completed" if valid else status
    if (
        valid
        and row.payload.get("patterns_version") == 2
        and not row.payload.get("generation_only")
    ):
        row.payload = {**row.payload, "phase": "request"}


def cancel(db: Session, row: ContentTask) -> None:
    if row.status in TERMINAL:
        return
    if row.status == "working":
        row.payload = {**row.payload, "cancel_requested": True}
    else:
        finish(db, row, False, "cancelled")


def state_text(row: ContentTask) -> str:
    if row.payload.get("patterns_version") == 2 and row.payload.get("workflow") == "patterns":
        if row.status == "completed":
            return "Анализ сохранён" if row.payload.get("phase") == "request" else "Посты готовы"
        if row.status == "working" and row.stage == 0:
            return "Изучаю темы и паттерны исходных постов…"
        if row.status == "working" and row.stage == 1:
            return f"Готовлю посты: {len(domain.requested_topics(row.payload['requests']))}…"
    if row.payload.get("workflow") == "competitor" and row.status == "completed":
        return (
            f"Найдено публикаций: {len(row.payload.get('parts', []))}. Ссылки и метрики сохранены."
        )
    if row.payload.get("workflow") == "competitor" and row.status == "failed":
        return "Сбор не дал результата или завершился ошибкой. Списание за поиск не выполнено."
    if row.payload.get("cancel_requested") and row.status not in TERMINAL:
        return "Останавливаю задачу после текущего запроса…"
    if row.status == "working":
        return {
            -1: "Запускаю сбор публикаций…",
            0: "Анализирую паттерны, выбираю темы и готовлю контент-план…",
            1: "Готовлю 3 черновика…",
            2: "Дорабатываю тексты и проверяю формат…",
        }.get(row.stage, "Собираю авторские продолжения Threads…")
    return STATUSES.get(row.status, "Обрабатываю задачу…")


def claim(engine: Engine, config: Settings, bot: int) -> str | None:
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        stale = list(
            db.scalars(
                select(ContentTask)
                .where(
                    ContentTask.bot_id == bot,
                    ContentTask.test_mode == agents.test_mode(config),
                    ContentTask.status == "working",
                    ContentTask.locked_at < now - timedelta(minutes=10),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for expired in stale:
            expired.status = "uncertain"
            for number in (expired.operation_id, expired.search_operation_id):
                op = db.get(AIOperation, number) if number else None
                if op and op.status not in {"completed", "failed"}:
                    op.status = "uncertain"
                    ai_failures.alert(db, config, bot, op.id, AgentError())
        row = db.scalar(
            select(ContentTask)
            .where(
                ContentTask.bot_id == bot,
                ContentTask.test_mode == agents.test_mode(config),
                ContentTask.status.in_({"queued", "waiting"}),
                ContentTask.ready_at <= now,
            )
            .order_by(ContentTask.ready_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.status, row.locked_at = "working", now
        return row.token


def _search_done(db: Session, row: ContentTask) -> None:
    if row.search_operation_id is None:
        return
    op = db.get(AIOperation, row.search_operation_id, with_for_update=True)
    if op and op.status not in {"completed", "failed"}:
        funds = ai_tokens.account(db, row.owner_user_id, row.bot_id, row.test_mode)
        ai_tokens.release_reservation(funds, op)
        ai_tokens.charge(funds, op, op.reserve_tokens)
        op.status, op.result = (
            "completed",
            {"posts": len(row.payload["parts"]), "provider": row.provider},
        )
        if row.payload.get("workflow") == "competitor":
            op.result = {
                "source_links": row.payload["parts"],
                "channel": row.payload["url"],
                "source_platform": row.payload["source_platform"],
                "days": row.payload["days"],
                "checked": row.provider.get("checked"),
            }
        op.balance_after = ai_tokens.balance_after(funds, op)
        costs = [r.get("cost_usd") for r in row.provider.get("runs", [])]
        op.cost_usd = sum((Decimal(c) for c in costs if c is not None), Decimal(0))
        op.cost_rub = op.cost_usd * Decimal(row.payload["offer"]["rates"]["usd_rub"])


def advance(engine: Engine, config: Settings, token: str) -> None:
    """Exactly one external step. Mark sent before the request, persist before advancing."""
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        is_lm = row is not None and row.payload.get("kind") == "lead_magnet"
    if is_lm:
        from leedbox.application.lead_magnet_agent import advance as advance_lm

        advance_lm(engine, config, token)
        return
    with Session(engine) as db, db.begin():
        row = db.get(ContentTask, token)
        if row is None or row.status != "working":
            return
        owner, bot, test, stage = row.owner_user_id, row.bot_id, row.test_mode, row.stage
        payload, provider, steps = dict(row.payload), dict(row.provider), list(row.steps)
        ai_tokens.account(db, owner, bot, test)
        try:
            agents.allowed(db, owner, bot, test, "content", config)
        except (ValueError, ObjectUnavailable):
            finish(db, row, False)
            return
        if payload.get("cancel_requested"):
            finish(db, row, False, "cancelled")
            return
        offer = payload["offer"]
        op = db.get(AIOperation, row.operation_id)
        if op is None or op.status in {"failed", "completed"}:
            row.status = "failed"
            return
        op.status = "sent"
    try:
        if stage < 0:
            run_kind = "replies" if stage == -2 else payload["source_platform"]
            runs = provider.get("runs", [])
            current = next((r for r in runs if r["kind"] == run_kind), None)
            if current is None:
                if run_kind in {"threads", "replies"}:
                    raise ValueError("Threads отключён. Новых платных запусков не будет.")
                roots = [p["url"] for p in payload.get("parts", [])[:5]]
                budget = str(
                    Decimal(offer["apify_budget"])
                    / (
                        2
                        if payload["source_platform"] == "threads"
                        and payload.get("workflow") != "competitor"
                        else 1
                    )
                )
                data = apify.start(
                    config.apify_api_token.get_secret_value(),
                    run_kind,
                    payload["url"],
                    payload["days"],
                    budget,
                    roots,
                    **({"competitor": True} if payload.get("workflow") == "competitor" else {}),
                )
                provider["runs"] = runs + [{"kind": run_kind, "id": data["id"]}]
                new_status, next_stage = "waiting", stage
                parts = None
            else:
                data = apify.poll(config.apify_api_token.get_secret_value(), current["id"])
                # HTTP GET can safely be retried next cycle. Paid POST is never repeated.
                if data.get("status") not in {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}:
                    if datetime.now(UTC) - datetime.fromisoformat(
                        provider["started_at"]
                    ) > timedelta(minutes=8):
                        raise ValueError("Сбор не завершился за отведённое время")
                    new_status, next_stage, parts = "waiting", stage, None
                elif data["status"] != "SUCCEEDED":
                    raise ValueError("Сбор публикаций не завершён провайдером")
                else:
                    cost = apify.run_cost(data)
                    if (
                        cost is not None
                        and Decimal(str(cost)).is_finite()
                        and Decimal(str(cost)) >= 0
                    ):
                        current["cost_usd"] = str(cost)
                    raw = apify.items(
                        config.apify_api_token.get_secret_value(),
                        data["defaultDatasetId"],
                        **({"limit": 20} if payload.get("workflow") == "competitor" else {}),
                    )
                    parts = (
                        apify.attach_replies(payload["parts"], raw)
                        if stage == -2
                        else apify.normalized(
                            raw,
                            run_kind,
                            payload["url"],
                            payload["days"],
                            links_only=payload.get("workflow") == "competitor",
                        )
                    )
                    if not parts:
                        raise ValueError("В выбранной выборке нет подходящих публикаций")
                    provider["checked"] = len(raw)
                    next_stage = (
                        -2
                        if stage == -1
                        and run_kind == "threads"
                        and payload.get("workflow") != "competitor"
                        else 0
                    )
                    new_status = "queued"
            provider.setdefault("started_at", datetime.now(UTC).isoformat())
            with Session(engine) as db, db.begin():
                ai_tokens.account(db, owner, bot, test)
                row = owned(db, config, owner, bot, token, locked=True)
                row.provider = provider
                if parts is not None:
                    row.payload = {**row.payload, "parts": parts}
                if row.payload.get("cancel_requested"):
                    finish(db, row, False, "cancelled")
                else:
                    row.status, row.stage = new_status, next_stage
                    row.ready_at = datetime.now(UTC) + timedelta(
                        seconds=3 if new_status == "waiting" else 0
                    )
                    if next_stage == 0:
                        _search_done(db, row)
                        if payload.get("workflow") == "competitor":
                            row.status = "completed"
            return
        inputs: dict[str, Any] = {"brief": payload["brief"], "target": payload["target"]}
        if stage == 0:
            inputs["sources"] = [
                {
                    "id": p["id"],
                    "text": p["text"],
                    "metrics": p.get("metrics"),
                    "incomplete": p.get("thread_incomplete", False),
                }
                for p in (
                    domain.source_materials(payload["parts"])
                    if offer.get("version") == 2
                    else payload["parts"]
                )
            ]
        else:
            inputs["plan"] = steps[0]["result"]
            if payload.get("generation_only"):
                inputs = domain.generation_inputs(
                    payload["brief"], payload["target"], steps[0]["result"], payload["requests"]
                )
            if stage == 2:
                inputs["drafts"] = steps[1]["result"]
        system = offer["systems"][stage]
        previous = sum(s["usage"]["total_tokens"] for s in steps)
        remaining = offer["maximum"] - previous
        limit = offer.get("limits", domain.LIMITS)[stage]
        if domain.byte_size(inputs) + len(system.encode()) + 512 + limit > remaining:
            raise ValueError("Для следующего этапа недостаточно согласованного резерва")
        response = deepseek_generate(
            config.deepseek_api_key.get_secret_value(),
            offer["model"],
            system,
            inputs,
            limit,
        )
        usage = response.get("usage")
        ai_tokens.usage_counts(usage, remaining)
        assert isinstance(usage, dict)
        recorded = {"stage": stage, "usage": usage}
        # Save known usage even for invalid JSON, then release rather than charging a bad result.
        steps.append(recorded)
        message = response["choices"][0]
        if message.get("finish_reason") != "stop":
            raise ValueError("Ответ ИИ не завершён")
        if stage == 0:
            raw_result = json.loads(message["message"]["content"])
            result = (
                domain.analysis_result(raw_result, domain.source_materials(payload["parts"]))
                if offer.get("version") == 2
                else domain.plan_result(raw_result, {p["id"] for p in payload["parts"]})
            )
        else:
            topics = (
                domain.requested_topics(payload["requests"])
                if payload.get("generation_only")
                else None
            )
            try:
                result = domain.posts_result(
                    json.loads(message["message"]["content"]), payload["target"], topics
                )
            except (ValueError, KeyError, TypeError, IndexError):
                if (
                    not offer.get("max_repairs")
                    or any(s.get("repair_attempted") for s in steps)
                    or not domain.repairable_posts(message["message"]["content"])
                ):
                    raise
                repair_system, repair_inputs = domain.repair_request(
                    message["message"]["content"], inputs, payload["requests"]
                )
                remaining -= usage["total_tokens"]
                if (
                    domain.byte_size(repair_inputs) + len(repair_system.encode()) + 512 + limit
                    > remaining
                ):
                    raise ValueError(
                        "Для исправления формата недостаточно согласованного резерва"
                    ) from None
                # A known invalid response may be corrected once within the approved reserve.
                # Record the attempt BEFORE the call; an interrupted paid request is never retried.
                recorded["repair_attempted"] = True
                with Session(engine) as db, db.begin():
                    ai_tokens.account(db, owner, bot, test)
                    current = owned(db, config, owner, bot, token, locked=True)
                    current.steps = steps
                    if current.payload.get("cancel_requested"):
                        finish(db, current, False, "cancelled")
                        return
                corrected = deepseek_generate(
                    config.deepseek_api_key.get_secret_value(),
                    offer["model"],
                    repair_system,
                    repair_inputs,
                    limit,
                )
                extra = corrected.get("usage")
                ai_tokens.usage_counts(extra, remaining)
                assert isinstance(extra, dict)
                recorded["usage"] = {
                    key: usage[key] + extra[key]
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
                recorded["repair_usage"] = extra
                fixed = corrected["choices"][0]
                if fixed.get("finish_reason") != "stop":
                    raise ValueError("Ответ ИИ не завершён") from None
                result = domain.posts_result(
                    json.loads(fixed["message"]["content"]), payload["target"], topics
                )
        recorded["result"] = result
        with Session(engine) as db, db.begin():
            ai_tokens.account(db, owner, bot, test)
            row = owned(db, config, owner, bot, token, locked=True)
            row.steps = steps
            if row.payload.get("cancel_requested"):
                finish(db, row, False, "cancelled")
            elif stage == 2 or (stage == 0 and offer.get("version") == 2):
                finish(db, row, True)
            else:
                row.stage, row.status, row.ready_at = stage + 1, "queued", datetime.now(UTC)
    except (*PROVIDER_ERRORS, KeyError, TypeError, IndexError) as error:
        with Session(engine) as db, db.begin():
            ai_tokens.account(db, owner, bot, test)
            row = owned(db, config, owner, bot, token, locked=True)
            row.steps = steps
            row.provider = provider
            if isinstance(error, TRANSPORT_ERRORS) and not rejected_request(error):
                row.status = "uncertain"
                for number in (row.operation_id, row.search_operation_id):
                    op = db.get(AIOperation, number) if number else None
                    if op and op.status not in {"completed", "failed"}:
                        op.status = "uncertain"
            else:
                finish(db, row, False)
            if stage >= 0 and isinstance(error, TRANSPORT_ERRORS) and row.operation_id:
                ai_failures.alert(db, config, bot, row.operation_id, error)
            row.payload = {
                **row.payload,
                "error": "provider" if isinstance(error, AgentError) else "invalid_result",
            }
