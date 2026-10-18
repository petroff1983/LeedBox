import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application.account_overview import overview as account_overview
from leedbox.application.cabinet_access import issue_ticket
from leedbox.application.entitlements import start_expert
from leedbox.application.links import record_start as record_link_start
from leedbox.application.profile import delete_my_data, update_profile
from leedbox.application.projects import ensure_project, get_project, register_user
from leedbox.application.sections import visible_sections
from leedbox.application.settings import setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.banner_assets import banner_source
from leedbox.infrastructure.config import Settings
from leedbox.telegram.access import AccessMiddleware
from leedbox.telegram.assistance import build_fallback_router, install_assistance
from leedbox.telegram.audience import build_audience_router
from leedbox.telegram.calculators import INTRO as CALC_INTRO
from leedbox.telegram.calculators import build_calculator_router, menu_rows
from leedbox.telegram.content_tools import build_content_router, prompt_menu_rows
from leedbox.telegram.history import build_history_router
from leedbox.telegram.invites import build_membership_router
from leedbox.telegram.library import build_library_router
from leedbox.telegram.links import build_link_router
from leedbox.telegram.links import menu_rows as link_menu_rows
from leedbox.telegram.material_ui import CREATE, HOME, LIBRARY, next_message, retire, shortcuts
from leedbox.telegram.material_wizard import build_material_router, buttons
from leedbox.telegram.monetization import RecommendationSessionMiddleware, build_knowledge_router
from leedbox.telegram.navigation import can_return_to_expert, pause_input
from leedbox.telegram.objections import build_objection_router
from leedbox.telegram.owner import build_owner_router, owner_panel
from leedbox.telegram.payments import install_payments
from leedbox.telegram.projects import build_project_router
from leedbox.telegram.recipient import build_recipient_router, recipient_start
from leedbox.telegram.safety import LimitMiddleware, safe_error
from leedbox.telegram.sections import SectionVisibilityMiddleware
from leedbox.telegram.services import build_service_router
from leedbox.telegram.services import menu_rows as service_menu_rows
from leedbox.telegram.storefront import build_storefront_router
from leedbox.telegram.texts import ui_text
from leedbox.telegram.youtube import build_youtube_router


def menu(
    settings: Settings,
    telegram_id: int,
    service_label: str = "🛍 Товары и услуги",
    consultation_label: str = "🎓 Консультации и обучение",
    visible: set[str] | None = None,
) -> InlineKeyboardMarkup:
    labels = [
        ("🎁 Лид-магниты", "materials"),
        ("🧮 Расчёты", "calc"),
        ("🔗 Ссылки", "links"),
        ("✍️ Промпты", "texts"),
        (service_label, "services"),
        ("👤 Профиль", "profile"),
        (consultation_label, "consultations"),
        ("👥 Моя аудитория", "audience"),
        ("🤖 Агенты", "agents"),
    ]
    if visible is not None and "wallet" in visible:
        labels.append(("💳 Баланс", "wallet"))
    if visible is not None:
        labels = [(label, code) for label, code in labels if code in visible]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"section:{code}")
                for label, code in labels[index : index + 2]
            ]
            for index in range(0, len(labels), 2)
        ]
    )


def navigation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="В меню", callback_data="menu")]]
    )


def cabinet_button(url: str) -> InlineKeyboardButton:
    # Telegram rejects localhost as an inline URL. Local testing uses native copy instead.
    if urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}:
        return InlineKeyboardButton(
            text="📋 Скопировать личную ссылку", copy_text=CopyTextButton(text=url)
        )
    return InlineKeyboardButton(text="👥 Открыть кабинет", url=url)


