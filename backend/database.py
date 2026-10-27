"""Shared SQLAlchemy metadata and tenant directories.

Production schema changes are applied by Alembic, never on application startup.
An explicitly supplied SQLite URL is supported only for isolated tests.
"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import re

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError
from sqlalchemy.orm import DeclarativeBase


class DatabaseConfigurationError(ValueError):
    """Configuration errors that do not disclose a database connection string."""


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


def database_enabled() -> bool:
    return bool(os.getenv("APP_DATABASE_URL", "").strip())


@lru_cache(maxsize=16)
def _engine(url: str) -> Engine:
    try:
        parsed = make_url(url)
        backend = parsed.get_backend_name()
        if backend not in {"postgresql", "sqlite"}:
            raise DatabaseConfigurationError("数据库必须使用 PostgreSQL。")
        if backend == "postgresql" and parsed.drivername == "postgresql":
            parsed = parsed.set(drivername="postgresql+psycopg")
        kwargs = {"pool_pre_ping": True, "echo": False, "hide_parameters": True}
        if backend == "postgresql":
            kwargs["connect_args"] = {"connect_timeout": 5}
        else:
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if parsed.database in {None, "", ":memory:"}:
                from sqlalchemy.pool import StaticPool
                kwargs["poolclass"] = StaticPool
        engine = create_engine(parsed, **kwargs)
    except DatabaseConfigurationError:
        raise
    except (ValueError, TypeError, ImportError, ModuleNotFoundError, ArgumentError, NoSuchModuleError):
        raise DatabaseConfigurationError("数据库连接配置无效，请检查部署配置。") from None
    if backend == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
    return engine


def get_engine(url: str | None = None) -> Engine:
    explicit = url is not None
    selected = (url if explicit else os.getenv("APP_DATABASE_URL", "")).strip()
    if not selected:
        raise DatabaseConfigurationError("尚未配置 PostgreSQL 数据库。")
    if not explicit:
        try:
            if make_url(selected).get_backend_name() != "postgresql":
                raise DatabaseConfigurationError("部署数据库必须使用 PostgreSQL。")
        except (ValueError, TypeError, ArgumentError):
            raise DatabaseConfigurationError("数据库连接配置无效，请检查部署配置。") from None
    return _engine(selected)


_WORKSPACE_ID = re.compile(r"[0-9a-f]{32}\Z")


def workspace_root(workspace_id: str, data_dir=None) -> Path:
    """Return a UUID-only private directory and reject symlink traversal.

    The deployment owner controls the parent directory; untrusted requests cannot
    choose filesystem paths. Check before and after mkdir to catch pre-existing
    redirects, including Windows junctions where Python exposes them.
    """
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID.fullmatch(workspace_id):
        raise DatabaseConfigurationError("工作区标识无效。")
    base = Path(data_dir if data_dir is not None else
                os.getenv("APP_DATA_DIR") or Path(".runtime") / "history").absolute()
    target = base / "workspaces" / workspace_id
    for candidate in (base, base / "workspaces", target):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise DatabaseConfigurationError("工作区目录不能使用符号链接。")
    # Parents of a configured root must also be real paths, not an escape hatch.
    for candidate in base.parents:
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise DatabaseConfigurationError("工作区目录不能使用符号链接。")
    target.mkdir(parents=True, exist_ok=True)
    for candidate in (base, base / "workspaces", target):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise DatabaseConfigurationError("工作区目录不能使用符号链接。")
    if target.resolve().parent != (base / "workspaces").resolve():
        raise DatabaseConfigurationError("工作区目录无效。")
    return target.resolve()
