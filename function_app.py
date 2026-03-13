import json
import logging
import os
from typing import Dict

import azure.functions as func
import requests
from requests import Response
from requests.exceptions import RequestException, Timeout

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def get_backend_base_url() -> str:
    value = os.environ.get("BACKEND_BASE_URL", "").strip().rstrip("/")
    if not value:
        raise RuntimeError("BACKEND_BASE_URL is not configured")
    return value


def get_backend_timeout_seconds() -> int:
    raw = os.environ.get("BACKEND_TIMEOUT_SECONDS", "60").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("BACKEND_TIMEOUT_SECONDS must be an integer") from exc

    if value <= 0:
        raise RuntimeError("BACKEND_TIMEOUT_SECONDS must be > 0")

    return value


def build_backend_messages_url() -> str:
    return f"{get_backend_base_url()}/api/messages"


def filtered_headers(req: func.HttpRequest) -> Dict[str, str]:
    allowed: Dict[str, str] = {}

    for name in ("authorization", "content-type", "user-agent"):
        value = req.headers.get(name)
        if value:
            allowed[name] = value

    return allowed


def build_response_headers(response: Response) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    content_type = response.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    return headers


def short_text(value: bytes, limit: int = 500) -> str:
    try:
        text = value.decode("utf-8", errors="replace")
    except Exception:
        return "<binary>"
    return text[:limit]


@app.route(route="messages", methods=["POST"])
def messages(req: func.HttpRequest) -> func.HttpResponse:
    try:
        auth = req.headers.get("authorization")
        if not auth:
            logging.warning("Rejected request without Authorization header")
            return func.HttpResponse(
                body=json.dumps(
                    {
                        "ok": False,
                        "error": "missing_authorization",
                        "message": "Authorization header is required.",
                    }
                ),
                status_code=401,
                mimetype="application/json",
            )

        backend_url = build_backend_messages_url()
        timeout_seconds = get_backend_timeout_seconds()
        headers = filtered_headers(req)
        body = req.get_body()

        logging.info(
            "Forwarding bot request to backend: %s (timeout=%ss)",
            backend_url,
            timeout_seconds,
        )

        response = requests.post(
            backend_url,
            headers=headers,
            data=body,
            timeout=timeout_seconds,
        )

        logging.info(
            "Backend response received: status=%s, content_type=%s",
            response.status_code,
            response.headers.get("content-type"),
        )

        if response.status_code >= 400:
            logging.warning(
                "Backend returned error status=%s, body=%s",
                response.status_code,
                short_text(response.content),
            )

        return func.HttpResponse(
            body=response.content,
            status_code=response.status_code,
            headers=build_response_headers(response),
        )

    except Timeout:
        logging.exception("Gateway timeout while calling backend")
        return func.HttpResponse(
            body=json.dumps(
                {
                    "ok": False,
                    "error": "backend_timeout",
                    "message": "The backend did not respond in time.",
                }
            ),
            status_code=504,
            mimetype="application/json",
        )

    except RequestException as exc:
        logging.exception("Gateway request error while calling backend")
        return func.HttpResponse(
            body=json.dumps(
                {
                    "ok": False,
                    "error": "backend_request_error",
                    "message": str(exc),
                }
            ),
            status_code=502,
            mimetype="application/json",
        )

    except Exception as exc:
        logging.exception("Unexpected gateway error")
        return func.HttpResponse(
            body=json.dumps(
                {
                    "ok": False,
                    "error": "gateway_error",
                    "message": str(exc),
                }
            ),
            status_code=500,
            mimetype="application/json",
        )


@app.route(route="healthz", methods=["GET"])
def healthz(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = {
            "ok": True,
            "service": "jarvisbot-gateway",
        }
        return func.HttpResponse(
            body=json.dumps(payload),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as exc:
        logging.exception("Health check failed")
        return func.HttpResponse(
            body=json.dumps(
                {
                    "ok": False,
                    "service": "jarvisbot-gateway",
                    "error": "health_check_failed",
                    "message": str(exc),
                }
            ),
            status_code=500,
            mimetype="application/json",
        )