"""Expert-only knowledge and recommendations. Never used by material recipients."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.monetization import (
    article_public,
    claim_recommendation,
    preference,
    redirect_code,
    shown,
)
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import Article, ArticleCategory
from leedbox.telegram.projects import owner
from leedbox.telegram.services import edit


class RecommendationSessionMiddleware(BaseMiddleware):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and (event.successful_payment or event.refunded_payment):
            return await handler(event, data)
        state = data.get("state")
        user = data.get("event_from_user")
        bot = data.get("bot")
        if state and user and bot and (await state.get_data()).get("mode") == "expert":

            def touch() -> None:
                with Session(self.engine) as db, db.begin(), suppress(ObjectUnavailable):
                    preference(db, owner(db, user.id), bot.id)

            await asyncio.to_thread(touch)
        return await handler(event, data)


async def recommend(
    engine: Engine,
    config: Settings,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    tool: str,
    attached_ids: list[int] | None = None,
) -> None:
    if not config.public_base_url or (await state.get_data()).get("mode") != "expert":
        return

    def claim() -> tuple[int, dict[str, Any] | None]:
        with Session(engine) as db, db.begin():
            uid = owner(db, chat_id)
            return uid, claim_recommendation(db, uid, bot.id, tool, attached_ids)

    uid, item = await asyncio.to_thread(claim)
    if item is None:
        return
    # Claim commits before send: ambiguous network failures cannot create duplicate promotions.
    try:
        await bot.send_message(
            chat_id,
            item["body"],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Открыть рекомендацию",
                            url=config.public_base_url + "/go/" + item["code"],
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Не показывать рекомендации", callback_data="rec:off"
                        )
                    ],
                ]
            ),
        )
    except (TelegramAPIError, TimeoutError, OSError):
        logging.getLogger(__name__).warning("recommendation_send_unconfirmed")
        return

    def confirm() -> None:
        with Session(engine) as db, db.begin():
            shown(db, uid, bot.id, item)

    await asyncio.to_thread(confirm)


def build_knowledge_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    @router.callback_query(F.data.in_({"rec:off", "rec:on"}))
    async def preference_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert":
            await callback.answer("Откройте меню эксперта", show_alert=True)
            return

        def save() -> None:
            with Session(engine) as db, db.begin():
                preference(db, owner(db, callback.from_user.id), bot.id).opted_out = (
                    callback.data == "rec:off"
                )

        await asyncio.to_thread(save)
        await callback.answer(
            "Рекомендации отключены" if callback.data == "rec:off" else "Рекомендации включены"
        )

    @router.callback_query(F.data.startswith("kb:"))
    async def knowledge(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Откройте меню эксперта", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        try:

            def read() -> tuple[str, list[list[tuple[str, str]]], str | None, list[int]]:
                with Session(engine) as db, db.begin():
                    if parts == ["kb", "categories"]:
                        rows = [
                            [(row.title, f"kb:list:{row.id}:0")]
                            for row in db.scalars(
                                select(ArticleCategory)
                                .where(ArticleCategory.enabled.is_(True))
                                .order_by(ArticleCategory.id)
                                .limit(50)
                            )
                        ]
                        return (
                            "База знаний" if rows else "Статьи пока не опубликованы",
                            rows,
                            None,
                            [],
                        )
                    if len(parts) == 4 and parts[1] == "list":
                        id, page = int(parts[2]), int(parts[3])
                        if not 0 < id <= 2**63 - 1 or not 0 <= page <= 1000:
                            raise ObjectUnavailable
                        category = db.get(ArticleCategory, id)
                        if category is None or not category.enabled:
                            raise ObjectUnavailable
                        articles = list(
                            db.scalars(
                                select(Article)
                                .where(
                                    Article.enabled.is_(True),
                                    Article.payload["category"].astext == category.slug,
                                )
                                .order_by(Article.id)
                                .offset(page * 5)
                                .limit(6)
                            )
                        )
                        rows = [[(row.title, f"kb:open:{row.id}")] for row in articles[:5]]
                        if page:
                            rows.append([("Назад", f"kb:list:{id}:{page - 1}")])
                        if len(articles) > 5:
                            rows.append([("Далее", f"kb:list:{id}:{page + 1}")])
                        rows.append([("Категории", "kb:categories")])
                        return (
                            category.title if articles else "В категории пока нет статей",
                            rows,
                            None,
                            [],
                        )
                    if len(parts) == 3 and parts[1] == "open":
                        id = int(parts[2])
                        if not 0 < id <= 2**63 - 1:
                            raise ObjectUnavailable
                        article = db.get(Article, id)
                        if not article_public(db, article):
                            raise ObjectUnavailable
                        assert article is not None
                        code = redirect_code(db, bot.id, "article", id)
                        return (
                            article.title + "\n\n" + article.payload["description"],
                            [[("Категории", "kb:categories")]],
                            code,
                            article.payload["partner_ids"],
                        )
                    raise ObjectUnavailable

            body, rows, code, partner_ids = await asyncio.to_thread(read)
            rows.append([("В меню", "menu")])
            await state.update_data(paused=True)
            await edit(callback.message, body, rows)
            if code:
                if config.public_base_url:
                    from leedbox.telegram.material_wizard import buttons

                    markup = buttons(rows)
                    markup.inline_keyboard.insert(
                        0,
                        [
                            InlineKeyboardButton(
                                text="Читать статью", url=config.public_base_url + "/go/" + code
                            )
                        ],
                    )
                    await callback.message.edit_reply_markup(reply_markup=markup)
                    await recommend(
                        engine, config, bot, callback.from_user.id, state, "article", partner_ids
                    )
                else:
                    await callback.answer(
                        "Сервисные ссылки появятся после настройки адреса сайта", show_alert=True
                    )
                    return
            await callback.answer()
        except (ValueError, ObjectUnavailable):
            await callback.answer("Статья недоступна. Откройте категории заново", show_alert=True)

    return router
