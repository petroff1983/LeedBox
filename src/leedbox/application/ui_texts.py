"""Stable text keys shared by bot and owner editor; read fresh for each screen."""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.versions import fingerprint, lock_content, record_version
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AuditLog, Setting

TEXTS = {
    "expert_start": "Привет! 👋 Это LeedBox — инструменты для экспертов.\n\n"
    "🎁 Хотите раздавать подарки за подписку? Откройте «Лид-магниты»:\n"
    "1. Загрузите файл или добавьте ссылку.\n"
    "2. Укажите свой Telegram-канал.\n"
    "3. Получите готовую ссылку для поста.\n\nВыберите раздел ниже:",
    "resume": "Есть незавершённый мастер. Продолжите или откройте меню.",
    "material_0": "📦 Что получит подписчик?\n\nВыберите формат подарка ниже.",
    "material_1": "📤 Отправьте подарок сюда в чат.",
    "material_2": "✏️ Как называется подарок?\n\n"
    "Например: «Чек-лист для первого запуска». До 60 символов.",
    "material_3": "📝 Добавьте короткое описание: чем полезен подарок?\n\n"
    "До 300 символов. Можно нажать «Пропустить».",
    "material_4": "📢 Выберите свой канал ниже или подключите новый.\n\n"
    "Бот должен быть администратором канала. "
    "Если подписка не нужна, выберите «Без канала».",
    "material_5": "🎁 Кому выдавать подарок?\n\n"
    "«Только подписчикам» — после проверки подписки.\n"
    "«Всем» — сразу после открытия ссылки.",
    "material_6": "👀 Всё верно?\n\nПосле публикации вы получите ссылку для поста в канале.",
    "unavailable": "Материал сейчас недоступен",
    "services_menu_label": "🛍 Товары и услуги",
    "consultations_menu_label": "🎓 Консультации и обучение",
    "services_intro": "🛍 Товары и услуги\n\n"
    "🤖 Услуги — разработка ботов, ИИ-агентов, промптов и сервисов.\n"
    "🛒 Товары — инструменты и подписки, которыми пользуюсь сам.\n\nВыберите раздел 👇",
    "service_catalog": "🤖 Услуги\n\nПомогу воплотить вашу задачу с Telegram и ИИ. "
    "Нажмите «Связаться», чтобы обсудить вашу задачу.",
    "products_intro": "🛒 Проверенные товары\n\nТолько то, чем пользуюсь сам.\n"
    "Выберите карточку ниже 👇\n\nЦены и возможные скидки проверяйте у продавца.",
    "consultations_greeting": "Привет, эксперт! 👋\n\n"
    "Я создатель LeedBox. Надеюсь, бот поможет тебе привлекать аудиторию, "
    "создавать контент и превращать знания в доход. "
    "А если нужна помощь с ИИ — давай познакомимся!",
    "consultations_intro": "🎓 Консультации и обучение\n\n"
    "Помогу разобраться в ИИ — от первого промпта до своего продукта.\n\n"
    "По каким темам:\n\n"
    "1️⃣ Вайбкодинг — Codex, Claude Code, Replit, Google AI Studio\n"
    "2️⃣ Контент с ИИ — тексты, посты, статьи\n"
    "3️⃣ Видео с ИИ — генерация, монтаж, озвучка\n"
    "4️⃣ Промпт-инжиниринг — точные запросы под результат\n"
    "5️⃣ Ассистенты — свои GPT-боты под задачи\n"
    "6️⃣ Раскрутка ТГ-канала с ИИ — контент + автоворонка\n\n"
    "💬 Формат: сначала созвон-знакомство — подберу, что вам нужно: "
    "разовая консультация или обучение.\n\n"
    "💰 Консультация — 1 000 ₽\n"
    "🎯 Обучение — индивидуально, цена по задаче.\n\nПишите 👇",
    "service_contact": "Введите контакт: @username, email или номер телефона (до 200 символов).",
    "service_comment": "Добавьте комментарий к заявке (до 1000 символов) или пропустите.",
}


def read_text(session: Session, key: str) -> str:
    row = session.get(Setting, f"ui:{key}")
    return str(row.value) if row and isinstance(row.value, str) else TEXTS[key]


def set_text(
    session: Session, config: Settings, actor: int, key: str, value: str, expected: str
) -> None:
    if not config.is_owner(actor):
        raise PermissionError("Owner access required")
    if key not in TEXTS or not value.strip() or len(value) > 4000:
        raise ValueError("Выберите известный текст длиной от 1 до 4000 символов")
    if key.endswith("_menu_label") and (len(value) > 64 or "\n" in value):
        raise ValueError("Название кнопки: одна строка, до 64 символов")
    if key in {"products_intro", "service_catalog"} and len(value) > 600:
        raise ValueError("Вступление каталога: до 600 символов")
    if key == "consultations_greeting" and len(value) > 400:
        raise ValueError("Приветствие: до 400 символов")
    lock_content(session, "ui", key)
    before = read_text(session, key)
    if fingerprint(before) != expected:
        raise ValueError("Текст изменился. Откройте его заново")
    if before == value:
        return
    session.execute(
        insert(Setting)
        .values(key=f"ui:{key}", value=value)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    )
    record_version(session, "ui", key, actor, {"value": value}, {"value": before})
    session.add(AuditLog(actor_telegram_id=actor, action="ui_text_changed", details={"key": key}))
