"""User accounts: sign-in, sessions, own password, admin user management, safety rules and the screens.

Run from the repository root:  python -m pytest platform/tests/test_users.py -q
"""
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in ("platform", "edge", "connectors/sdk-python", "connectors/modbus", "tools/simulators", "tools/demo"):
    sys.path.insert(0, str(ROOT / p))

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from daq_platform.accounts import Accounts, hash_password, password_problem, verify_password  # noqa: E402
from daq_platform.api import create_app  # noqa: E402
from daq_platform.bus import InMemoryBus  # noqa: E402
from daq_platform.services import Platform  # noqa: E402
from daq_platform.store import Store  # noqa: E402
from test_config_screens import VALIDATOR, free_port, http  # noqa: E402

ADMIN_PW = "First-admin-2026"


class Stack:
    async def __aenter__(self):
        self.bus, self.store = InMemoryBus(), Store("sqlite://:memory:")
        self.platform = Platform(self.store, self.bus, VALIDATOR)
        await self.platform.start()
        self.accounts = Accounts(self.store)
        assert await self.accounts.ensure_admin("admin", ADMIN_PW, "Khaled") is True
        assert await self.accounts.ensure_admin("other", ADMIN_PW) is False          # only on an empty database
        self.port = free_port()
        app = create_app(self.platform, {"svc-token": {"user": "scada-gw", "role": "viewer"}}, accounts=self.accounts,
                         login_hint={"username": "admin", "password": ADMIN_PW})
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.srv = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.05)
        self.base = f"http://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        await asyncio.gather(self.srv, return_exceptions=True)
        await self.platform.stop()
        self.store.close()

    async def call(self, method, path, token=None, body=None):
        return await asyncio.to_thread(http, method, self.base + path, token, body)

    async def login(self, user, password):
        return await self.call("POST", "/api/login", None, {"username": user, "password": password})


def test_password_hashing_and_policy():
    h = hash_password("Correct-horse-9")
    assert h.startswith("scrypt$") and "Correct" not in h
    assert verify_password("Correct-horse-9", h) and not verify_password("correct-horse-9", h)
    assert hash_password("Correct-horse-9") != h                                       # salted
    assert verify_password("x", "garbage") is False
    assert password_problem("short1") and password_problem("onlyletters-here") and password_problem("1234567890")
    assert "user name" in password_problem("ahmed-2026-pass", "ahmed")
    assert password_problem("Good-password-1", "ahmed") is None


