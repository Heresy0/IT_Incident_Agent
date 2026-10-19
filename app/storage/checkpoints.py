"""Official PostgreSQL saver with the same transaction-level execution fence."""
from contextlib import contextmanager
from langgraph.checkpoint.postgres import PostgresSaver


class FencedPostgresSaver(PostgresSaver):
    def __init__(self, backend):
        super().__init__(backend.pool)
        self.backend = backend

    @contextmanager
    def _cursor(self, *, pipeline=False):
        # Guard and checkpoint writes share this transaction. A stale owner may
        # neither overwrite a checkpoint nor commit pending task writes.
        with self.lock, self.backend.pool.connection() as conn, conn.transaction():
            self.backend.guard(conn)
            with conn.cursor(binary=True) as cursor:
                yield cursor
