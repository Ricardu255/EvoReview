"""Small SQL dialect and transaction adapter for shared persistence contracts."""
from contextlib import contextmanager, nullcontext


class SqlStoreMixin:
    _postgres_sql = False

    def _sql(self, sql):
        return sql.replace("?", "%s") if self._postgres_sql else sql.replace("::jsonb", "")

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
