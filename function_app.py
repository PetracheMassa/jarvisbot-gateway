import json
import logging
import os
import uuid
from functools import lru_cache
from typing import Dict, Optional

import azure.functions as func
import requests
from requests import Response, Session
from requests.exceptions import RequestException, Timeout

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

SERVICE_NAME = "jarvisbot-gateway"
DEFAULT_TIMEOUT_SECONDS = 60
MAX_LOG_BODY_LENGTH = 500

# Reuse one session across warm executions
HTTP_SESSION: Session = requests.Session()


def json_response(
    status_code: int,
    payload: Dict[str, object],
    headers: Optional[Dict[str, str]] = None,
) -> func.HttpResponse:
    response_headers = {"content-type": "application/json; charset=utf-8"}
    if headers:
        response_headers.update(headers)

    return func.HttpResponse(
        body=json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        headers=response_headers,
    )


@lru_cache(maxsize=1)
def get_backend_base_url() -> str:
    value = os.environ.get("BACKEND_BASE_URL", "").strip().rstrip("/")
    if not value:
        raise RuntimeError("BACKEND_BASE_URL is not configured")
    return value


@lru_cache(maxsize=1)
def get_backend_timeout_seconds() -> int:
    raw = os.environ.get(
        "BACKEND_TIMEOUT_SECONDS",
        str(DEFAULT_TIMEOUT_SECONDS),
    ).strip()

    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("BACKEND_TIMEOUT_SECONDS must be an integer") from exc

    if value <= 0:
        raise RuntimeError("BACKEND_TIMEOUT_SECONDS must be greater than 0")

    return value


def build_backend_messages_url() -> str:
    return f"{get_backend_base_url()}/api/messages"


def get_request_id(req: func.HttpRequest) -> str:
    return (
        req.headers.get("x-request-id")
        or req.headers.get("x-ms-client-request-id")
        or str(uuid.uuid4())
    )


def filtered_forward_headers(req: func.HttpRequest, request_id: str) -> Dict[str, str]:
    """
    Forward only the headers that are needed.
    Never log or expose the Authorization header value.
    """
    allowed: Dict[str, str] = {}

    for name in (
        "authorization",
        "content-type",
        "user-agent",
        "accept",
        "traceparent",
        "tracestate",
        "x-ms-client-request-id",
    ):
        value = req.headers.get(name)
        if value:
            allowed[name] = value

    # Always provide a request id for tracing
    allowed["x-request-id"] = request_id
    return allowed


def build_response_headers(response: Response, request_id: str) -> Dict[str, str]:
    headers: Dict[str, str] = {
        "x-request-id": request_id,
    }

    content_type = response.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type

    retry_after = response.headers.get("retry-after")
    if retry_after:
        headers["retry-after"] = retry_after

    return headers


def short_text(value: bytes, limit: int = MAX_LOG_BODY_LENGTH) -> str:
    try:
        text = value.decode("utf-8", errors="replace")
    except Exception:
        return "<binary>"
    return text[:limit]


def log_request_start(req: func.HttpRequest, request_id: str) -> None:
    body_length = len(req.get_body() or b"")
    logging.info(
        "Incoming /messages request. request_id=%s method=%s body_length=%s",
        request_id,
        req.method,
        body_length,
    )


@app.route(route="messages", methods=["POST"])
def messages(req: func.HttpRequest) -> func.HttpResponse:
    request_id = get_request_id(req)

    try:
        log_request_start(req, request_id)

        auth_header = req.headers.get("authorization")
        if not auth_header:
            logging.warning(
                "Rejected request without Authorization header. request_id=%s",
                request_id,
            )
            return json_response(
                status_code=401,
                payload={
                    "ok": False,
                    "error": "missing_authorization",
                    "message": "Authorization header is required.",
                    "request_id": request_id,
                },
                headers={"x-request-id": request_id},
            )

        backend_url = build_backend_messages_url()
        timeout_seconds = get_backend_timeout_seconds()
        headers = filtered_forward_headers(req, request_id)
        body = req.get_body()

        # Do not log backend_url to avoid exposing internal endpoint details
        logging.info(
            "Forwarding request to backend. request_id=%s timeout_seconds=%s",
            request_id,
            timeout_seconds,
        )

        response = HTTP_SESSION.post(
            backend_url,
            headers=headers,
            data=body,
            timeout=timeout_seconds,
        )

        logging.info(
            "Backend response received. request_id=%s status_code=%s content_type=%s",
            request_id,
            response.status_code,
            response.headers.get("content-type"),
        )

        if response.status_code >= 400:
            logging.warning(
                "Backend returned error. request_id=%s status_code=%s body=%s",
                request_id,
                response.status_code,
                short_text(response.content),
            )

        return func.HttpResponse(
            body=response.content,
            status_code=response.status_code,
            headers=build_response_headers(response, request_id),
        )

    except Timeout:
        logging.exception(
            "Backend timeout while forwarding request. request_id=%s",
            request_id,
        )
        return json_response(
            status_code=504,
            payload={
                "ok": False,
                "error": "backend_timeout",
                "message": "The backend did not respond in time.",
                "request_id": request_id,
            },
            headers={"x-request-id": request_id},
        )

    except RequestException:
        logging.exception(
            "Backend request error while forwarding request. request_id=%s",
            request_id,
        )
        return json_response(
            status_code=502,
            payload={
                "ok": False,
                "error": "backend_request_error",
                "message": "The gateway could not reach the backend service.",
                "request_id": request_id,
            },
            headers={"x-request-id": request_id},
        )

    except RuntimeError:
        logging.exception(
            "Gateway configuration error. request_id=%s",
            request_id,
        )
        return json_response(
            status_code=500,
            payload={
                "ok": False,
                "error": "gateway_configuration_error",
                "message": "The gateway is not configured correctly.",
                "request_id": request_id,
            },
            headers={"x-request-id": request_id},
        )

    except Exception:
        logging.exception(
            "Unexpected gateway error. request_id=%s",
            request_id,
        )
        return json_response(
            status_code=500,
            payload={
                "ok": False,
                "error": "gateway_error",
                "message": "An unexpected gateway error occurred.",
                "request_id": request_id,
            },
            headers={"x-request-id": request_id},
        )


@app.route(route="healthz", methods=["GET"])
def healthz(req: func.HttpRequest) -> func.HttpResponse:
    request_id = get_request_id(req)

    try:
        timeout_seconds = get_backend_timeout_seconds()
        _ = get_backend_base_url()

        return json_response(
            status_code=200,
            payload={
                "ok": True,
                "service": SERVICE_NAME,
                "backend_configured": True,
                "timeout_seconds": timeout_seconds,
                "request_id": request_id,
            },
            headers={"x-request-id": request_id},
        )

    except Exception:
        logging.exception("Health check failed. request_id=%s", request_id)
        return json_response(
            status_code=500,
            payload={
                "ok": False,
                "service": SERVICE_NAME,
                "error": "health_check_failed",
                "message": "Health check failed.",
                "request_id": request_id,
            },
            headers={"x-request-id": request_id},
        )