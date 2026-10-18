"""Initial five materials -> approve OR two automatic critic/revision steps."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application import agents, ai_failures, ai_tokens, credits
from leedbox.application import content_agent as tasks
from leedbox.application.settings import setting
from leedbox.application.versions import fingerprint
from leedbox.domain import lead_magnet_agent as domain
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.content_agent import AUTO_BRIEF, byte_size
from leedbox.infrastructure.agents import (
    PROVIDER_ERRORS,
    TRANSPORT_ERRORS,
    deepseek_generate,
    rejected_request,
)
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AIOperation, ContentTask, ToolResult


def create(
    db: Session, config: Settings, owner: int, bot: int, chat: int, *, fresh: bool = False
) -> ContentTask:
    agents.allowed(db, owner, bot, agents.test_mode(config), "lead_magnet", config)
    ai_tokens.account(db, owner, bot, agents.test_mode(config))
    previous = next(
        (
            r
            for r in tasks.latest(db, config, owner, bot, "lead_magnet")
            if r.status not in tasks.TERMINAL
            and not r.payload.get("superseded")
            and (not fresh or (r.status == "draft" and not r.payload.get("brief") and not r.steps))
        ),
        None,
    )
    if previous:
        return previous
    row = ContentTask(
        token=uuid4().hex[:16],
        owner_user_id=owner,
        bot_id=bot,
        test_mode=agents.test_mode(config),
        chat_id=chat,
        status="draft",
        stage=0,
        revision=1,
        payload={"kind": "lead_magnet", "phase": "niche", "brief": {}},
        steps=[],
        provider={},
        delivered=False,
    )
    db.add(row)
    db.flush()
    return row


def new_selection(db: Session, config: Settings, row: ContentTask) -> ContentTask:
    """Explicit, idempotent restart without deleting or charging the previous selection."""
    if row.payload.get("kind") != "lead_magnet":
        raise ObjectUnavailable
    if row.status not in tasks.TERMINAL and row.status != "draft":
        raise ValueError("Дождитесь текущей генерации или остановите её перед новой подборкой.")
    successor = row.payload.get("new_task_token")
    if successor:
        return tasks.owned(db, config, row.owner_user_id, row.bot_id, successor)
    new = create(db, config, row.owner_user_id, row.bot_id, row.chat_id, fresh=True)
    if new.token != row.token:
        row.payload = {**row.payload, "new_task_token": new.token, "superseded": True}
    return new


def audience_options(db: Session, row: ContentTask) -> list[dict[str, str]]:
    options = []
    profiles = db.scalars(
        select(AIOperation)
        .where(
            AIOperation.owner_user_id == row.owner_user_id,
            AIOperation.bot_id == row.bot_id,
            AIOperation.test_mode == row.test_mode,
            AIOperation.status == "completed",
            AIOperation.snapshot["purpose"].astext == "writing_profile",
        )
        .order_by(AIOperation.id.desc())
        .limit(3)
    )
    for profile in profiles:
        brief = profile.snapshot.get("inputs", {}).get("brief", {})
        if not isinstance(brief, dict):
            continue
        topic, audience = brief.get("topic"), brief.get("audience")
        if isinstance(topic, str) and isinstance(audience, str) and topic and audience:
            options.append(
                {
                    "code": f"profile-{profile.id}",
                    "label": "Дзен · " + topic[:45],
                    "niche": topic[:500],
                    "audience": audience[:1000],
                }
            )
    for previous in tasks.latest(
        db,
        Settings.model_construct(environment="test" if row.test_mode else "production"),
        row.owner_user_id,
        row.bot_id,
    ):
        if (
            previous.token != row.token
            and isinstance(previous.payload.get("brief"), str)
            and previous.payload["brief"] != AUTO_BRIEF
        ):
            text = previous.payload["brief"][:1000]
            options.append(
                {
                    "code": "task-" + previous.token,
                    "label": text[:55],
                    "audience": text,
                    "niche": text[:500],
                }
            )
    results = db.scalars(
        select(ToolResult)
        .where(
            ToolResult.owner_user_id == row.owner_user_id,
            ToolResult.bot_id == row.bot_id,
            ToolResult.tool.in_({"ca", "content_strategy"}),
        )
        .order_by(ToolResult.id.desc())
        .limit(5)
    )
    for result in results:
        values = result.inputs.get("values", {})
        # A saved CA tool returns a prompt, not an AI analysis. Reuse only user-supplied facts.
        subject, focus = values.get("ca_subject", ""), values.get("ca_focus", "")
        audience = values.get("ca_report") or focus or subject
        if isinstance(audience, str) and audience:
            options.append(
                {
                    "code": f"result-{result.id}",
                    "label": str(subject)[:55] or "Сохранённая ЦА",
                    "audience": audience[:1000],
                    "niche": str(subject)[:500],
                }
            )
    return options[:7]


@credits.quoted
def quote(db: Session, row: ContentTask, revision: bool = False) -> dict[str, Any]:
    brief = row.payload.get("brief", {})
    if any(
        not isinstance(brief.get(k), str) or not brief[k].strip()
        for k in ("niche", "audience", "pain")
    ):
        raise ValueError("Сначала ответьте на вопросы о нише, аудитории и задаче")
    stages = (1, 2) if revision else (0,)
    systems = [
        domain.instruction(
            i, str(setting(db, "ai_lead_magnet_" + ("generate", "critic", "revise")[i] + "_prompt"))
        )
        for i in stages
    ]
    inputs = {"brief": brief}
    initial = (
        domain.idea_input(
            next(
                step["result"]
                for step in reversed(row.steps)
                if step.get("result", {}).get("lead_magnets")
            )
        )
        if revision
        else None
    )
    if initial is not None:
        inputs["initial"] = initial
        inputs["feedback"] = row.payload.get("feedback", "")
    bound = (
        sum(len(s.encode()) for s in systems)
        + (byte_size(inputs) + 512) * len(stages)
        + (11000 if revision else 0)  # Max UTF-8 size of the compact critic JSON.
        + sum(domain.LIMITS[i] for i in stages)
    )
    if not 1 <= bound <= 100000:
        raise ValueError("Вводные слишком велики для безопасного резерва")
    return {
        "purpose": "lead_magnet_revision" if revision else "lead_magnet",
        "output_kind": "ideas",
        "limits": list(domain.LIMITS),
        "initial": initial,
        "feedback": inputs.get("feedback", ""),
        "model": "deepseek-flash",
        "maximum": bound,
        "systems": systems,
        "revision": row.revision,
        "brief": brief,
        "stages": list(stages),
        "rates": {
            "input": setting(db, "ai_input_usd_per_million"),
            "output": setting(db, "ai_output_usd_per_million"),
            "usd_rub": setting(db, "ai_usd_rub"),
        },
    }


def confirmation(
    db: Session, config: Settings, row: ContentTask, revision: bool
) -> tuple[dict[str, Any], str, str, bool]:
    offer = quote(db, row, revision)
    mode = ai_tokens.funding(db, config, row.owner_user_id, row.bot_id, row.test_mode, offer)
    digest = fingerprint({**offer, "billing": mode})[:12]
    balance = ai_tokens.available(db, row.owner_user_id, row.bot_id, row.test_mode)
    text = (
        "♾ Безлимитный доступ, пользовательский баланс не списывается."
        if mode == "unlimited"
        else "🎁 Бесплатная попытка: 5 тем лид-магнитов. Баланс не списывается."
        if mode == "trial"
        else f"Остаток: {balance:,}. Резерв: до {offer['maximum']:,} токенов. "
        "Спишем фактический расход API."
    )
    if credits.is_credit(offer) and mode == "balance":
        text = (
            f"Стоимость: {credits.amount(offer)} кредитов. Остаток: {balance} кредитов. "
            "Цена фиксирована."
        )
    return offer, digest, text, mode in {"unlimited", "trial"} or balance >= credits.amount(offer)


def launch(
    db: Session, config: Settings, row: ContentTask, digest: str, revision: bool = False
) -> None:
    if row.status != "draft" or row.payload.get("phase") != (
        "lm_review" if revision else "confirm"
    ):
        return
    ai_tokens.account(db, row.owner_user_id, row.bot_id, row.test_mode)
    agents.allowed(db, row.owner_user_id, row.bot_id, row.test_mode, "lead_magnet", config)
    if not config.deepseek_api_key.get_secret_value():
        raise ValueError("DeepSeek пока не настроен")
    offer, current, _, _ = confirmation(db, config, row, revision)
    if digest != current:
        raise ValueError("Условия изменились. Обновите экран перед запуском.")
    mode = ai_tokens.funding(db, config, row.owner_user_id, row.bot_id, row.test_mode, offer)
    round_number = int(row.payload.get("revision_round", 0)) + 1 if revision else 0
    action = ("lm-revise:" if revision else "lm:") + row.token
    if round_number > 1:
        action += f":{round_number}"
    op = ai_tokens.reserve(
        db,
        row.owner_user_id,
        row.bot_id,
        row.test_mode,
        action,
        offer["maximum"],
        offer,
        mode,
    )
    row.operation_id = op.id
    row.payload = {
        **row.payload,
        "offer": offer,
        "phase": "working",
        "run_start": len(row.steps),
        "revision_round": round_number,
    }
    row.status, row.stage, row.ready_at, row.delivered = (
        "queued",
        1 if revision else 0,
        datetime.now(UTC),
        False,
    )


def feedback(row: ContentTask, text: str, message_id: int) -> None:
    if row.payload.get("kind") != "lead_magnet" or not (
        row.status == "completed"
        or (row.status == "draft" and row.payload.get("phase") == "lm_review")
    ):
        raise ValueError(
            "Сначала дождитесь пяти вариантов. Во время генерации правки не принимаются."
        )
    if not 3 <= len(text.strip()) <= 1000:
        raise ValueError("Пожелания: 3–1000 символов")
    if row.payload.get("feedback_message_id") == message_id:
        return
    row.status = "draft"
    tasks.change(
        row,
        phase="lm_review",
        feedback=text.strip(),
        feedback_message_id=message_id,
        approved=False,
    )


def finish(db: Session, row: ContentTask, valid: bool, status: str = "failed") -> None:
    if row.operation_id:
        op = db.get(AIOperation, row.operation_id, with_for_update=True)
        if op and op.status not in {"completed", "failed"}:
            steps = row.steps[row.payload.get("run_start", 0) :]
            incoming = sum(s["usage"]["prompt_tokens"] for s in steps)
            outgoing = sum(s["usage"]["completion_tokens"] for s in steps)
            if incoming + outgoing:
                result = (
                    {
                        **row.steps[-1].get("result", {}),
                        "brief": row.payload["brief"],
                        "version": "final" if row.stage == 2 else "initial",
                    }
                    if valid
                    else None
                )
                if result is not None and row.stage == 2:
                    result["critique"] = row.steps[row.payload.get("run_start", 1)]["result"]
                ai_tokens.settle(
                    db,
                    row.owner_user_id,
                    row.bot_id,
                    row.test_mode,
                    op.id,
                    {
                        "prompt_tokens": incoming,
                        "completion_tokens": outgoing,
                        "total_tokens": incoming + outgoing,
                    },
                    result,
                    valid,
                )
            else:
                funds = ai_tokens.account(db, row.owner_user_id, row.bot_id, row.test_mode)
                ai_tokens.release_reservation(funds, op)
                op.status = "failed"
    if valid and row.stage == 0:
        row.status = "draft"
        row.payload = {
            **row.payload,
            "phase": "lm_review",
            "initial_operation_id": row.operation_id,
        }
        row.revision += 1
    else:
        row.status = "completed" if valid else status
        if valid:
            row.payload = {**row.payload, "feedback": ""}


def label(row: ContentTask) -> str:
    if row.status == "completed":
        return "Пять вариантов сохранены"
    if row.status == "failed":
        return "Генерация не завершена. Списание за неудачный этап не выполнялось."
    if row.status == "draft":
        return (
            "Пять вариантов готовы — одобрить или отправить на критику?"
            if row.payload.get("phase") == "lm_review"
            else "Опрос для лид-магнитов"
        )
    if row.status in {"queued", "working"}:
        return {
            0: "Подбираю 5 тем для лид-магнитов…",
            1: "Критик оценивает темы глазами аудитории…",
            2: "Улучшаю 5 тем по замечаниям…",
        }.get(row.stage, "Обрабатываю…")
    return tasks.STATUSES.get(row.status, "Готово")


def advance(engine: Engine, config: Settings, token: str) -> None:
    with Session(engine) as db, db.begin():
        row = db.get(ContentTask, token)
        if row is None or row.status != "working":
            return
        owner, bot, test, stage = row.owner_user_id, row.bot_id, row.test_mode, row.stage
        ai_tokens.account(db, owner, bot, test)
        try:
            agents.allowed(db, owner, bot, test, "lead_magnet", config)
        except (ValueError, ObjectUnavailable):
            finish(db, row, False)
            return
        if row.payload.get("cancel_requested"):
            finish(db, row, False, "cancelled")
            return
        offer, brief, steps = row.payload["offer"], row.payload["brief"], list(row.steps)
        op = db.get(AIOperation, row.operation_id)
        if op is None or op.status in {"completed", "failed"}:
            row.status = "failed"
            return
        op.status = "sent"
        run_start = row.payload.get("run_start", 0)
    inputs: dict[str, Any] = {"brief": brief}
    ideas = offer.get("output_kind") == "ideas"
    if stage >= 1:
        inputs["initial"] = offer["initial"] if ideas else steps[0]["result"]
        inputs["feedback"] = offer.get("feedback", "")
    if stage == 2:
        inputs["critique"] = steps[run_start]["result"]
    system = offer["systems"][offer["stages"].index(stage)]
    previous = sum(s["usage"]["total_tokens"] for s in steps[run_start:])
    try:
        limit = offer["limits"][stage] if ideas else 2200 if stage == 1 else 5500
        remaining = offer["maximum"] - previous
        if byte_size(inputs) + len(system.encode()) + 512 + limit > remaining:
            raise ValueError("Недостаточно согласованного резерва")
        response = deepseek_generate(
            config.deepseek_api_key.get_secret_value(), offer["model"], system, inputs, limit
        )
        usage = response.get("usage")
        ai_tokens.usage_counts(usage, remaining)
        record = {"stage": stage, "usage": usage}
        steps.append(record)
        choice = response["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ValueError("ИИ не завершил ответ")
        value = json.loads(choice["message"]["content"])
        record["result"] = (
            domain.validate_critique(value, ideas=ideas)
            if stage == 1
            else domain.validate(value, ideas=ideas)
        )
        with Session(engine) as db, db.begin():
            ai_tokens.account(db, owner, bot, test)
            row = tasks.owned(db, config, owner, bot, token, locked=True)
            row.steps = steps
            if row.payload.get("cancel_requested"):
                finish(db, row, False, "cancelled")
            elif stage in {0, 2}:
                finish(db, row, True)
            else:
                row.status, row.stage, row.ready_at = "queued", 2, datetime.now(UTC)
    except (*PROVIDER_ERRORS, KeyError, TypeError, IndexError) as error:
        with Session(engine) as db, db.begin():
            ai_tokens.account(db, owner, bot, test)
            row = tasks.owned(db, config, owner, bot, token, locked=True)
            row.steps = steps
            if isinstance(error, TRANSPORT_ERRORS) and not rejected_request(error):
                row.status = "uncertain"
                op = db.get(AIOperation, row.operation_id)
                if op:
                    op.status = "uncertain"
            else:
                finish(db, row, False)
            if isinstance(error, TRANSPORT_ERRORS) and row.operation_id:
                ai_failures.alert(db, config, bot, row.operation_id, error)
