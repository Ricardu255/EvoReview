"""Optional retention for resolved feedback, audit rows and alerts."""


class RetentionStoreMixin:
    def purge_expired_metadata(
        self, before: str, failure_cases: bool = True,
        audit: bool = True, alerts: bool = True,
    ) -> dict:
        counts = {"failure_cases": 0, "audit": 0, "alerts": 0}
        with self._write() as conn:
            if failure_cases:
                cursor = conn.execute(self._sql(
                    "DELETE FROM failure_cases WHERE resolved=? AND created_at<?"
                ), (True, before))
                counts["failure_cases"] = max(0, cursor.rowcount)
            if audit:
                cursor = conn.execute(self._sql(
                    "DELETE FROM audit_log WHERE created_at<?"
                ), (before,))
                counts["audit"] = max(0, cursor.rowcount)
            if alerts:
                # Measure from resolution, not creation: an alert resolved
                # today should survive the full retention window.
                cursor = conn.execute(self._sql(
                    "DELETE FROM alerts WHERE status='resolved' AND updated_at<?"
                ), (before,))
                counts["alerts"] = max(0, cursor.rowcount)
        return counts
