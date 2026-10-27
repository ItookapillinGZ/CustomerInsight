"""Durable attempt budgets and stop signals for explicitly authorized LIVE runs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time

from openai import APIConnectionError, APIStatusError


PHASE3_REQUEST_LIMIT = 180
DEFAULT_BUDGET_PATH = Path(__file__).resolve().parents[1] / "validation" / "phase3_request_budget.json"
_ATOMIC_REPLACE_BACKOFF_SECONDS = (0.01, 0.02, 0.04, 0.08)
STOP_REASONS = {
    "request_limit", "cumulative_budget", "authentication_error",
    "consecutive_service_errors", "interrupted", "configuration_error",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RequestControlStopped(RuntimeError):
    """The call was prevented before entering the SDK transport."""


class PersistentRequestBudget:
    """Reserve before sending. Counts survive process termination and restarts.

    The fixed phase-three ceiling is never increased or reset by this module.
    An exclusive lock file coordinates separate CLI processes; a stale lock
    fails closed and requires manual inspection rather than automatic removal.
    """

    def __init__(self, path: str | Path = DEFAULT_BUDGET_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()

    @contextmanager
    def _locked(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            deadline = time.monotonic() + 10
            descriptor = None
            while descriptor is None:
                try:
                    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    if time.monotonic() >= deadline:
                        raise ValueError("请求预算文件被锁定；请先检查是否有仍在运行的实验。") from None
                    time.sleep(0.05)
            try:
                os.write(descriptor, str(os.getpid()).encode("ascii"))
                yield
            finally:
                os.close(descriptor)
                lock_path.unlink()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "request_limit": PHASE3_REQUEST_LIMIT,
                    "attempts_used": 0, "attempts": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (set(data) != {"schema_version", "request_limit", "attempts_used", "attempts"}
                    or data["schema_version"] != 1 or data["request_limit"] != PHASE3_REQUEST_LIMIT
                    or type(data["attempts_used"]) is not int
                    or not 0 <= data["attempts_used"] <= PHASE3_REQUEST_LIMIT
                    or not isinstance(data["attempts"], list)
                    or len(data["attempts"]) != data["attempts_used"]):
                raise ValueError
            for ordinal, attempt in enumerate(data["attempts"], 1):
                if (set(attempt) != {"ordinal", "run_id", "sample_id", "prompt_version", "sample_attempt", "reserved_at"}
                        or attempt["ordinal"] != ordinal or type(attempt["sample_attempt"]) is not int
                        or attempt["sample_attempt"] < 1
                        or any(not isinstance(attempt[key], str) or not attempt[key] for key in ("run_id", "sample_id", "prompt_version", "reserved_at"))):
                    raise ValueError
            return data
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            raise ValueError("累计请求预算文件无效；为避免超出授权上限，已禁止真实调用。") from None

    def _write(self, data: dict) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows scanners can briefly deny replacement after the handle has
        # closed. Keep both locks and the same fsynced file while retrying only
        # PermissionError; a durable reservation must succeed before any SDK
        # call. Persistent denial still propagates without changing the ledger.
        for attempt in range(len(_ATOMIC_REPLACE_BACKOFF_SECONDS) + 1):
            try:
                temporary.replace(self.path)
                return
            except PermissionError:
                if attempt == len(_ATOMIC_REPLACE_BACKOFF_SECONDS):
                    raise
                time.sleep(_ATOMIC_REPLACE_BACKOFF_SECONDS[attempt])

    def snapshot(self) -> dict:
        with self._locked():
            data = self._read()
            return {"attempts_used": data["attempts_used"], "request_limit": data["request_limit"]}

    def reserve(self, *, run_id: str, sample_id: str, prompt_version: str, sample_attempt: int) -> bool:
        with self._locked():
            data = self._read()
            if data["attempts_used"] >= data["request_limit"]:
                return False
            data["attempts_used"] += 1
            data["attempts"].append({
                "ordinal": data["attempts_used"], "run_id": run_id, "sample_id": sample_id,
                "prompt_version": prompt_version, "sample_attempt": sample_attempt, "reserved_at": _now(),
            })
            self._write(data)
            return True


class RunControl:
    """One invocation's hard cap and circuit breaker, shared across versions."""

    def __init__(self, max_attempts: int = PHASE3_REQUEST_LIMIT, *, budget: PersistentRequestBudget | None = None):
        if type(max_attempts) is not int or not 1 <= max_attempts <= PHASE3_REQUEST_LIMIT:
            raise ValueError("本次请求硬上限必须是 1 到 180 之间的整数。")
        self.max_attempts = max_attempts
        self.budget = budget if budget is not None else PersistentRequestBudget()
        self._lock = threading.RLock()
        self.attempts_used = 0
        self.stop_reason: str | None = None
        self.consecutive_service_errors = 0
        self._run_counts: dict[str, int] = {}

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self.stop_reason is not None

    def stop(self, reason: str) -> None:
        if reason not in STOP_REASONS:
            raise ValueError("非法请求停止原因。")
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = reason

    def before_attempt(self, run_id: str, sample_id: str, prompt_version: str, sample_attempt: int) -> None:
        with self._lock:
            if self.stop_reason:
                raise RequestControlStopped(self.stop_reason)
            if self.attempts_used >= self.max_attempts:
                self.stop("request_limit")
                raise RequestControlStopped(self.stop_reason)
            if not self.budget.reserve(run_id=run_id, sample_id=sample_id, prompt_version=prompt_version, sample_attempt=sample_attempt):
                self.stop("cumulative_budget")
                raise RequestControlStopped(self.stop_reason)
            self.attempts_used += 1
            self._run_counts[run_id] = self._run_counts.get(run_id, 0) + 1

    def response_received(self) -> None:
        with self._lock:
            self.consecutive_service_errors = 0

    def request_failed(self, error: Exception) -> None:
        with self._lock:
            if isinstance(error, APIStatusError) and error.status_code in {401, 403}:
                self.stop("authentication_error")
            elif isinstance(error, APIConnectionError) or (
                isinstance(error, APIStatusError) and (error.status_code in {408, 409, 429} or error.status_code >= 500)
            ):
                self.consecutive_service_errors += 1
                if self.consecutive_service_errors >= 3:
                    self.stop("consecutive_service_errors")
            else:
                self.consecutive_service_errors = 0

    def snapshot(self, run_id: str) -> dict:
        with self._lock:
            cumulative = self.budget.snapshot()
            return {
                "request_attempts_actual": self._run_counts.get(run_id, 0),
                "request_attempts_hard_limit": self.max_attempts,
                "cumulative_request_attempts": cumulative["attempts_used"],
                "cumulative_request_limit": cumulative["request_limit"],
                "stop_reason": self.stop_reason,
            }

    def trace(self, run_id: str, sample_id: str, prompt_version: str) -> "AttemptTrace":
        return AttemptTrace(self, run_id, sample_id, prompt_version)


