"""Read migrated reports with a revocable owner session, never permit inference."""
import argparse
import json
from pathlib import Path
import sys

import requests
from verify_backend_existing_history import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--phase4", action="store_true", help="Also verify remote report transport and readiness")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    private = project / ".runtime" / "phase3"
    credentials = json.loads((private / "owner_credentials.json").read_text(encoding="utf-8-sig"))
    base = args.base_url
    session = requests.Session()
    session.trust_env = False
    response = session.post(base + "/api/v1/auth/login", json=credentials, timeout=15, allow_redirects=False)
    if response.status_code != 200:
        raise AssertionError("Owner login failed: status=" + str(response.status_code))
    token = response.json()["access_token"]
    try:
        result = verify(base, project, queued=True, token=token,
                        baseline_path=private / "history_before.json",
                        output_path=args.output or private / "existing_history_acceptance.json")
        if args.phase4:
            from backend.client import JobApiClient
            from backend.ui_history import decode_view
            from core.analysis_agent import report_fingerprint
            client = JobApiClient(base, token)
            ready = session.get(base + "/readyz", timeout=15)
            assert ready.status_code == 200 and ready.json()["status"] == "ready"
            assert ready.json()["model_checked"] is False
            status = client.status()
            assert isinstance(status, dict)
            views = 0
            for record in client.list_batches(limit=100)["items"]:
                view = decode_view(client.view_batch(record["id"]))
                assert report_fingerprint(view["report"]) == record["report_fingerprint"]
                assert len(view["report"]["detail"]) == record["total_rows"]
                views += 1
            metrics = session.get(base + "/api/v1/operations/metrics",
                                  headers={"Authorization": "Bearer " + token}, timeout=15)
            assert metrics.status_code == 200
            assert metrics.json()["daily_budget"]["scope"] == "deployment"
            unauthorized = session.get(base + "/api/v1/operations/metrics", timeout=15)
            assert unauthorized.status_code == 401
            result.update(remote_report_views=views, readyz=ready.json(),
                          protected_metrics_checked=True, remote_status_checked=True)
            (args.output or private / "existing_history_acceptance.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        logged_out = session.post(base + "/api/v1/auth/logout", headers={"Authorization": "Bearer " + token}, timeout=15)
        if logged_out.status_code != 204:
            raise AssertionError("Acceptance session revocation failed")
        session.close()


if __name__ == "__main__":
    main()