def build_router(engine: Engine, settings: Settings) -> Router:
    router = Router()
    from leedbox.telegram.legal import LegalMiddleware, install_legal

    router.message.middleware(LimitMiddleware(engine))
    router.callback_query.middleware(LimitMiddleware(engine))
    router.message.middleware(LegalMiddleware(engine, settings))
    router.callback_query.middleware(LegalMiddleware(engine, settings))
    router.errors.register(safe_error)
    router.message.middleware(SectionVisibilityMiddleware(engine))
    router.callback_query.middleware(SectionVisibilityMiddleware(engine))
    router.message.middleware(AccessMiddleware(engine, settings))
    router.callback_query.middleware(AccessMiddleware(engine, settings))
    router.message.middleware(RecommendationSessionMiddleware(engine))
    router.callback_query.middleware(RecommendationSessionMiddleware(engine))
    install_payments(router, engine, settings)
    install_assistance(router, engine)

    async def current_menu(actor: int) -> InlineKeyboardMarkup:
        service_label = await asyncio.to_thread(ui_text, engine, "services_menu_label")
        consultation_label = await asyncio.to_thread(ui_text, engine, "consultations_menu_label")

        def read_visibility() -> set[str]:
            with Session(engine) as db:
                return visible_sections(db)

        visible = await asyncio.to_thread(read_visibility)
        return menu(settings, actor, service_label, consultation_label, visible)

    async def present_menu(message: Message, actor: int, text: str, state: FSMContext) -> None:
        await pause_input(state)

        def source() -> Path | str | None:
            with Session(engine) as db:
                return banner_source(
                    str(setting(db, "main_menu_banner")), settings.owner_media_directory
                )

        banner = await asyncio.to_thread(source)
        markup = await current_menu(actor)
        shown = {button.callback_data for row in markup.inline_keyboard for button in row}
        if not shown:
            text = (
                "Все разделы временно скрыты. Включите нужные в админке: "
                "«Управление → Разделы бота»."
                if settings.is_owner(actor)
                else "Разделы временно недоступны. Попробуйте открыть /menu позже."
            )
        if "section:materials" not in shown and (await state.get_data()).get("material_shortcuts"):
            await message.answer("Главное меню", reply_markup=ReplyKeyboardRemove())
            await state.update_data(material_shortcuts=False)
        if banner:
            try:
                await message.answer_photo(
                    FSInputFile(banner) if isinstance(banner, Path) else banner,
                    caption=text if len(text) <= 1024 else None,
                    reply_markup=markup if len(text) <= 1024 else None,
                )
                if len(text) <= 1024:
                    return
            except (TelegramBadRequest, OSError):
                # A deleted file or rejected external photo must never hide navigation.
                pass
        await message.answer(text, reply_markup=markup)

    def project_for(telegram_id: int, username: str | None) -> tuple[int, str]:
        with Session(engine) as session, session.begin():
            user = register_user(session, telegram_id, username)
            start_expert(session, user)
            from leedbox.application.broadcasts import configured_bot, enroll

            enroll(session, user, configured_bot(settings))
            project = ensure_project(session, user.id)
            contact = "@" + project.public_contact if project.public_contact else "не задан"
            return project.id, contact

    async def legal_resume(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(mode="expert", paused=True, active_tool=None)
        await asyncio.to_thread(project_for, callback.from_user.id, callback.from_user.username)
        if isinstance(callback.message, Message):
            await present_menu(
                callback.message,
                callback.from_user.id,
                await asyncio.to_thread(ui_text, engine, "expert_start"),
                state,
            )

    install_legal(router, engine, settings, legal_resume)

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject, state: FSMContext) -> None:
        if not message.from_user or message.chat.type != "private":
            return
        if command.args and command.args.startswith("lm_"):
            await state.update_data(
                expert_return_allowed=can_return_to_expert(await state.get_data())
            )
        if (
            command.args
            and message.bot
            and re.fullmatch(r"(?:ex_|lm_[A-Za-z0-9-]+_)[a-f0-9]{12}", command.args)
        ):
            bot_id, telegram_id = message.bot.id, message.from_user.id

            def track() -> bool | None:
                with Session(engine) as session, session.begin():
                    return record_link_start(
                        session,
                        bot_id,
                        command.args or "",
                        telegram_id,
                        f"start:{bot_id}:{message.chat.id}:{message.message_id}",
                    )

            tracked = await asyncio.to_thread(track)
            if tracked is False or (command.args.startswith("ex_") and tracked is None):
                await state.update_data(mode="recipient", paused=True)
                await message.answer("Ссылка недоступна")
                return
        if command.args and command.args.startswith("lm_"):
            if (await state.get_data()).get("material_shortcuts"):
                await message.answer("🎁 Получение подарка", reply_markup=ReplyKeyboardRemove())
                await state.update_data(material_shortcuts=False)
            await state.update_data(mode="recipient")
            if message.bot:
                await recipient_start(message.bot, engine, message, command.args)
            return
        await state.update_data(
            mode="expert",
            paused=True,
            library_pending=None,
            profile_pending=None,
            history_pending=None,
            idea_material=None,
            delete_token=None,
            link_disable=None,
            link_restart=None,
            service_withdraw=None,
        )
        await asyncio.to_thread(project_for, message.from_user.id, message.from_user.username)
        await present_menu(
            message,
            message.from_user.id,
            await asyncio.to_thread(ui_text, engine, "expert_start"),
            state,
        )

    @router.message(F.text == HOME)
    @router.message(Command("menu"))
    async def show_menu(message: Message, state: FSMContext) -> None:
        if message.from_user and message.chat.type == "private":
            await asyncio.to_thread(project_for, message.from_user.id, message.from_user.username)
            await state.update_data(
                mode="expert",
                paused=True,
                library_pending=None,
                service_withdraw=None,
                profile_pending=None,
                history_pending=None,
                idea_material=None,
                delete_token=None,
            )
            await present_menu(
                message,
                message.from_user.id,
                "Выберите раздел.",
                state,
            )

    @router.callback_query(F.data == "menu")
    async def menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if not can_return_to_expert(await state.get_data()):
            await callback.answer(
                "Экран устарел. Для инструментов откройте /start", show_alert=True
            )
            return
        await state.update_data(
            mode="expert",
            paused=True,
            library_pending=None,
            profile_pending=None,
            history_pending=None,
            idea_material=None,
            delete_token=None,
            link_disable=None,
            link_restart=None,
            service_withdraw=None,
        )
        if isinstance(callback.message, Message):
            if callback.message.bot:
                await retire(
                    callback.message.bot, callback.message.chat.id, callback.message.message_id
                )
            await present_menu(
                callback.message,
                callback.from_user.id,
                "Выберите раздел.",
                state,
            )
        await callback.answer()

    @router.callback_query(
        F.data.startswith("section:")
        & ~F.data.in_(
            {"section:consultations", "section:youtube", "section:wallet", "section:agents"}
        )
    )
    async def section(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert":
            await callback.answer("Экран устарел. Откройте /start", show_alert=True)
            return
        section_name = (callback.data or "").split(":")[1]
        if section_name == "audience" and isinstance(callback.message, Message):
            await state.set_state(None)
            await state.update_data(paused=True, active_tool=None)

            def cabinet_link() -> str:
                with Session(engine) as db, db.begin():
                    return issue_ticket(db, settings, callback.from_user.id)

            try:
                url = await asyncio.to_thread(cabinet_link)
            except (ValueError, ObjectUnavailable):
                await callback.answer(
                    "Кабинет пока недоступен. Откройте /start или обратитесь к владельцу.",
                    show_alert=True,
                )
                return
            await next_message(
                callback.message,
                "👥 Моя аудитория\n\n"
                "Все лид-магниты, источники и получатели — в одном кабинете. "
                "Сравните, какие подарки привлекают людей "
                "и из каких статей или видео приходят запуски.\n\n"
                "🔐 Это ваша личная ссылка: действует 5 минут и используется один раз. "
                "Не пересылайте её."
                + (
                    "\n\nКабинет запущен локально. Нажмите «Скопировать личную ссылку» "
                    "и вставьте её в адресную строку браузера на этом компьютере."
                    if urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}
                    else ""
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [cabinet_button(url)],
                        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu")],
                    ]
                ),
            )
            await callback.answer()
            return
        await pause_input(state)
        await state.update_data(
            paused=True,
            library_pending=None,
            profile_pending=None,
            history_pending=None,
            idea_material=None,
            delete_token=None,
            link_disable=None,
            link_restart=None,
            service_withdraw=None,
        )
        if section_name == "owner" and not settings.is_owner(callback.from_user.id):
            await callback.answer("Доступ закрыт", show_alert=True)
            return
        if section_name == "owner":
            await state.update_data(paused=True)
            await owner_panel(engine, settings, callback, bot)
            return
        if section_name == "materials" and isinstance(callback.message, Message):
            await next_message(
                callback.message,
                "🎁 Лид-магниты\n\n"
                "Раздавайте файлы, фото, видео, тексты или ссылки за подписку на свой канал.\n\n"
                "➕ «Создать лид-магнит» — загрузить подарок и получить ссылку для поста.\n"
                "📚 «Мои лид-магниты» — найти ссылку, изменить подарок или посмотреть выдачи.\n\n"
                "Выберите действие на клавиатуре внизу 👇",
                reply_markup=shortcuts(),
            )
            await state.update_data(material_shortcuts=True)
            await callback.answer()
            return
        if section_name == "calc" and isinstance(callback.message, Message):
            rows = menu_rows()
            await state.update_data(paused=True)
            sent = await next_message(callback.message, CALC_INTRO, reply_markup=buttons(rows))
            await state.update_data(calc_menu_screen=sent.message_id)
            await callback.answer()
            return
        if section_name == "texts" and isinstance(callback.message, Message):
            await next_message(
                callback.message,
                "✍️ Промпты и ответы\n\n"
                "🎯 УТП · 🧱 лендинг · 📖 кейс — ответьте на вопросы и получите промпт "
                "для своей нейросети.\n\n"
                "🔥 Воронка — готовый промпт без опроса в боте.\n"
                "💬 Возражения — готовые ответы с копированием.\n\nВыберите инструмент 👇",
                reply_markup=buttons(prompt_menu_rows()),
            )
            await callback.answer()
            return
        if section_name == "links" and isinstance(callback.message, Message):
            rows = link_menu_rows()
            await next_message(
                callback.message,
                "🔗 Ссылки\n\nВыберите инструмент, пришлите адрес и получите готовую ссылку.",
                reply_markup=buttons(rows),
            )
            await callback.answer()
            return
        if section_name == "services" and isinstance(callback.message, Message):
            rows = service_menu_rows()
            await next_message(
                callback.message,
                await asyncio.to_thread(ui_text, engine, "services_intro"),
                reply_markup=buttons(rows),
            )
            await callback.answer()
            return
        if section_name == "profile" and isinstance(callback.message, Message):
            if callback.message.chat.type != "private":
                await callback.answer("Откройте профиль в личном чате с ботом", show_alert=True)
                return
            _, contact = await asyncio.to_thread(
                project_for, callback.from_user.id, callback.from_user.username
            )

            def account_text() -> str:
                with Session(engine) as db:
                    return str(
                        account_overview(db, settings, callback.from_user.id, bot.id)["summary"]
                    )

            summary = await asyncio.to_thread(account_text)
            await next_message(
                callback.message,
                "👤 Профиль\n\n" + summary + "\n\n"
                f"💬 Контакт для получателей: {contact}\n"
                "По нему получатель лид-магнита сможет связаться с вами.\n\n"
                "Здесь — ответы на возражения, настройки контакта, доступ и заказы.\n"
                "Результаты агентов сохранены в подборках каждого агента.",
                reply_markup=buttons(
                    [
                        [("💬 Мои ответы на возражения", "ob:list:0")],
                        [("✉️ Контакт для получателей", "project:contact")],
                        [("🔔 Уведомления владельца", "notify:open")],
                        [("💳 Баланс, токены и доступ", "account:open")],
                        [("🛍 Мои заказы", "purchase:orders:0")],
                        [("❓ Помощь", "help:open")],
                        [("📄 Документы и согласия", "legal:open")],
                        [("🗑 Удалить все мои данные", "profile:delete")],
                        [("🏠 В главное меню", "menu")],
                    ]
                ),
            )
        else:
            await callback.answer("Раздел пока недоступен", show_alert=True)
            return
        await callback.answer()

    @router.callback_query(F.data == "profile:delete")
    async def delete_prompt(callback: CallbackQuery, state: FSMContext) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        from uuid import uuid4

        token = uuid4().hex[:12]
        await state.update_data(delete_token=token, paused=True)
        await callback.message.edit_text(
            "Удалить все ваши проекты, материалы, черновики и согласия? Это действие необратимо.",
            reply_markup=buttons([[("Да, удалить", f"delete:{token}")], [("Отмена", "menu")]]),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("delete:"))
    async def delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or (callback.data or "")[7:] != data.get("delete_token"):
            await callback.answer("Экран устарел", show_alert=True)
            return

        def save() -> None:
            with Session(engine) as session, session.begin():
                delete_my_data(session, callback.from_user.id)

        try:
            await asyncio.to_thread(save)
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.edit_text("Ваши данные удалены. Начать заново: /start")
        await callback.answer()

    @router.callback_query(
        F.data.in_({"project:rename", "project:resume", "project:contact", "project:timezone"})
    )
    async def rename(callback: CallbackQuery, state: FSMContext) -> None:
        if (await state.get_data()).get("mode") != "expert":
            await callback.answer("Экран устарел", show_alert=True)
            return
        previous = await state.get_data()

        def context() -> tuple[int, int]:
            with Session(engine) as session, session.begin():
                user = register_user(session, callback.from_user.id, callback.from_user.username)
                if callback.data == "project:resume" and previous.get("project_id"):
                    project = get_project(session, user.id, int(previous["project_id"]))
                    if project.archived:
                        raise ObjectUnavailable
                    return project.id, int(previous.get("project_revision", project.revision))
                project = ensure_project(session, user.id)
                return project.id, project.revision

        try:
            project_id, revision = await asyncio.to_thread(context)
        except ObjectUnavailable:
            await callback.answer("Проект недоступен. Откройте профиль заново", show_alert=True)
            return
        field = (
            previous.get("project_field", "name")
            if callback.data == "project:resume"
            else "contact"
            if callback.data == "project:contact"
            else "timezone"
            if callback.data == "project:timezone"
            else "name"
        )
        await state.update_data(
            project_id=project_id,
            project_revision=revision,
            paused=False,
            active_tool="project",
            project_field=field,
        )
        await state.set_state("project_name")
        if isinstance(callback.message, Message):
            sent = await next_message(
                callback.message,
                {
                    "name": "Введите название проекта (до 100 символов).",
                    "contact": (
                        "✉️ Контакт для получателей\n\n"
                        "Пришлите ваш Telegram @username, например @my_name.\n"
                        "Он появится в кнопке «Связаться с автором» после выдачи лид-магнита.\n\n"
                        "Контакт необязателен. Отправьте «-», чтобы убрать его."
                    ),
                    "timezone": "Введите часовой пояс IANA, например Europe/Moscow.",
                }[field],
                reply_markup=buttons([[("← Профиль", "section:profile")]]),
            )
            await state.update_data(screen_message_id=sent.message_id)
        await callback.answer()

    @router.message(
        StateFilter("project_name"), ~F.text.startswith("/"), ~F.text.in_({CREATE, LIBRARY, HOME})
    )
    async def project_name(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or data.get("paused") or not message.from_user:
            return
        name = (message.text or "").strip()
        if not name or len(name) > 100:
            await message.answer(
                "Введите Telegram @username или «-», чтобы убрать контакт."
                if data.get("project_field") == "contact"
                else "Введите значение длиной от 1 до 100 символов."
            )
            return

        telegram_user = message.from_user

        def save() -> None:
            with Session(engine) as session, session.begin():
                user = register_user(session, telegram_user.id, telegram_user.username)
                update_profile(
                    session,
                    user.id,
                    int(data["project_id"]),
                    str(data.get("project_field", "name")),
                    name,
                    data.get("project_revision"),
                )

        try:
            await asyncio.to_thread(save)
        except (ValueError, ObjectUnavailable) as error:
            await message.answer(
                str(error) if isinstance(error, ValueError) else "Проект недоступен"
            )
            return
        await state.set_state(None)
        await state.update_data(paused=True)
        if message.bot:
            await retire(message.bot, message.chat.id, int(data["screen_message_id"]))
            await next_message(
                message,
                (
                    "✅ Контакт убран. Кнопка связи с автором не будет показываться."
                    if name == "-"
                    else f"✅ Контакт сохранён: @{name.removeprefix('@')}"
                )
                if data.get("project_field") == "contact"
                else f"✅ Сохранено: {name}",
                reply_markup=buttons([[("← Профиль", "section:profile")]]),
            )

    router.include_router(build_storefront_router(engine, settings))
    from leedbox.telegram.account import build_account_router
    from leedbox.telegram.wallet import build_wallet_router

    router.include_router(build_account_router(engine, settings))
    router.include_router(build_wallet_router(engine, settings))
    router.include_router(build_library_router(engine))
    from leedbox.telegram.notifications import build_notification_router

    router.include_router(build_notification_router(engine, settings))
    router.include_router(build_audience_router(engine))
    router.include_router(build_material_router(engine))
    router.include_router(build_recipient_router(engine))
    router.include_router(build_owner_router(engine, settings))
    router.include_router(build_calculator_router(engine, settings))
    router.include_router(build_link_router(engine, settings))
    router.include_router(build_youtube_router(engine, settings))
    from leedbox.telegram.agents import build_agent_router

    router.include_router(build_agent_router(engine, settings))
    router.include_router(build_membership_router(engine))
    router.include_router(build_service_router(engine, settings))
    router.include_router(build_project_router(engine))
    router.include_router(build_history_router(engine))
    router.include_router(build_content_router(engine, settings))
    router.include_router(build_objection_router(engine))
    router.include_router(build_knowledge_router(engine, settings))
    router.include_router(build_fallback_router(engine))
    return router
