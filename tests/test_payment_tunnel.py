from unittest.mock import MagicMock, patch

import pytest

from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.payment_tunnel import create_gateway


def config():
    return Settings(
        database_url="postgresql+psycopg://fixture:fixture@localhost/leedbox_test",
        environment="development",
        robokassa_test=True,
    )


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/owner/",
        "/owner/login",
        "/cabinet",
        "/ready",
        "/static/owner.css",
        "/payments/robokassa/../owner",
        "/%2e%2e/owner",
        "/payments/robokassa/result/extra",
    ],
)
def test_gateway_does_not_expose_private_routes(path):
    with patch("leedbox.infrastructure.payment_tunnel.HTTPConnection") as network:
        assert create_gateway(config()).test_client().get(path).status_code == 404
        network.assert_not_called()


def test_gateway_health_and_method_and_size_limits():
    client = create_gateway(config()).test_client()
    assert client.get("/payments/robokassa/tunnel-health").status_code == 200
    assert client.get("/payments/robokassa/result").status_code == 405
    assert client.post("/payments/robokassa/result?x=1", data={"a": "b"}).status_code == 400
    assert client.post("/payments/robokassa/result", json={"a": "b"}).status_code == 400
    assert client.post("/payments/robokassa/result", data={"a": "b" * 17000}).status_code == 413


def test_gateway_proxies_only_fixed_target_and_no_credentials():
    connection = MagicMock()
    upstream = connection.getresponse.return_value
    upstream.read.return_value = b"OK123"
    upstream.status = 200
    upstream.getheader.return_value = "text/plain"
    with patch(
        "leedbox.infrastructure.payment_tunnel.HTTPConnection", return_value=connection
    ) as network:
        client = create_gateway(config()).test_client()
        response = client.post(
            "/payments/robokassa/result",
            data={"InvId": "123"},
            headers={
                "Authorization": "secret",
                "Cookie": "session=secret",
                "X-Forwarded-Host": "evil.test",
            },
        )
        assert response.data == b"OK123"
        network.assert_called_once_with("127.0.0.1", config().web_port, timeout=10)
        assert connection.request.call_args.args[:2] == ("POST", "/payments/robokassa/result")
        assert set(connection.request.call_args.kwargs["headers"]) == {"Content-Type"}
        assert "Set-Cookie" not in response.headers
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "updates", [{"environment": "production"}, {"robokassa_test": False}, {"web_host": "0.0.0.0"}]
)
def test_gateway_refuses_live_or_public_target(updates):
    with pytest.raises(ValueError):
        create_gateway(config().model_copy(update=updates))
