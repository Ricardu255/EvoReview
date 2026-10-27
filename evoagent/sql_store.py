"""Small SQL dialect and transaction adapter for shared persistence contracts."""
from contextlib import contextmanager, nullcontext


class SqlStoreMixin:
    _postgres_sql = False

    def _sql(self, sql):
        return sql.replace("?", "%s") if self._postgres_sql else sql.replace("::jsonb", "")

    def _in_clause(self, values):
        """Return a backend-specific IN clause and its parameter values."""
        values = list(values)
        if not values:
            return "IN (NULL)", []
        if self._postgres_sql:
            return "=ANY(%s)", [values]
        return "IN (%s)" % ",".join("?" for _ in values), values

    def _greatest(self, left: str, right: str) -> str:
        return "GREATEST(%s,%s)" % (left, right) if self._postgres_sql else (
            "MAX(%s,%s)" % (left, right)
        )

    def _acquire_key_lock(self, conn, key: str) -> None:
        if self._postgres_sql:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (key,),
            )

    @contextmanager
    def _write(self):
        """Open a write connection with the backend's process/row lock."""
        lock = getattr(self, "_lock", None)
        with lock if lock is not None else nullcontext():
            with self._connect() as conn:
                yield conn

    @contextmanager
    def _transaction(self, write=False, key=""):
        lock = getattr(self, "_lock", None) if write else None
        with lock if lock is not None else nullcontext():
            with self._connect() as conn:
                if write:
                    if self._postgres_sql and key:
                        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
                    elif not self._postgres_sql:
                        conn.execute("BEGIN IMMEDIATE")
                yield conn
