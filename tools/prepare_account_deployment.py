"""Add only a missing PostgreSQL password; never print deployment secrets."""
from pathlib import Path
import secrets
from dotenv import dotenv_values


def prepare(project: Path):
    path = project / ".env"
    if not path.is_file():
        raise ValueError("请先创建并配置 .env，不覆盖已有模型配置。")
    if (dotenv_values(path).get("APP_POSTGRES_PASSWORD") or "").strip():
        return False
    original = path.read_bytes()
    backup = project / ".runtime" / "phase3" / "env_before_accounts.env"
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        backup.write_bytes(original)
    text = original.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    password = secrets.token_urlsafe(36)
    found = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("APP_POSTGRES_PASSWORD="):
            lines[index] = "APP_POSTGRES_PASSWORD=" + password + "\n"
            found = True
    if not found:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.append("APP_POSTGRES_PASSWORD=" + password + "\n")
    path.write_text("".join(lines), encoding="utf-8")
    return True


if __name__ == "__main__":
    changed = prepare(Path(__file__).resolve().parents[1])
    print("已准备 PostgreSQL 凭据；原配置已保留。" if changed else "PostgreSQL 凭据已存在，未改动配置。")
