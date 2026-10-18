from waitress import serve

from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.logging import configure_logging
from leedbox.web.app import create_app


def main() -> None:
    configure_logging()
    settings = Settings()
    serve(create_app(settings), host=settings.web_host, port=settings.web_port)


if __name__ == "__main__":
    main()
