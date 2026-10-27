"""Deployment-provisioned users and revocable, hashed bearer sessions.

No public registration endpoint exists. Rate-limit reservations use a database
upsert, so multiple API processes share the same attempt budget.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, delete, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base, get_engine


SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 300
LOGIN_USERNAME_LIMIT = 8
LOGIN_IP_LIMIT = 30
_USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,63}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{64}\Z")
_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
# A fixed dummy hash means startup and invalid usernames never create new hashes.
_DUMMY_HASH = "$argon2id$v=19$m=19456,t=2,p=1$Y3VzdG9tZXJpbnNpZ2h0MQ$3Zd1b55hPkKvul7dqaHsGPAe8nDq+nvRN3D4uDV6hFI"


class AccountError(ValueError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _unauthorized():
    return AccountError(401, "invalid_credentials", "登录信息无效或已过期，请重新登录。")


class Workspace(Base):
    __tablename__ = "app_workspaces"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class User(Base):
    __tablename__ = "app_users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("app_workspaces.id"), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class AccountSession(Base):
    __tablename__ = "app_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (Index("ix_app_sessions_user_id", "user_id"),
                      Index("ix_app_sessions_expires_at", "expires_at"))


class LoginThrottle(Base):
    __tablename__ = "app_login_throttles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[float] = mapped_column(Float, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (Index("ix_app_login_throttles_window_start", "window_start"),)


def _public(row):
    return {name: row[name] for name in ("id", "username", "workspace_id")}


class AccountService:
    def __init__(self, engine: Engine | None = None, clock=None):
        self.engine = engine if engine is not None else get_engine()
        self.clock = clock or time.time

    def create_user(self, username: str, password: str) -> dict:
        name = username.strip().lower() if isinstance(username, str) else ""
        if not _USERNAME.fullmatch(name):
            raise AccountError(422, "invalid_username", "用户名需为 3 至 64 位小写字母、数字、点、下划线或短横线。")
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise AccountError(422, "invalid_password", "密码长度需为 12 至 256 个字符。")
        now = self.clock()
        value = {"id": uuid4().hex, "username": name, "workspace_id": uuid4().hex}
        hashed = _HASHER.hash(password)
        try:
            with self.engine.begin() as connection:
                connection.execute(Workspace.__table__.insert().values(id=value["workspace_id"], created_at=now))
                connection.execute(User.__table__.insert().values(**value, password_hash=hashed, active=True, created_at=now))
        except IntegrityError:
            raise AccountError(409, "username_unavailable", "此用户名不可用。") from None
        return value

    def get_user(self, username: str) -> dict | None:
        name = username.strip().lower() if isinstance(username, str) else ""
        with self.engine.connect() as connection:
            row = connection.execute(select(User.__table__).where(User.username == name)).mappings().first()
        return _public(row) if row else None

    def _reserve_login(self, name: str, client_ip: str, now: float):
        # Reserve the IP first. New/random usernames cannot bypass this cap and
        # cannot create unbounded per-name records after the IP has been blocked.
        ip_key = hashlib.sha256(("ip\0" + str(client_ip)[:256]).encode()).hexdigest()
        user_key = hashlib.sha256(("user\0" + name).encode()).hexdigest()
        table = LoginThrottle.__table__
        from sqlalchemy import case
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif self.engine.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise RuntimeError("Unsupported account database")
        for key, limit in ((ip_key, LOGIN_IP_LIMIT), (user_key, LOGIN_USERNAME_LIMIT)):
            stale = table.c.window_start <= now - LOGIN_WINDOW_SECONDS
            statement = insert(table).values(key=key, window_start=now, attempts=1)
            statement = statement.on_conflict_do_update(index_elements=[table.c.key], set_={
                "window_start": case((stale, now), else_=table.c.window_start),
                "attempts": case((stale, 1), else_=table.c.attempts + 1),
            }).returning(table.c.attempts)
            with self.engine.begin() as connection:
                attempts = connection.execute(statement).scalar_one()
                # Keep retention bounded even on installations without a cron.
                connection.execute(delete(table).where(table.c.window_start < now - 24 * 60 * 60))
            if attempts > limit:
                raise AccountError(429, "login_rate_limited", "登录尝试过于频繁，请稍后重试。")

    def login(self, username: str, password: str, *, client_ip: str = "unknown") -> dict:
        # Invalid syntax still consumes the same two shared rate-limit buckets.
        raw_name = username.strip().lower() if isinstance(username, str) else ""
        name = raw_name[:256]
        now = self.clock()
        self._reserve_login(name, client_ip, now)
        with self.engine.connect() as connection:
            row = connection.execute(select(User.__table__).where(User.username == name)).mappings().first() if _USERNAME.fullmatch(name) else None
        supplied = password if isinstance(password, str) and len(password) <= 256 else ""
        valid = False
        try:
            valid = _HASHER.verify(row["password_hash"] if row else _DUMMY_HASH, supplied)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass
        if not valid or row is None or not row["active"]:
            raise _unauthorized()
        token = secrets.token_urlsafe(48)
        expires = now + SESSION_SECONDS
        # Lock the account row before issuing a session so deactivation cannot
        # race a stale successful password check into a valid new session.
        with self.engine.begin() as connection:
            current = connection.execute(select(User.__table__).where(User.id == row["id"]).with_for_update()).mappings().first()
            if current is None or not current["active"] or current["password_hash"] != row["password_hash"]:
                raise _unauthorized()
            connection.execute(AccountSession.__table__.insert().values(
                token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=row["id"],
                created_at=now, expires_at=expires, revoked_at=None))
            connection.execute(delete(AccountSession).where(AccountSession.expires_at < now - 24 * 60 * 60))
        return {"access_token": token, "token_type": "bearer", "expires_at": expires, "user": _public(row)}

    def authenticate(self, token: str) -> dict:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise _unauthorized()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.engine.connect() as connection:
            row = connection.execute(select(User.__table__).join(
                AccountSession, AccountSession.user_id == User.id).where(
                AccountSession.token_hash == token_hash, AccountSession.revoked_at.is_(None),
                AccountSession.expires_at > self.clock(), User.active.is_(True))).mappings().first()
        if row is None:
            raise _unauthorized()
        return _public(row)

    def logout(self, token: str):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return
        with self.engine.begin() as connection:
            connection.execute(update(AccountSession).where(
                AccountSession.token_hash == hashlib.sha256(token.encode()).hexdigest(),
                AccountSession.revoked_at.is_(None)).values(revoked_at=self.clock()))

    def change_password(self, token: str, new_password: str, *, current_password: str):
        principal = self.authenticate(token)
        if not isinstance(new_password, str) or not 12 <= len(new_password) <= 256:
            raise AccountError(422, "invalid_password", "密码长度需为 12 至 256 个字符。")
        self._reserve_login(principal["username"], "password-change:" + principal["id"], self.clock())
        with self.engine.connect() as connection:
            previous = connection.execute(select(User.password_hash).where(User.id == principal["id"])).scalar_one()
        supplied = current_password if isinstance(current_password, str) and len(current_password) <= 256 else ""
        try:
            valid = _HASHER.verify(previous, supplied)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            valid = False
        if not valid:
            raise _unauthorized()
        password_hash = _HASHER.hash(new_password)
        now = self.clock()
        with self.engine.begin() as connection:
            # The same account lock used at login makes the password update and
            # revocation atomic against concurrent session issuance.
            current = connection.execute(select(User.__table__).where(
                User.id == principal["id"], User.active.is_(True)).with_for_update()).mappings().first()
            session = connection.execute(select(AccountSession.__table__).where(
                AccountSession.token_hash == hashlib.sha256(token.encode()).hexdigest(),
                AccountSession.user_id == principal["id"], AccountSession.revoked_at.is_(None),
                AccountSession.expires_at > now)).first()
            if current is None or session is None or current["password_hash"] != previous:
                raise _unauthorized()
            connection.execute(update(User).where(User.id == principal["id"]).values(password_hash=password_hash))
            connection.execute(update(AccountSession).where(
                AccountSession.user_id == principal["id"], AccountSession.revoked_at.is_(None)).values(revoked_at=now))
