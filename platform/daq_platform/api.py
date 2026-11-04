"""HTTP API and live stream (Starlette; FastAPI is built on the same framework).

Auth: bearer tokens with roles viewer < operator < engineer < admin (Architecture section 10.2).
Tokens come from OPCARE_API_TOKENS (JSON {"<token>": {"user": "...", "role": "..."}}); LDAP/AD login
replaces this in a later release. The live stream /api/live uses Server-Sent Events and takes ?token=
because browsers cannot set headers on EventSource.
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

from .catalog import catalog as build_catalog
from .services import ApiError, Platform
from .store import iso_to_ms, now_ms

ROLES = {"viewer": 1, "operator": 2, "engineer": 3, "admin": 4}
WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"
SSE_KEEPALIVE_S = 15


def create_app(platform: Platform, tokens: dict[str, dict], demo_info: dict | None = None,
               schema_types: set[str] | None = None, login_hint: str | None = None) -> Starlette:
    """demo_info: shown on the configuration screen when running the demo (simulated devices to try).
    schema_types: connector types that have a configuration schema (spec), for the catalog status."""
    store = platform.store
    specified = schema_types or {"modbus", "opcua", "mqtt", "iec61850", "iec104", "dnp3", "camera"}

    def auth(request: Request, need: str) -> dict:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        who = tokens.get(token)
        if not who:
            raise ApiError(401, "UNAUTHORIZED", "missing or invalid token")
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
        if login_hint:          # demo only (run_demo.py --login-hint): the sign-in box offers the demo token
            res["login_hint"] = login_hint
        return JSONResponse(res)

    async def me(request):
        return JSONResponse(auth(request, "viewer"))

    async def list_sources(request):
        auth(request, "viewer")
        return JSONResponse(await store.run(store.list_sources))

    async def get_source(request):
        auth(request, "viewer")
        sid = request.path_params["sid"]
        cfg = await store.run(store.get_source, sid)
        if not cfg:
            raise ApiError(404, "NOT_FOUND", f"source {sid} not found")
        status = next((s["status"] for s in await store.run(store.list_sources) if s["source_id"] == sid), None)
        return JSONResponse({"config": cfg, "status": status})

    async def create_source(request):
        who = auth(request, "engineer")
        doc = await platform.save_source(await body(request), who["user"], create=True)
        return JSONResponse(doc, status_code=201)

    async def update_source(request):
        who = auth(request, "engineer")
        doc = await platform.save_source(await body(request), who["user"], create=False,
                                         source_id=request.path_params["sid"])
        return JSONResponse(doc)

    async def delete_source(request):
        who = auth(request, "admin")
        await platform.delete_source(request.path_params["sid"], who["user"])
        return Response(status_code=204)

    async def admin_op(request):
        op = request.path_params["op"]
        who = auth(request, "viewer" if op in ("ping", "get_diagnostics") else "engineer")
        args = await body(request) if (await request.body()) else {}
        res = await platform.admin(request.path_params["sid"], op, args, who["user"],
                                   int(request.query_params.get("timeout_ms", 10000)))
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)

    async def get_catalog(request):
        auth(request, "viewer")
        available = platform.available_types if platform.available_types is not None else specified
        return JSONResponse({"types": build_catalog(set(available), specified), "available": sorted(available),
                             "demo": demo_info})

    async def check_source(request):
        auth(request, "engineer")
        qp = request.query_params
        doc = await platform.check_source(await body(request), create=qp.get("mode", "create") == "create",
                                          source_id=qp.get("source_id"))
        return JSONResponse({"ok": True, "config": doc})

    async def test_unsaved(request):
        who = auth(request, "engineer")
        qp = request.query_params
        res = await platform.test_unsaved(await body(request), who["user"], create=qp.get("mode", "create") == "create",
                                          source_id=qp.get("source_id"))
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)

    async def tags(request):
        auth(request, "viewer")
        qp = request.query_params
        return JSONResponse(await store.run(store.list_tags, qp.get("source"), qp.get("q"),
                                            min(int(qp.get("limit", 5000)), 20000)))

    async def history(request):
        auth(request, "viewer")
        qp = request.query_params
        to_ms = iso_to_ms(qp["to"]) if "to" in qp else now_ms()
        from_ms = iso_to_ms(qp["from"]) if "from" in qp else to_ms - int(qp.get("minutes", 60)) * 60000
        rows = await store.run(store.history, request.path_params["tag"], from_ms, to_ms,
                               min(int(qp.get("limit", 2000)), 20000))
        return JSONResponse(rows)

    async def events(request):
        auth(request, "viewer")
        qp = request.query_params
        since = iso_to_ms(qp["since"]) if "since" in qp else 0
        return JSONResponse(await store.run(store.events, min(int(qp.get("limit", 100)), 1000), since))

    async def audit(request):
        auth(request, "admin")
        return JSONResponse(await store.run(store.audit_log))

    async def stats(request):
        auth(request, "viewer")
        return JSONResponse({"ingest": platform.stats, "sequences": await store.run(store.seq_stats),
                             "live_clients": len(platform.hub.clients), "dropped_clients": platform.hub.dropped_clients})

    async def live(request):
        auth(request, "viewer")
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
        Route("/api/sources", list_sources, methods=["GET"]),
        Route("/api/sources", create_source, methods=["POST"]),
        Route("/api/catalog", get_catalog),
        Route("/api/sources-check", check_source, methods=["POST"]),
        Route("/api/sources-test", test_unsaved, methods=["POST"]),
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
    return Starlette(routes=routes, exception_handlers={ApiError: api_error, ValueError: bad_value})
