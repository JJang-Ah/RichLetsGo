"""Loopback-only Trader API; the authenticated server determines the web actor."""

import hmac
import re
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from richletsgo.domain import Environment
from richletsgo.keystore import Secret
from richletsgo_dashboard.security import HEADERS, allowed_hosts, require_loopback, same_origin

from .models import COMMANDS, Context
from .service import CommandDenied, CommandService


class CommandBody(BaseModel):
    session_id: str = Field(min_length=64, max_length=64)
    environment: Environment
    account_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    target: str | None = Field(default=None, max_length=256)
    confirmation_id: str | None = Field(default=None, max_length=256)
    manually_checked: bool = False


class RevokeBody(BaseModel):
    session_id: str = Field(min_length=64, max_length=64)


def create_control_app(
    service: CommandService,
    *,
    operator_id: str,
    token: Secret,
    port: int,
    dashboard_port: int,
    host: str = "127.0.0.1",
) -> FastAPI:
    require_loopback(host, port)
    require_loopback(host, dashboard_port)
    if not operator_id.strip() or not token.reveal() or port == dashboard_port:
        raise ValueError("operator, distinct ports and environment control token required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host") not in allowed_hosts(port):
            result: Response = Response("invalid host", status_code=400)
        elif not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + token.reveal()).encode()
        ):
            result = Response("unauthorized", status_code=401)
        elif request.method != "GET" and not same_origin(
            request.headers.get("origin"), dashboard_port
        ):
            result = Response("invalid origin", status_code=403)
        else:
            result = await call_next(request)
        for key, value in HEADERS.items():
            result.headers[key] = value
        return result

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"environment": service.environment.value, "account_id": service.account_id}

    @app.post("/commands/{command}")
    async def command_route(command: str, body: CommandBody) -> Response:
        if command not in COMMANDS:
            return Response("unknown command", status_code=404)
        if not re.fullmatch(r"[0-9a-f]{64}", body.session_id):
            return Response("invalid session hash", status_code=400)
        try:
            context = Context(
                "web",
                "web:" + operator_id,
                body.session_id,
                body.environment,
                body.account_id,
                body.request_id,
            )
            result = await service.request(
                context,
                command,
                target=body.target,
                confirmation_id=body.confirmation_id,
                manually_checked=body.manually_checked,
            )
            return JSONResponse(result)
        except CommandDenied as error:
            return JSONResponse({"state": "rejected", "message": str(error)}, status_code=403)
        except OSError:
            return JSONResponse(
                {"state": "unavailable", "message": "저장소 확인 필요"}, status_code=503
            )

    @app.post("/sessions/revoke")
    async def revoke(body: RevokeBody) -> Response:
        if not re.fullmatch(r"[0-9a-f]{64}", body.session_id):
            return Response("invalid session hash", status_code=400)
        await service.revoke("web", body.session_id)
        return Response(status_code=204)

    return app
