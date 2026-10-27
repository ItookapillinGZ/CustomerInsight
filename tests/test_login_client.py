"""Login errors describe authentication without exposing response contents."""
import json
import unittest

import httpx

from backend.client import JobApiClient, JobClientError


class LoginClientTests(unittest.TestCase):
    def test_login_errors_have_specific_safe_messages_and_are_never_retried(self):
        expected = {
            401: "用户名或密码不正确，请使用已创建的账户登录。",
            422: "用户名或密码字段无效，请核对输入格式。",
            429: "登录尝试过于频繁，请稍后再试。",
        }
        for status, message in expected.items():
            with self.subTest(status=status):
                requests = []
                def respond(request):
                    requests.append(request)
                    return httpx.Response(status, json={"error": {
                        "message": "PRIVATE_SERVER_USER_EXISTS PRIVATE_PASSWORD PRIVATE_SESSION"}})
                client = JobApiClient("http://api:8000", "PRIVATE_SESSION",
                                      transport=httpx.MockTransport(respond))
                with self.assertRaises(JobClientError) as raised:
                    client.login("PRIVATE_USERNAME", "PRIVATE_PASSWORD")
                self.assertEqual(str(raised.exception), message)
                self.assertEqual(raised.exception.status_code, status)
                self.assertNotIn("PRIVATE", str(raised.exception))
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].url.path, "/api/v1/auth/login")

    def test_known_and_unknown_username_receive_the_same_login_error(self):
        calls = []
        def respond(request):
            calls.append(request)
            username = json.loads(request.content)["username"]
            detail = "user exists but password failed" if username == "existing" else "username does not exist"
            return httpx.Response(401, json={"error": {"message": detail}})
        client = JobApiClient("http://api:8000", "unused", transport=httpx.MockTransport(respond))
        messages = []
        for username in ("existing", "unknown"):
            with self.assertRaises(JobClientError) as raised:
                client.login(username, "wrong-password")
            messages.append(str(raised.exception))
        self.assertEqual(messages[0], messages[1])
        self.assertEqual(len(calls), 2)

    def test_login_transport_failure_is_not_retried_or_echoed(self):
        requests = []
        def fail(request):
            requests.append(request)
            raise httpx.ReadTimeout("PRIVATE_USERNAME PRIVATE_PASSWORD PRIVATE_SESSION", request=request)
        client = JobApiClient("http://api:8000", "PRIVATE_SESSION", transport=httpx.MockTransport(fail))
        with self.assertRaises(JobClientError) as raised:
            client.login("PRIVATE_USERNAME", "PRIVATE_PASSWORD")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertIsNone(raised.exception.status_code)

    def test_non_login_error_messages_keep_their_existing_meaning(self):
        for status, action, expected in (
            (401, lambda client: client.me(), "登录信息无效或已过期，请重新登录。"),
            (422, lambda client: client.create_job({}, idempotency_key="a" * 32), "表格选列或任务参数无效，请重新核对。"),
            (429, lambda client: client.create_job({}, idempotency_key="a" * 32), "当前分析额度或并发已满，请稍后查看任务状态。"),
        ):
            with self.subTest(status=status):
                client = JobApiClient("http://api:8000", "unused",
                    transport=httpx.MockTransport(lambda request: httpx.Response(status, text="PRIVATE")))
                with self.assertRaises(JobClientError) as raised:
                    action(client)
                self.assertEqual(str(raised.exception), expected)


if __name__ == "__main__":
    unittest.main()
