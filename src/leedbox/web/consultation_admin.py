"""One owner form for the consultation portrait, copy and destination buttons."""

from pathlib import Path
from typing import Any

from flask import abort, flash, g, redirect, request, send_file
from flask_admin import expose  # type: ignore[import-untyped]
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application.settings import set_setting, setting
from leedbox.application.ui_texts import read_text, set_text
from leedbox.application.versions import fingerprint, lock_content
from leedbox.infrastructure.banner_assets import MAX_BANNER_BYTES, consultation_source, save_banner
from leedbox.web.admin import OwnerView, confirmed, input_field

SETTING_FIELDS = {
    "consultation_photo": "Фото: default — готовый портрет, off — без фото",
    "consultation_contact_url": "«Написать»: ссылка или @username (пусто — контакт владельца)",
    "consultation_channel_url": "«Мой канал»: ссылка или @username",
    "consultation_blog_url": "«Мой блог»: ссылка HTTP/HTTPS",
}
TEXT_FIELDS = {
    "consultations_greeting": "Приветствие от создателя (до 400 символов)",
    "consultations_intro": "Темы, формат и цены консультаций (до 4000 символов)",
}


def page_values(db: Session) -> dict[str, Any]:
    return {
        **{key: setting(db, key) for key in SETTING_FIELDS},
        **{key: read_text(db, key) for key in TEXT_FIELDS},
    }


class ConsultationView(OwnerView):
    @expose("/photo")
    def photo(self) -> Response:
        with Session(self.engine) as db:
            source = consultation_source(
                str(setting(db, "consultation_photo")), self.config.owner_media_directory
            )
        if not isinstance(source, Path):
            abort(404)
        return send_file(source.resolve(), max_age=0)

    @expose("/", methods=["GET", "POST"])
    def index(self) -> str | Response:
        with Session(self.engine) as db:
            current = page_values(db)
        values = current.copy()
        expected = (
            request.form.get("expected", "") if request.method == "POST" else fingerprint(current)
        )
        error = ""
        if request.method == "POST":
            values = {key: request.form.get(key, "") for key in current}
            try:
                confirmed()
                if set(request.form) - {*current, "csrf_token", "confirmed", "expected", "action"}:
                    raise ValueError("Форма содержит неизвестные поля")
                uploads = request.files.getlist("photo_file")
                if len(uploads) > 1:
                    raise ValueError("Загрузите только одно фото")
                with Session(self.engine) as db, db.begin():
                    lock_content(db, "consultation_page", "main")
                    before = page_values(db)
                    if not expected or expected != fingerprint(before):
                        raise ValueError(
                            "Настройки изменились. Обновите страницу перед сохранением"
                        )
                    if uploads and uploads[0].filename:
                        values["consultation_photo"] = save_banner(
                            uploads[0].read(MAX_BANNER_BYTES + 1), self.config.owner_media_directory
                        )
                    for key in SETTING_FIELDS:
                        set_setting(db, self.config, g.owner_id, key, values[key])
                    for key in TEXT_FIELDS:
                        set_text(
                            db, self.config, g.owner_id, key, values[key], fingerprint(before[key])
                        )
                flash("Раздел консультаций обновлён", "success")
                return redirect(self.url + "/")
            except ValueError as problem:
                error = str(problem)
        with Session(self.engine) as db:
            source = consultation_source(
                str(setting(db, "consultation_photo")), self.config.owner_media_directory
            )
        fields = [
            input_field(
                "consultation_photo",
                SETTING_FIELDS["consultation_photo"],
                values["consultation_photo"],
            ),
            input_field(
                "photo_file",
                "Заменить фото PNG/JPEG (до 5 МБ)",
                "",
                "file",
                accept="image/png,image/jpeg",
            ),
            *[
                input_field(
                    key, label, values[key], "textarea", rows=3 if key.endswith("greeting") else 8
                )
                for key, label in TEXT_FIELDS.items()
            ],
            *[
                input_field(key, label, values[key])
                for key, label in SETTING_FIELDS.items()
                if key != "consultation_photo"
            ],
        ]
        return self.editor(
            "Консультации и обучение",
            fields,
            {"expected": expected},
            error=error,
            image_preview=self.url + "/photo" if isinstance(source, Path) else "",
            portrait_preview=True,
            help_text="Фото, приветствие и кнопки меняются без перезапуска. "
            "Ваши темы и цены сохранены отдельно от приветствия. "
            "Ссылки канала и блога заполните ниже; пока они пустые, кнопки сообщают, "
            "что ссылка не настроена. Загруженное фото показывается как есть: "
            "круглое оформление подготовлено только для стандартного портрета.",
            submit_label="Сохранить раздел",
            confirmation_label="Применить изменения раздела консультаций",
        )