def test_accounts_api_flow():
    async def run():
        async with Stack() as s:
            # sign in
            assert (await s.call("GET", "/api/me"))[0] == 401
            status, res = await s.login("admin", "wrong-password-1")
            assert status == 401 and res["error"]["code"] == "LOGIN_FAILED"
            status, res = await s.login("ADMIN ", ADMIN_PW)                          # case and spaces tolerated
            assert status == 200 and res["role"] == "admin" and res["name"] == "Khaled" and len(res["token"]) > 30
            admin = res["token"]
            assert (await s.call("GET", "/api/me", admin))[1]["user"] == "admin"
            assert (await s.call("GET", "/api/sources", admin))[0] == 200
            assert (await s.call("GET", "/api/health"))[1]["login_hint"]["username"] == "admin"

            # admin adds an engineer; the password never comes back
            new = {"username": "Ahmed.Hassan", "name": "Ahmed Hassan", "email": "ahmed@example.com", "role": "engineer",
                   "password": "Temp-pass-2026"}
            status, user = await s.call("POST", "/api/users", admin, new)
            assert status == 201 and user["username"] == "ahmed.hassan" and user["must_change_password"] is True
            assert "password_hash" not in json.dumps(user) and "scrypt" not in json.dumps(user) and "Temp-pass" not in json.dumps(user)
            assert (await s.call("POST", "/api/users", admin, new))[0] == 409
            assert (await s.call("POST", "/api/users", admin, {**new, "username": "x y"}))[0] == 400
            assert (await s.call("POST", "/api/users", admin, {**new, "username": "weak", "password": "abc"}))[1]["error"]["code"] == "WEAK_PASSWORD"
            assert (await s.call("POST", "/api/users", admin, {**new, "username": "r", "role": "god"}))[0] == 400
            status, listing = await s.call("GET", "/api/users", admin)
            assert [u["username"] for u in listing["users"]] == ["admin", "ahmed.hassan"] and "engineer" in listing["roles"]

            # first sign-in: must change the temporary password before anything else
            _, res = await s.login("ahmed.hassan", "Temp-pass-2026")
            eng = res["token"]
            assert res["must_change_password"] is True
            status, res = await s.call("GET", "/api/sources", eng)
            assert status == 403 and res["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
            assert (await s.call("POST", "/api/me/password", eng, {"current": "nope-nope-1", "new": "Ahmed-own-2026"}))[0] == 400
            assert (await s.call("POST", "/api/me/password", eng, {"current": "Temp-pass-2026", "new": "short"}))[0] == 400
            assert (await s.call("POST", "/api/me/password", eng, {"current": "Temp-pass-2026", "new": "Own-secret-2026"}))[0] == 204
            assert (await s.call("GET", "/api/sources", eng))[0] == 200
            assert (await s.login("ahmed.hassan", "Temp-pass-2026"))[0] == 401
            assert (await s.login("ahmed.hassan", "Own-secret-2026"))[0] == 200

            # roles are enforced, and a role change applies at once
            assert (await s.call("GET", "/api/users", eng))[0] == 403
            assert (await s.call("POST", "/api/sources-check", eng, {}))[0] in (400, 422)       # engineer may check sources
            await s.call("PUT", "/api/users/ahmed.hassan", admin, {"role": "viewer"})
            assert (await s.call("POST", "/api/sources-check", eng, {}))[0] == 403
            assert (await s.call("GET", "/api/me", eng))[1]["role"] == "viewer"

            # disable signs the user out; enable allows sign-in again
            await s.call("PUT", "/api/users/ahmed.hassan", admin, {"active": False})
            assert (await s.call("GET", "/api/me", eng))[0] == 401
            assert (await s.login("ahmed.hassan", "Own-secret-2026"))[0] == 401
            await s.call("PUT", "/api/users/ahmed.hassan", admin, {"active": True})
            _, res = await s.login("ahmed.hassan", "Own-secret-2026")
            eng = res["token"]

            # reset by an admin: sessions end, a new temporary password must be changed
            assert (await s.call("POST", "/api/users/ahmed.hassan/password", admin, {"password": "Reset-temp-2026"}))[0] == 204
            assert (await s.call("GET", "/api/me", eng))[0] == 401
            assert (await s.login("ahmed.hassan", "Reset-temp-2026"))[1]["must_change_password"] is True

            # safety rules: no self-lockout, always one active admin
            assert (await s.call("PUT", "/api/users/admin", admin, {"role": "viewer"}))[1]["error"]["code"] == "SELF"
            assert (await s.call("PUT", "/api/users/admin", admin, {"active": False}))[1]["error"]["code"] == "SELF"
            assert (await s.call("DELETE", "/api/users/admin", admin))[1]["error"]["code"] == "SELF"
            await s.call("PUT", "/api/users/ahmed.hassan", admin, {"role": "admin"})
            _, res = await s.login("ahmed.hassan", "Reset-temp-2026")
            await s.call("POST", "/api/me/password", res["token"], {"current": "Reset-temp-2026", "new": "Second-admin-26"})
            second = (await s.login("ahmed.hassan", "Second-admin-26"))[1]["token"]
            assert (await s.call("PUT", "/api/users/admin", second, {"role": "viewer"}))[0] == 200      # another admin may
            status, res = await s.call("PUT", "/api/users/ahmed.hassan", admin, {"role": "viewer"})
            assert status == 403                                                       # admin is a viewer now
            assert (await s.call("DELETE", "/api/users/admin", second))[0] == 204
            assert (await s.call("DELETE", "/api/users/nobody", second))[0] == 404

            # sign out ends the session; static API tokens still work for machines
            assert (await s.call("POST", "/api/logout", second))[0] == 204
            assert (await s.call("GET", "/api/me", second))[0] == 401
            assert (await s.call("GET", "/api/me", "svc-token"))[1] == {"user": "scada-gw", "role": "viewer", "kind": "token"}

            # audit log: who did what, never a password
            audit = await s.store.run(s.store.audit_log, 200)
            actions = {a["action"] for a in audit}
            assert {"login", "login_failed", "create_user", "update_user", "reset_password", "change_password",
                    "delete_user", "logout"} <= actions
            text = json.dumps(audit)
            for pw in ("Temp-pass-2026", "Own-secret-2026", "Reset-temp-2026", ADMIN_PW):
                assert pw not in text
    asyncio.run(run())


def test_lockout_after_five_wrong_passwords():
    async def run():
        async with Stack() as s:
            for _ in range(5):
                assert (await s.login("admin", "wrong-password-1"))[0] == 401
            status, res = await s.login("admin", ADMIN_PW)                            # right password, but locked
            assert status == 429 and res["error"]["code"] == "LOCKED"
            assert (await s.login("nobody", "wrong-password-1"))[0] == 401           # unknown users answer the same
    asyncio.run(run())


def test_screens_sign_in_admin_adds_user_who_must_change_password():
    pw = pytest.importorskip("playwright.async_api")

    async def run():
        async with Stack() as s, pw.async_playwright() as p:
            browser = await p.chromium.launch()
            pg = await browser.new_page(viewport={"width": 1366, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            await pg.goto(s.base + "/")
            await pg.wait_for_selector("#login:not([hidden])")
            await pg.wait_for_function("document.getElementById('login-hint').hidden === false")   # demo hint fills the form
            await pg.fill("#login-pass", "wrong-password-1")
            await pg.click("#login-btn")
            await pg.wait_for_selector("#login-error:not([hidden])")
            assert "not correct" in await pg.locator("#login-error").inner_text()
            await pg.fill("#login-user", "admin")
            await pg.fill("#login-pass", ADMIN_PW)
            await pg.click("#login-btn")
            await pg.wait_for_selector("#login", state="hidden")
            assert await pg.locator("#tab-admin").is_visible()
            assert "Khaled" in await pg.locator("#user").inner_text()

            # Admin tab: add a viewer
            await pg.click("#tab-admin")
            await pg.wait_for_selector("#users-table")
            await pg.click("#add-user")
            await pg.fill("#u-name", "Control Room")
            await pg.fill("#u-user", "control.room")
            await pg.select_option("#u-role", "viewer")
            await pg.fill("#u-pass", "Temporary-2026")
            await pg.click("#u-create")
            await pg.wait_for_selector("tr[data-user='control.room']")
            assert "new password due" in (await pg.locator("tr[data-user='control.room']").inner_text()).lower()
            assert "Added user" in await pg.locator("#audit-table").inner_text()

            # sign out; the new user signs in, must choose a password, and has no Admin tab
            await pg.click("#signout")
            await pg.wait_for_selector("#login:not([hidden])")
            await pg.fill("#login-user", "control.room")
            await pg.fill("#login-pass", "Temporary-2026")
            await pg.click("#login-btn")
            await pg.wait_for_selector("#pw-modal:not([hidden])")
            assert await pg.locator("#pw-cancel").is_hidden()
            await pg.fill("#pw-current", "Temporary-2026")
            await pg.fill("#pw-new", "My-own-pass-26")
            await pg.fill("#pw-confirm", "My-own-pass-26")
            await pg.click("#pw-save")
            await pg.wait_for_selector("#pw-modal", state="hidden")
            await pg.wait_for_selector("#live[data-state='live']", timeout=10000)
            assert await pg.locator("#tab-admin").is_hidden()
            assert "viewer" in await pg.locator("#user").inner_text()
            assert errors == []
            await browser.close()
    asyncio.run(run())


def test_accounts_on_postgres():
    import os
    dsn = os.environ.get("OPCARE_TEST_PG_DSN")
    if not dsn:
        pytest.skip("set OPCARE_TEST_PG_DSN (CI runs this against PostgreSQL + TimescaleDB)")
    pytest.importorskip("psycopg")

    async def run():
        store = Store(dsn)
        try:
            await store.run(store._x, "DELETE FROM sessions")
            await store.run(store._x, "DELETE FROM users")
            acc = Accounts(store)
            assert await acc.ensure_admin("admin", ADMIN_PW)
            res = await acc.login("admin", ADMIN_PW)
            assert (await acc.session(res["token"]))["role"] == "admin"
            await acc.create_user("admin", {"username": "pg.user", "name": "PG", "role": "viewer", "password": "Postgres-pass-1"})
            await acc.update_user("admin", "pg.user", {"active": False})
            assert [u["active"] for u in await acc.list_users()] == [True, False]
            assert await store.run(store.count_admins) == 1
            await acc.delete_user("admin", "pg.user")
            await acc.logout(res["token"])
            assert await acc.session(res["token"]) is None
        finally:
            store.close()
    asyncio.run(run())
