"""HTTP API and live stream (Starlette; FastAPI is built on the same framework).

Auth: roles viewer < operator < engineer < admin (Architecture section 10.2). People sign in with user name and
password (POST /api/login, accounts.py) and get a session token; admins manage users under /api/users.
Machines and scripts may use static tokens (OPCARE_API_TOKENS_FILE JSON {"<token>": {"user", "role"}}).
Both are sent as "Authorization: Bearer <token>". The live stream /api/live uses Server-Sent Events and takes
?token= because browsers cannot set headers on EventSource.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .accounts import ROLE_TEXT, AccountError, Accounts
from .catalog import TEMPLATES
from .catalog import catalog as build_catalog
from .services import ApiError, Platform
from .store import iso_to_ms, now_ms

ROLES = {"viewer": 1, "operator": 2, "engineer": 3, "admin": 4}
PASSWORD_CHANGE_ALLOWED = {"/api/me", "/api/me/password", "/api/logout"}
WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"
SSE_KEEPALIVE_S = 15


def create_app(platform: Platform, tokens: dict[str, dict], demo_info: dict | None = None,
               schema_types: set[str] | None = None, login_hint: dict | str | None = None,
               accounts: Accounts | None = None) -> Starlette:
    """demo_info: shown on the configuration screen when running the demo (simulated devices to try).
    schema_types: connector types that have a configuration schema (spec), for the catalog status."""
    store = platform.store
    specified = schema_types or {"modbus", "opcua", "mqtt", "iec61850", "iec104", "dnp3", "camera", "udp", "snmp"}

    accounts = accounts or Accounts(store)

    def bearer(request: Request) -> str:
        header = request.headers.get("authorization", "")
        return header[7:].strip() if header.lower().startswith("bearer ") else request.query_params.get("token", "")

    async def auth(request: Request, need: str) -> dict:
        token = bearer(request)
        who = tokens.get(token)
        if who:
            who = {**who, "kind": "token"}
        else:
            user = await accounts.session(token)
            if not user:
                raise ApiError(401, "UNAUTHORIZED", "please sign in")
            who = {**Accounts.me(user), "token": token}
            if user["must_change_password"] and request.url.path not in PASSWORD_CHANGE_ALLOWED:
                raise ApiError(403, "PASSWORD_CHANGE_REQUIRED", "change your password first")
        if ROLES.get(who["role"], 0) < ROLES[need]:
            raise ApiError(403, "FORBIDDEN", f"role {who['role']} cannot do this (needs {need})")
        return who

    async def body(request: Request) -> dict:
        try:
            return await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "BAD_REQUEST", "body is not valid JSON") from exc

    # -------------------------------------------------------------- endpoints
    async def health(request):
        res = {"status": "ok", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "db": store.dialect}
        if login_hint:          # demo only (run_demo.py --login-hint): the sign-in box shows the demo user
            res["login_hint"] = login_hint
        return JSONResponse(res)

    async def me(request):
        who = await auth(request, "viewer")
        return JSONResponse({k: v for k, v in who.items() if k != "token"})

    # -------------------------------------------------------------- sign in, sign out, own password
    async def login(request):
        data = await body(request)
        if not isinstance(data, dict):
            raise ApiError(400, "BAD_REQUEST", "body must be {username, password}")
        return JSONResponse(await accounts.login(data.get("username"), data.get("password")))

    async def logout(request):
        who = await auth(request, "viewer")
        if who.get("kind") == "session":
            await accounts.logout(who["token"])
            await store.run(store.audit, who["user"], "logout", who["user"], "")
        return Response(status_code=204)

    async def change_password(request):
        who = await auth(request, "viewer")
        if who.get("kind") != "session":
            raise ApiError(400, "BAD_REQUEST", "access tokens have no password")
        data = await body(request)
        data = data if isinstance(data, dict) else {}
        await accounts.change_own_password({"username": who["user"]}, who["token"], data.get("current"), data.get("new"))
        return Response(status_code=204)

    # -------------------------------------------------------------- users (admins)
    async def list_users(request):
        await auth(request, "admin")
        return JSONResponse({"users": await accounts.list_users(), "roles": ROLE_TEXT})

    async def create_user(request):
        who = await auth(request, "admin")
        return JSONResponse(await accounts.create_user(who["user"], await body(request)), status_code=201)

    async def update_user(request):
        who = await auth(request, "admin")
        return JSONResponse(await accounts.update_user(who["user"], request.path_params["name"], await body(request)))

    async def reset_password(request):
        who = await auth(request, "admin")
        data = await body(request)
        await accounts.reset_password(who["user"], request.path_params["name"], data.get("password") if isinstance(data, dict) else None)
        return Response(status_code=204)

    async def delete_user(request):
        who = await auth(request, "admin")
        await accounts.delete_user(who["user"], request.path_params["name"])
        return Response(status_code=204)

    async def list_sources(request):
        await auth(request, "viewer")
        return JSONResponse(await store.run(store.list_sources))

    async def get_source(request):
        await auth(request, "viewer")
        sid = request.path_params["sid"]
        cfg = await store.run(store.get_source, sid)
        if not cfg:
            raise ApiError(404, "NOT_FOUND", f"source {sid} not found")
        status = next((s["status"] for s in await store.run(store.list_sources) if s["source_id"] == sid), None)
        return JSONResponse({"config": cfg, "status": status})

    async def create_source(request):
        who = await auth(request, "engineer")
        doc = await platform.save_source(await body(request), who["user"], create=True)
        return JSONResponse(doc, status_code=201)

    async def update_source(request):
        who = await auth(request, "engineer")
        doc = await platform.save_source(await body(request), who["user"], create=False,
                                         source_id=request.path_params["sid"])
        return JSONResponse(doc)

    async def delete_source(request):
        who = await auth(request, "admin")
        await platform.delete_source(request.path_params["sid"], who["user"])
        return Response(status_code=204)

    async def admin_op(request):
        op = request.path_params["op"]
        who = await auth(request, "viewer" if op in ("ping", "get_diagnostics") else "engineer")
        args = await body(request) if (await request.body()) else {}
        res = await platform.admin(request.path_params["sid"], op, args, who["user"],
                                   int(request.query_params.get("timeout_ms", 10000)))
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)

    async def get_catalog(request):
        await auth(request, "viewer")
        available = platform.available_types if platform.available_types is not None else specified
        return JSONResponse({"types": build_catalog(set(available), specified), "available": sorted(available),
                             "templates": TEMPLATES, "secrets": platform.secrets_dir is not None, "demo": demo_info})

    async def check_source(request):
        await auth(request, "engineer")
        qp = request.query_params
        doc = await platform.check_source(await body(request), create=qp.get("mode", "create") == "create",
                                          source_id=qp.get("source_id"))
        return JSONResponse({"ok": True, "config": doc})

    async def test_unsaved(request):
        who = await auth(request, "engineer")
        qp = request.query_params
        res = await platform.test_unsaved(await body(request), who["user"], create=qp.get("mode", "create") == "create",
                                          source_id=qp.get("source_id"))
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)

    async def browse_unsaved(request):
        who = await auth(request, "engineer")
        qp = request.query_params
        res = await platform.browse_unsaved(await body(request), who["user"],
                                            create=qp.get("mode", "create") == "create", source_id=qp.get("source_id"))
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)

    async def list_secrets(request):
        await auth(request, "engineer")
        return JSONResponse({"names": platform.secret_names(), "managed_here": platform.secrets_dir is not None})

    async def put_secret(request):
        who = await auth(request, "engineer")
        name = request.path_params["name"]
        data = await body(request)
        platform.store_secret(name, data.get("value") if isinstance(data, dict) else None)
        await store.run(store.audit, who["user"], "store_secret", name, "")   # never the value
        return Response(status_code=204)

    async def opcua_certificate(request):
        who = await auth(request, "engineer")
        data = await body(request) if request.method == "POST" else {}
        name = (data or {}).get("name") or request.query_params.get("name") or "opcua-client"
        regenerate = request.method == "POST" and bool((data or {}).get("regenerate"))
        res = platform.opcua_certificate(name, regenerate, create=request.method == "POST")
        if res.get("created"):
            await store.run(store.audit, who["user"], "create_opcua_certificate", name, res["thumbprint"])
        return JSONResponse(res)

    async def tags(request):
        await auth(request, "viewer")
        qp = request.query_params
        return JSONResponse(await store.run(store.list_tags, qp.get("source"), qp.get("q"),
                                            min(int(qp.get("limit", 5000)), 20000)))

    async def history(request):
        await auth(request, "viewer")
        qp = request.query_params
        to_ms = iso_to_ms(qp["to"]) if "to" in qp else now_ms()
        from_ms = iso_to_ms(qp["from"]) if "from" in qp else to_ms - int(qp.get("minutes", 60)) * 60000
        rows = await store.run(store.history, request.path_params["tag"], from_ms, to_ms,
                               min(int(qp.get("limit", 2000)), 20000))
        return JSONResponse(rows)

    async def events(request):
        await auth(request, "viewer")
        qp = request.query_params
        since = iso_to_ms(qp["since"]) if "since" in qp else 0
        return JSONResponse(await store.run(store.events, min(int(qp.get("limit", 100)), 1000), since))

    async def audit(request):
        await auth(request, "admin")
        return JSONResponse(await store.run(store.audit_log))

    async def stats(request):
        await auth(request, "viewer")
        return JSONResponse({"ingest": platform.stats, "sequences": await store.run(store.seq_stats),
                             "live_clients": len(platform.hub.clients), "dropped_clients": platform.hub.dropped_clients})

    async def live(request):
        await auth(request, "viewer")
        q = platform.hub.subscribe()

        async def stream():
            try:
                yield "retry: 3000\n\nevent: hello\ndata: {}\n\n"
                while q in platform.hub.clients:
                    try:
                        msg = await asyncio.wait_for(q.get(), SSE_KEEPALIVE_S)
                        yield f"data: {json.dumps(msg, separators=(',', ':'))}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                platform.hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def index(request):
        return FileResponse(WEB_DIR / "index.html")

    # -------------------------------------------------------------- errors
    async def account_error(request, exc: AccountError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    async def api_error(request, exc: ApiError):
        err = {"code": exc.code, "message": exc.message}
        if exc.details is not None:
            err["details"] = exc.details
        return JSONResponse({"error": err}, status_code=exc.status)

    async def bad_value(request, exc: ValueError):
        return JSONResponse({"error": {"code": "BAD_REQUEST", "message": str(exc)[:300]}}, status_code=400)

    routes = [
        Route("/api/health", health),
        Route("/api/me", me),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/me/password", change_password, methods=["POST"]),
        Route("/api/users", list_users, methods=["GET"]),
        Route("/api/users", create_user, methods=["POST"]),
        Route("/api/users/{name}", update_user, methods=["PUT"]),
        Route("/api/users/{name}", delete_user, methods=["DELETE"]),
        Route("/api/users/{name}/password", reset_password, methods=["POST"]),
        Route("/api/sources", list_sources, methods=["GET"]),
        Route("/api/sources", create_source, methods=["POST"]),
        Route("/api/catalog", get_catalog),
        Route("/api/sources-check", check_source, methods=["POST"]),
        Route("/api/sources-test", test_unsaved, methods=["POST"]),
        Route("/api/sources-browse", browse_unsaved, methods=["POST"]),
        Route("/api/secrets", list_secrets, methods=["GET"]),
        Route("/api/secrets/{name}", put_secret, methods=["PUT"]),
        Route("/api/opcua-certificate", opcua_certificate, methods=["GET", "POST"]),
        Route("/api/sources/{sid}", get_source, methods=["GET"]),
        Route("/api/sources/{sid}", update_source, methods=["PUT"]),
        Route("/api/sources/{sid}", delete_source, methods=["DELETE"]),
        Route("/api/sources/{sid}/admin/{op}", admin_op, methods=["POST"]),
        Route("/api/tags", tags),
        Route("/api/tags/{tag}/history", history),
        Route("/api/events", events),
        Route("/api/audit", audit),
        Route("/api/stats", stats),
        Route("/api/live", live),
        Route("/", index),
        Mount("/static", StaticFiles(directory=WEB_DIR), name="static"),
    ]
    return Starlette(routes=routes, exception_handlers={ApiError: api_error, AccountError: account_error, ValueError: bad_value})
