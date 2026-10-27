"""Deployment CLI. Passwords never enter command arguments or stdout."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys

from backend.accounts import AccountService, AccountError
from backend.database import get_engine
from core.history_store import HistoryStoreError


def bootstrap(service, username, credentials_file):
    path = Path(credentials_file)
    existing = service.get_user(username)
    if existing is not None:
        if not path.is_file():
            raise ValueError("部署者账户已存在，请使用原凭据；不会自动重置密码。")
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("username") != existing["username"]:
            raise ValueError("凭据文件与账户不匹配。")
        return {"created": False, "user": existing}
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("username") != username:
            raise ValueError("已有凭据文件属于其他账户。")
        password = saved["password"]
    else:
        password = secrets.token_urlsafe(24)
        # Exclusive creation preserves a previous failed bootstrap's password.
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"username": username, "password": password}, handle, ensure_ascii=False, indent=2)
    user = service.create_user(username, password)
    return {"created": True, "user": user}


def main():
    parser = argparse.ArgumentParser(description="CustomerInsight account administration")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user")
    create.add_argument("username")
    create.add_argument("--password-file", help="File containing only a password; otherwise prompt privately")
    owner = commands.add_parser("bootstrap")
    owner.add_argument("--username", default="owner")
    owner.add_argument("--credentials-file", required=True)
    migrate = commands.add_parser("import-legacy")
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--owner", required=True)
    migrate.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        service = AccountService(get_engine())
        if args.command == "create-user":
            password = Path(args.password_file).read_text(encoding="utf-8").rstrip("\r\n") if args.password_file else getpass.getpass("New account password: ")
            result = service.create_user(args.username, password)
        elif args.command == "bootstrap":
            result = bootstrap(service, args.username, args.credentials_file)
        else:
            from backend.import_legacy import import_history
            from backend.pg_history import PgHistoryStore
            user = service.get_user(args.owner)
            if user is None:
                raise ValueError("请先创建明确的归属账户。")
            result = import_history(args.source, PgHistoryStore(service.engine, workspace_id=user["workspace_id"]),
                                    verify_only=args.verify_only)
        print(json.dumps(result, ensure_ascii=False))
    except (AccountError, HistoryStoreError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # SQLAlchemy/provider exception text can contain connection details.
        print("管理操作未完成，错误类型：" + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
