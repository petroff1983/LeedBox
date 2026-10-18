from dataclasses import dataclass
from urllib.parse import urlsplit


def http_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ValueError("Нужна корректная ссылка http:// или https://") from None
    if "\\" in value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Ссылка не должна содержать пробелы")
    return value


@dataclass(frozen=True)
class Content:
    title: str
    description: str
    kind: str
    value: str

    def validate(self) -> None:
        if not self.title.strip() or len(self.title) > 60:
            raise ValueError("Название: от 1 до 60 символов")
        if len(self.description) > 300:
            raise ValueError("Описание: не более 300 символов")
        if self.kind not in {"text", "url", "document", "photo", "video"} or not self.value:
            raise ValueError("Выберите поддерживаемый тип и содержимое")
        if self.kind == "text" and len(self.value) > 4000:
            raise ValueError("Текст: не более 4000 символов")
        if self.kind == "url":
            http_url(self.value)
