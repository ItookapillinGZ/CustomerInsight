"""Historical comparison survives API transport and authenticated exports."""
import unittest

import httpx

from backend.client import JobApiClient, JobClientError
from backend.ui_history import decode_view
from tests import test_account_api as account_fixtures
from tests.test_review_topics import aspect, report_for


class ComparisonApiTests(unittest.TestCase):
    setUp = account_fixtures.AccountApiTests.setUp
    _drop_schema = account_fixtures.AccountApiTests._drop_schema
    login = account_fixtures.AccountApiTests.login
    headers = account_fixtures.AccountApiTests.headers
    request = account_fixtures.AccountApiTests.request
    error = account_fixtures.AccountApiTests.error

    def save(self, user, *, complete, label):
        service, _ = self.app.state.tenants.for_workspace(user["workspace_id"])
        report = report_for(["保温很好", "包装完整"], [[aspect()],
            [aspect("包装完整", evidence="包装完整")] if complete else []])
        original = "评价\n保温很好\n包装完整\n".encode("utf-8-sig")
        identifier = service.history.save_batch(report, filename=label + ".csv", file_bytes=original,
            mapping={"text_column": "评价", "group_column": None, "date_column": None, "sheet_name": None})
        return identifier

    def test_partial_topics_keep_basic_comparison_in_remote_view_and_html(self):
        old = self.save(self.alice, complete=False, label="previous")
        # Distinct mapping gives a second batch without overwriting the first.
        service, _ = self.app.state.tenants.for_workspace(self.alice["workspace_id"])
        current_report = report_for(["保温很好", "包装完整"], [[aspect()], [aspect("包装完整", evidence="包装完整")]])
        current = service.history.save_batch(current_report, filename="current.csv",
            file_bytes="评价\n保温很好\n包装完整\n\n".encode("utf-8-sig"), mapping={"text_column": "评价"})
        view = self.request("GET", f"/api/v1/batches/{current}/view", params={"comparison_id": old})
        self.assertEqual(view.status_code, 200)
        comparison = decode_view(view.json())["report"]["historical_context"]["comparison"]
        self.assertTrue(comparison["basic_comparable"])
        self.assertFalse(comparison["topics_comparable"])
        self.assertEqual(comparison["topics"], [])
        response = self.request("GET", f"/api/v1/batches/{current}/exports/html", params={"comparison_id": old})
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="batch-comparison"', response.text)
        self.assertIn("情感分布", response.text)
        self.assertIn("主题覆盖不完整", response.text)
        self.assertNotIn('id="batch-comparison"', self.request("GET", f"/api/v1/batches/{current}/exports/html").text)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()
        self.budget.reserve.assert_not_called()

    def test_foreign_or_self_comparison_export_is_rejected(self):
        own = self.save(self.alice, complete=True, label="own")
        foreign = self.save(self.bob, complete=False, label="foreign")
        path = f"/api/v1/batches/{own}/exports/html"
        self.error(self.request("GET", path, params={"comparison_id": foreign}), 404)
        self.error(self.request("GET", path, params={"comparison_id": own}), 422)
        for kind in ("csv", "original"):
            self.error(self.request("GET", f"/api/v1/batches/{own}/exports/{kind}", params={"comparison_id": foreign}), 422)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()


class ComparisonExportClientTests(unittest.TestCase):
    def test_html_query_is_sent_once_and_csv_rejects_before_network(self):
        requests = []
        def serve(request):
            requests.append(request)
            return httpx.Response(200, content=b"offline-report")
        client = JobApiClient("http://api:8000", "offline-token", transport=httpx.MockTransport(serve))
        self.assertEqual(client.export("a" * 32, "html", comparison_id="b" * 32), b"offline-report")
        self.assertEqual(requests[0].url.params["comparison_id"], "b" * 32)
        with self.assertRaises(JobClientError):
            client.export("a" * 32, "csv", comparison_id="b" * 32)
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
