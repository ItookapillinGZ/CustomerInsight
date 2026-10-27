"""Read the historical 1.4.1 Agent acceptance without issuing any API calls.

The historical two-call planner/selector protocol was replaced in 1.6.0.
Its saved evidence stays immutable; use test_reasoning_agent.py for the current
protocol. This entry point deliberately has no imports from the Agent runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SUMMARY_PATH = ROOT / "validation" / "agent_live_acceptance" / "summary.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="只读 1.4.1 历史验收；不支持真实调用。")
    parser.add_argument("--live", action="store_true", help="已停用，始终拒绝真实调用")
    args = parser.parse_args()
    if args.live:
        parser.error(
            "旧的两请求 Agent 协议已归档，未发送请求。当前协议请使用 "
            "tools/test_reasoning_agent.py；默认离线，真实验收须先获得调用授权，"
            "再显式传入 --live --max-requests 3（两轮为 --turns 2 --max-requests 6）。"
        )
    payload = {
        "mode": "historical_read_only",
        "protocol_version": "1.4.1",
        "external_requests": 0,
        "current_protocol": "1.6.0 reasoning Agent; see tools/test_reasoning_agent.py",
        "historical_summary_exists": SUMMARY_PATH.exists(),
        "not_current_version_acceptance": True,
    }
    if SUMMARY_PATH.exists():
        content = SUMMARY_PATH.read_bytes()
        saved = json.loads(content)
        payload.update(
            summary_sha256=hashlib.sha256(content).hexdigest(),
            actual_attempts=saved.get("actual_attempts"),
            completed_at=saved.get("completed_at"),
            human_review_status=saved.get("human_review", {}).get("status"),
        )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
