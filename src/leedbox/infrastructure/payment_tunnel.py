"""Development-only, path-allowlisted gateway. Never expose the owner web app itself."""

from http.client import HTTPConnection

from flask import Flask, Response, request
from waitress import serve

from leedbox.infrastructure.config import Settings

PATHS = {
    "/payments/robokassa/result": {"POST"},
    "/payments/robokassa/success": {"GET", "POST"},
    "/payments/robokassa/fail": {"GET", "POST"},
}
MAX_BODY = 16384


def create_gateway(config: Settings) -> Flask:
    if config.environment != "development" or not config.robokassa_test:
        raise ValueError("Payment tunnel is restricted to local test payments")
    if config.web_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Payment tunnel requires a loopback web application")
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY

    @app.after_request
    def secure(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    @app.route(
        "/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
    )
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    def gateway(path: str) -> Response:
        route = "/" + path
        if route == "/payments/robokassa/tunnel-health" and request.method == "GET":
            return Response("LeedBox test payment tunnel ready", mimetype="text/plain")
        if route not in PATHS:
            return Response("Not found", status=404, mimetype="text/plain")
        if request.method not in PATHS[route]:
            return Response("Method not allowed", status=405, mimetype="text/plain")
        if request.content_length is not None and request.content_length > MAX_BODY:
            return Response("Request too large", status=413, mimetype="text/plain")
        if route.endswith("/result") and (
            request.query_string or request.mimetype != "application/x-www-form-urlencoded"
        ):
            return Response("Invalid payment notification", status=400, mimetype="text/plain")
        body = request.get_data(cache=False)
        if len(body) > MAX_BODY:
            return Response("Request too large", status=413, mimetype="text/plain")
        target = route
        if request.query_string:
            target += "?" + request.query_string.decode("ascii", errors="strict")
        connection = HTTPConnection("127.0.0.1", config.web_port, timeout=10)
        try:
            # Destination is fixed; cookies, auth, Host and forwarding headers are NOT relayed.
            connection.request(
                request.method,
                target,
                body=body,
                headers={"Content-Type": request.content_type or "application/octet-stream"},
            )
            upstream = connection.getresponse()
            data = upstream.read(65537)
            if len(data) > 65536:
                return Response("Invalid upstream response", status=502, mimetype="text/plain")
            return Response(
                data,
                status=upstream.status,
                content_type=upstream.getheader("Content-Type", "text/plain"),
            )
        except (OSError, ValueError):
            return Response(
                "Payment handler temporarily unavailable", status=502, mimetype="text/plain"
            )
        finally:
            connection.close()

    return app


def main() -> None:
    # No polling, credentials in output, arbitrary target URL or admin routes.
    serve(create_gateway(Settings()), host="127.0.0.1", port=5011, threads=4)


if __name__ == "__main__":
    main()
