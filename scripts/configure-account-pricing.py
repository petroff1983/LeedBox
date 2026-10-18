"""Explicit local pricing update; does not enable payments, wallets or paid access."""

import json

from sqlalchemy.orm import Session

from leedbox.application.settings import set_setting, setting
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine

VALUES = {
    "ai_token_price_rub": "0.0003",
    "ai_token_purchase_min_rub": "200.00",
    "wallet_topup_min_rub": "200.00",
    "ai_dzen_search_tokens": 4000,
    "ai_content_search_tokens": 400000,
    "ai_usd_rub": "100",
}


def main():
    config = Settings()
    if config.environment != "development" or not config.owner_ids:
        raise ValueError("Only local development with a configured owner")
    engine = create_database_engine(config)
    try:
        with Session(engine) as db, db.begin():
            unchanged = {
                key: setting(db, key)
                for key in ("wallet_enabled", "wallet_visible", "wallet_topup_enabled")
            }
            for key, value in VALUES.items():
                set_setting(db, config, min(config.owner_ids), key, value)
            assert unchanged == {key: setting(db, key) for key in unchanged}
        print(
            json.dumps(
                {
                    "pricing_updated": True,
                    "payment_switches_unchanged": True,
                    "token_price_rub": "0.0003",
                    "minimum_rub": "200.00",
                }
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"pricing_updated": False, "secrets_omitted": True}))
        raise SystemExit(1) from None
