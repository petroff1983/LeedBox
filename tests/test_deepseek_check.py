"""Offline tests for the bounded diagnostic, not a production LLM integration."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock


def load_check():
    spec = importlib.util.spec_from_file_location(
        "deepseek_check", Path(__file__).parents[1] / "scripts" / "check-deepseek.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_key_never_calls_api(monkeypatch, capsys):
    check = load_check()
    monkeypatch.setattr(check, "dotenv_values", lambda *a, **k: {})
    monkeypatch.setattr("sys.argv", ["check", "--generate"])
    client = Mock()
    monkeypatch.setattr(check, "fetch", client)
    assert check.main() == 1
    client.assert_not_called()


def test_synthetic_generation_is_bounded_and_secret_not_printed(monkeypatch, capsys):
    check = load_check()
    secret = "synthetic-private-key"
    monkeypatch.setattr(check, "dotenv_values", lambda *a, **k: {"deepsee_API": secret})
    monkeypatch.setattr("sys.argv", ["check", "--generate"])
    client = Mock(
        side_effect=[
            {"data": [{"id": "deepseek-flash"}]},
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "entities": ["растения"],
                                    "markers": ["ошибка"],
                                    "headlines": [
                                        "Заголовок один",
                                        "Заголовок два",
                                        "Заголовок три",
                                    ],
                                }
                            )
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            },
        ]
    )
    monkeypatch.setattr(check, "fetch", client)
    assert check.main() == 0
    assert client.call_count == 2
    body = client.call_args.args[2]
    assert body["max_tokens"] == 500
    assert body["thinking"] == {"type": "disabled"}
    assert secret not in json.dumps(body)
    assert secret not in capsys.readouterr().out


def test_unknown_model_never_triggers_paid_generation(monkeypatch):
    check = load_check()
    monkeypatch.setattr(check, "dotenv_values", lambda *a, **k: {"DEEPSEEK_API_KEY": "test"})
    monkeypatch.setattr("sys.argv", ["check", "--generate"])
    client = Mock(return_value={"data": [{"id": "unknown-model"}]})
    monkeypatch.setattr(check, "fetch", client)
    assert check.main() == 2
    client.assert_called_once()