class AttemptTrace:
    """Per-sample evidence, written by one worker and read after it completes."""

    def __init__(self, control: RunControl, run_id: str, sample_id: str, prompt_version: str):
        self.control, self.run_id, self.sample_id, self.prompt_version = control, run_id, sample_id, prompt_version
        self.token_usage: list[dict | str] = []

    @property
    def attempts(self) -> int:
        return len(self.token_usage)

    @property
    def stopped(self) -> bool:
        return self.control.stopped

    def before_attempt(self) -> None:
        self.control.before_attempt(self.run_id, self.sample_id, self.prompt_version, self.attempts + 1)
        self.token_usage.append("不可用")

    def response_received(self, usage: dict | str) -> None:
        self.token_usage[-1] = usage
        self.control.response_received()

    def request_failed(self, error: Exception) -> None:
        self.control.request_failed(error)


class RunJournal:
    """Append-and-fsync each completed row so interrupted runs retain evidence."""

    def __init__(self, directory: str | Path, run_id: str, start: dict):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{run_id}.journal.jsonl"
        self._lock = threading.Lock()
        # Reusing an existing run ID must never overwrite its evidence.
        with self.path.open("x", encoding="utf-8", newline="\n") as handle:
            self._append(handle, {"event": "started", "at": _now(), **start})

    @staticmethod
    def _append(handle, payload: dict) -> None:
        handle.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    def append(self, row: dict) -> None:
        with self._lock:
            # A Ctrl+C can arrive after writing but before returning to the
            # caller. Repeating this operation must neither duplicate a row nor
            # leave an interrupted final line that makes the journal unreadable.
            data = self.path.read_bytes()
            good_offset = 0
            records = []
            lines = data.splitlines(keepends=True)
            for index, line in enumerate(lines):
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeError, ValueError):
                    if index != len(lines) - 1:
                        raise ValueError("逐条运行证据文件损坏，已停止保存。") from None
                    with self.path.open("r+b") as handle:
                        handle.truncate(good_offset)
                        handle.flush()
                        os.fsync(handle.fileno())
                    break
                records.append(record)
                good_offset += len(line)
            if any(record.get("event") == "prediction" and record.get("prediction", {}).get("sample_id") == row["sample_id"] for record in records):
                return
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                if records and good_offset and not self.path.read_bytes().endswith(b"\n"):
                    handle.write("\n")
                self._append(handle, {"event": "prediction", "at": _now(), "prediction": row})

    def finish(self, metadata: dict) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                self._append(handle, {"event": "finished", "at": _now(), "metadata": metadata})
