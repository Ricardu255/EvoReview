"""Recoverable webhook claims; unfinished deliveries retain their task identity."""
import time

from .sql import SqlStoreMixin
from ....time_utils import utc_now


class WebhookStoreMixin(SqlStoreMixin):
    def claim_webhook(self, delivery_id, tenant_id, event_type, payload_sha256, claim_token="", lease_seconds=300):
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        now = time.time()
        with self._transaction(True, "webhook:" + delivery_id) as conn:
            inserted = conn.execute(self._sql(
                "INSERT INTO webhook_deliveries(delivery_id,tenant_id,event_type,payload_sha256,received_at,"
                "status,claim_token,lease_until) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(delivery_id) DO NOTHING"
            ), (delivery_id, tenant_id, event_type, payload_sha256, utc_now(), "processing", claim_token, now + lease_seconds))
            if inserted.rowcount == 1:
                return True
            row = conn.execute(self._sql(
                "SELECT * FROM webhook_deliveries WHERE delivery_id=?"
            ), (delivery_id,)).fetchone()
            if row["payload_sha256"] != payload_sha256 or row["tenant_id"] != tenant_id:
                raise ValueError("delivery id was already used with a different payload or tenant")
            if row["status"] == "completed" or row["lease_until"] > now:
                return False
            return conn.execute(self._sql(
                "UPDATE webhook_deliveries SET status='processing',claim_token=?,lease_until=? "
                "WHERE delivery_id=? AND status<>'completed' AND lease_until<=?"
            ), (claim_token, now + lease_seconds, delivery_id, now)).rowcount == 1

    def bind_webhook_task(self, delivery_id, task_id, claim_token):
        with self._transaction(True) as conn:
            if conn.execute(self._sql(
                "UPDATE webhook_deliveries SET task_id=? WHERE delivery_id=? AND claim_token=? AND status='processing'"
            ), (task_id, delivery_id, claim_token)).rowcount != 1:
                raise RuntimeError("webhook claim is no longer owned by this request")

    def release_webhook(self, delivery_id, claim_token):
        with self._transaction(True) as conn:
            conn.execute(self._sql(
                "UPDATE webhook_deliveries SET status='pending',lease_until=0 "
                "WHERE delivery_id=? AND claim_token=? AND status='processing'"
            ), (delivery_id, claim_token))

    def complete_webhook(self, delivery_id, task_id, claim_token=None):
        query = "UPDATE webhook_deliveries SET task_id=?,status='completed',lease_until=0 WHERE delivery_id=?"
        params = [task_id, delivery_id]
        if claim_token is not None:
            query += " AND claim_token=? AND status='processing'"
            params.append(claim_token)
        with self._transaction(True) as conn:
            if conn.execute(self._sql(query), params).rowcount != 1:
                raise RuntimeError("webhook claim is no longer owned by this request")

    def get_webhook(self, delivery_id):
        with self._transaction() as conn:
            row = conn.execute(self._sql(
                "SELECT * FROM webhook_deliveries WHERE delivery_id=?"
            ), (delivery_id,)).fetchone()
        value = dict(row) if row else None
        if value and hasattr(value["received_at"], "isoformat"):
            value["received_at"] = value["received_at"].isoformat()
        return value
