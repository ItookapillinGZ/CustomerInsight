"""Presentation adapter. This module owns no database, files, quota or LLM."""
from core.history_store import _unpack
from backend.client import JobClientError


def decode_view(payload):
    try:
        report = _unpack(payload["packed_report"])
        if not isinstance(report, dict) or "detail" not in report:
            raise ValueError
        return {**payload["metadata"], "report": report}
    except (KeyError, TypeError, ValueError):
        raise JobClientError("报告格式校验失败，请联系管理员。") from None


class RemoteHistory:
    def __init__(self, client):
        self.client = client

    def list_batches(self, limit=50):
        return self.client.list_batches(limit=min(100, limit))["items"]

    def load_batch(self, batch_id):
        return decode_view(self.client.view_batch(batch_id))

    def read_original(self, batch_id):
        return self.client.export(batch_id, "original")

    def load_chat(self, batch_id, comparison_id=None):
        return self.client.chat_history(batch_id, comparison_id)["items"]

    def export_backup(self):
        return self.client.backup()

    def delete_batch(self, batch_id):
        return self.client.delete_batch(batch_id)
