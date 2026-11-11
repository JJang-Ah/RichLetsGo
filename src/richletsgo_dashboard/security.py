"""Local-only access, login sessions, CSRF and response headers (M5-01 보안)."""

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

LOOPBACK = "127.0.0.1"
IDLE_SECONDS = 15 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60
MAX_FAILURES = 5
LOCKOUT_SECONDS = 10 * 60
CODE_RECHECK_SECONDS = 30

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def require_loopback(host: str, port: int) -> None:
    if host != LOOPBACK:
        raise ValueError("dashboard binds to 127.0.0.1 only")
    if not 1024 <= port <= 65535:
        raise ValueError("dashboard port must be 1024-65535")


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def same_origin(origin: str | None, port: int) -> bool:
    return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def _digest(code: str) -> bytes:
    return hashlib.sha256(code.encode("utf-8")).digest()


@dataclass
class Session:
    sid: str
    csrf: str
    created: float
    seen: float


@dataclass
class Auth:
    """In-memory sessions: a restart, logout or access-code change revokes them.

    `code` returns the current access code from the credential store (None when unset,
    which disables login). Only a digest of it is kept in memory.

    Sync FastAPI routes run in worker threads, so every state transition (digest
    refresh, verification, issue, revoke, failure count) happens under one lock: a login
    verified against an old code can never issue a session after that code's sessions
    were revoked (PR #64 P2-1).
    """

    code: Callable[[], str | None]
    clock: Callable[[], float]
    sessions: dict[str, Session] = field(default_factory=dict)
    failures: int = 0
    locked_until: float = 0.0
    on_revoke: Callable[[str], None] = lambda _sid: None
    _code_digest: bytes | None = None
    _checked: float = float("-inf")
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def _current_digest(self) -> bytes | None:
        now = self.clock()
        if now - self._checked >= CODE_RECHECK_SECONDS:
            self._checked = now
            value = self.code()
            digest = _digest(value) if value else None
            if digest != self._code_digest:
                self._revoke_all()
                self._code_digest = digest
        return self._code_digest

    def _revoke_all(self) -> None:
        for sid in list(self.sessions):
            self._revoke(sid)

    def _revoke(self, sid: str) -> None:
        if self.sessions.pop(sid, None) is not None:
            self.on_revoke(sid)

    def revoke_all(self) -> None:
        with self._lock:
            self._revoke_all()

    def revoke(self, sid: str) -> None:
        with self._lock:
            self._revoke(sid)

    def locked(self) -> bool:
        with self._lock:
            return self.clock() < self.locked_until

    def login(self, attempt: str, previous: str | None = None) -> Session | None:
        """Issue a new session id on success and revoke the caller's previous one.

        A failed attempt leaves the existing session alone (PR #64 P2-2).
        """
        with self._lock:
            digest = self._current_digest()
            if digest is None or self.clock() < self.locked_until:
                return None
            if not hmac.compare_digest(_digest(attempt), digest):
                self.failures += 1
                if self.failures >= MAX_FAILURES:
                    self.failures = 0
                    self.locked_until = self.clock() + LOCKOUT_SECONDS
                return None
            self.failures = 0
            if previous:
                self._revoke(previous)
            now = self.clock()
            session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now, now)
            self.sessions[session.sid] = session
            return session

    def session(self, sid: str | None) -> Session | None:
        with self._lock:
            self._current_digest()
            if not sid or sid not in self.sessions:
                return None
            session = self.sessions[sid]
            now = self.clock()
            if now - session.seen > IDLE_SECONDS or now - session.created > ABSOLUTE_SECONDS:
                self._revoke(sid)
                return None
            session.seen = now
            return session

    def csrf_ok(self, session: Session, token: str | None) -> bool:
        return token is not None and hmac.compare_digest(session.csrf, token)
