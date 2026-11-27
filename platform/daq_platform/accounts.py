"""User accounts and sign-in for the platform's web interface and API.

- Passwords are stored only as salted scrypt hashes (RFC 7914, Python standard library).
- Signing in returns a random session token; the database keeps only its SHA-256, so a stolen database
  does not give working sessions. Sessions last 12 hours and are extended while in use.
- Five wrong passwords for one user name lock that name for 5 minutes (answered with 429).
- Rules that keep the platform manageable: there is always at least one active admin, and admins cannot
  disable, demote or delete themselves.
- Static API tokens (OPCARE_API_TOKENS_FILE, tests, the demo) still work for machines and scripts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

from .store import Store, now_ms

ROLES = {"viewer": 1, "operator": 2, "engineer": 3, "admin": 4}
ROLE_TEXT = {"viewer": "Sees live values, history and events",
             "operator": "Same as viewer today; will acknowledge alarms when alarms are added",
             "engineer": "Operator + adds and changes data sources",
             "admin": "Engineer + deletes sources and manages users"}
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")
SESSION_MS = 12 * 3600 * 1000
EXTEND_WHEN_LEFT_MS = 6 * 3600 * 1000
MAX_FAILURES, LOCK_S = 5, 300
_N, _R, _P = 2 ** 14, 8, 1


class AccountError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want))
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


def password_problem(password, username: str = "") -> str | None:
    if not isinstance(password, str) or len(password) < 10:
        return "the password needs at least 10 characters"
    if len(password) > 200:
        return "the password is too long (200 characters at most)"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "the password needs letters and at least one digit"
    if username and username.lower() in password.lower():
        return "the password must not contain the user name"
    return None


_DUMMY_HASH = hash_password("not-a-real-password-0")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ accounts service
class Accounts:
    def __init__(self, store: Store):
        self.store = store
        self._failures: dict[str, list[float]] = {}

    async def ensure_admin(self, username: str, password: str, name: str = "Administrator") -> bool:
        """First start: create the first admin if there are no users at all. Returns True if created."""
        if await self.store.run(self.store.count_users):
            return False
        await self.store.run(self.store.create_user, username, name, None, "admin", hash_password(password), False)
        return True

    # ---------------------------------------------------------- sign in / out
    def _locked(self, username: str) -> bool:
        recent = [t for t in self._failures.get(username, []) if time.monotonic() - t < LOCK_S]
        self._failures[username] = recent
        return len(recent) >= MAX_FAILURES

    async def login(self, username, password) -> dict:
        username = str(username or "").strip().lower()
        if self._locked(username):
            raise AccountError(429, "LOCKED", f"too many wrong passwords; try again in {LOCK_S // 60} minutes")
        user = await self.store.run(self.store.get_user, username) if USERNAME.fullmatch(username) else None
        ok = verify_password(str(password or ""), user["password_hash"] if user else _DUMMY_HASH)  # same time either way
        if not (user and ok and user["active"]):
            self._failures.setdefault(username, []).append(time.monotonic())
            await self.store.run(self.store.audit, username or "?", "login_failed", username or "?", "")
            raise AccountError(401, "LOGIN_FAILED", "user name or password is not correct")
        self._failures.pop(username, None)
        token = secrets.token_urlsafe(32)
        expires = now_ms() + SESSION_MS
        await self.store.run(self.store.create_session, token_hash(token), username, expires)
        await self.store.run(self.store.touch_login, username)
        await self.store.run(self.store.audit, username, "login", username, "")
        user.pop("password_hash")
        return {"token": token, "expires_ms": expires, **self.me(user)}

    async def session(self, token: str) -> dict | None:
        """The signed-in user for a session token, or None. Extends sessions that are in use."""
        if not token:
            return None
        h = token_hash(token)
        found = await self.store.run(self.store.get_session, h)
        if not found:
            return None
        user, expires = found
        if expires - now_ms() < EXTEND_WHEN_LEFT_MS:
            await self.store.run(self.store.extend_session, h, now_ms() + SESSION_MS)
        return user

    async def logout(self, token: str) -> None:
        await self.store.run(self.store.delete_session, token_hash(token))

    @staticmethod
    def me(user: dict) -> dict:
        return {"user": user["username"], "name": user["name"], "role": user["role"],
                "must_change_password": user["must_change_password"], "kind": "session"}

    async def change_own_password(self, user: dict, token: str, current, new) -> None:
        full = await self.store.run(self.store.get_user, user["username"])
        if not full or not verify_password(str(current or ""), full["password_hash"]):
            raise AccountError(400, "BAD_PASSWORD", "the current password is not correct")
        problem = password_problem(new, user["username"])
        if problem:
            raise AccountError(400, "WEAK_PASSWORD", problem)
        if verify_password(new, full["password_hash"]):
            raise AccountError(400, "WEAK_PASSWORD", "choose a password different from the current one")
        await self.store.run(self.store.set_password, user["username"], hash_password(new), False)
        await self.store.run(self.store.delete_user_sessions, user["username"], token_hash(token))  # sign out elsewhere
        await self.store.run(self.store.audit, user["username"], "change_password", user["username"], "")

    # ---------------------------------------------------------- user management (admins)
    async def list_users(self) -> list[dict]:
        return await self.store.run(self.store.list_users)

    async def create_user(self, admin: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise AccountError(400, "BAD_REQUEST", "body must be a JSON object")
        username = str(body.get("username") or "").strip().lower()
        if not USERNAME.fullmatch(username):
            raise AccountError(400, "BAD_REQUEST", "user name: 2-32 lower case letters, digits, dot, dash or underscore")
        name = str(body.get("name") or "").strip()[:100] or username
        email = (str(body.get("email") or "").strip()[:200]) or None
        role = body.get("role")
        if role not in ROLES:
            raise AccountError(400, "BAD_REQUEST", f"role must be one of {', '.join(ROLES)}")
        problem = password_problem(body.get("password"), username)
        if problem:
            raise AccountError(400, "WEAK_PASSWORD", problem)
        if await self.store.run(self.store.get_user, username):
            raise AccountError(409, "EXISTS", f"user {username} already exists")
        must_change = body.get("must_change_password", True) is not False
        await self.store.run(self.store.create_user, username, name, email, role, hash_password(body["password"]), must_change)
        await self.store.run(self.store.audit, admin, "create_user", username, f"role {role}")
        return await self._public(username)

    async def update_user(self, admin: str, username: str, body: dict) -> dict:
        user = await self._existing(username)
        if not isinstance(body, dict):
            raise AccountError(400, "BAD_REQUEST", "body must be a JSON object")
        role = body.get("role", user["role"])
        active = bool(body.get("active", user["active"]))
        if role not in ROLES:
            raise AccountError(400, "BAD_REQUEST", f"role must be one of {', '.join(ROLES)}")
        if username == admin and (role != "admin" or not active):
            raise AccountError(400, "SELF", "you cannot remove your own admin role or disable yourself")
        if user["role"] == "admin" and user["active"] and (role != "admin" or not active):
            await self._keep_an_admin()
        name = str(body.get("name", user["name"]) or "").strip()[:100] or username
        email = (str(body.get("email", user["email"]) or "").strip()[:200]) or None
        await self.store.run(self.store.update_user, username, name, email, role, active)
        if not active:
            await self.store.run(self.store.delete_user_sessions, username, None)
        changes = [f"{k} {v}" for k, v in (("role", role), ("active", active)) if user[k] != v]
        await self.store.run(self.store.audit, admin, "update_user", username, ", ".join(changes) or "details")
        return await self._public(username)

    async def reset_password(self, admin: str, username: str, password) -> None:
        await self._existing(username)
        problem = password_problem(password, username)
        if problem:
            raise AccountError(400, "WEAK_PASSWORD", problem)
        await self.store.run(self.store.set_password, username, hash_password(password), username != admin)
        await self.store.run(self.store.delete_user_sessions, username, None)
        await self.store.run(self.store.audit, admin, "reset_password", username, "")

    async def delete_user(self, admin: str, username: str) -> None:
        user = await self._existing(username)
        if username == admin:
            raise AccountError(400, "SELF", "you cannot delete your own account")
        if user["role"] == "admin" and user["active"]:
            await self._keep_an_admin()
        await self.store.run(self.store.delete_user, username)
        await self.store.run(self.store.audit, admin, "delete_user", username, "")

    async def _existing(self, username: str) -> dict:
        user = await self.store.run(self.store.get_user, username)
        if not user:
            raise AccountError(404, "NOT_FOUND", f"user {username} not found")
        return user

    async def _public(self, username: str) -> dict:
        user = await self.store.run(self.store.get_user, username)
        user.pop("password_hash", None)
        return user

    async def _keep_an_admin(self) -> None:
        if await self.store.run(self.store.count_admins) <= 1:
            raise AccountError(400, "LAST_ADMIN", "the platform must keep at least one active admin")
