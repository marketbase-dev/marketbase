"""A cache read must not leave the connection idle in transaction.

Neon keeps a database awake for as long as a connection sits inside an open
transaction. api_cache.get ends the transaction their own SELECT
opened, and leave a caller's pending transaction alone. No database needed.

Run: python3 tools/test_cache_read_releases_transaction.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_cache


class _Info:
    host, dbname = "h", "d"
    def __init__(self): self.transaction_status = 0


class _Cur:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, args=None):
        self.conn.info.transaction_status = 2      # INTRANS, as a real SELECT does
        self.conn.statements.append(sql)
    def fetchone(self): return None
    def fetchall(self): return []


class _Conn:
    def __init__(self):
        self.info, self.commits, self.rollbacks, self.statements = _Info(), 0, 0, []
    def cursor(self): return _Cur(self)
    def commit(self): self.commits += 1; self.info.transaction_status = 0
    def rollback(self): self.rollbacks += 1; self.info.transaction_status = 0


def test_read_on_idle_connection_ends_its_transaction():
    c = _Conn()
    assert api_cache.get(c, "x", "/y", {"a": 1}) is None
    assert c.info.transaction_status == 0, "left idle in transaction"
    assert c.commits == 1 and c.rollbacks == 0


def test_read_inside_callers_transaction_is_left_alone():
    c = _Conn()
    c.info.transaction_status = 2                  # caller has work pending
    api_cache.get(c, "x", "/y", {"a": 1})
    assert c.commits == 0 and c.rollbacks == 0, "touched the caller's transaction"
    assert c.info.transaction_status == 2


def test_cached_call_is_idle_while_the_vendor_is_called():
    c = _Conn()
    seen = {}
    def fetch():
        seen["status"] = c.info.transaction_status
        return None                                # a failure: nothing is written
    api_cache.cached_call(c, "x", "/y", {"a": 1}, fetch)
    assert seen["status"] == 0, "vendor call ran inside an open transaction"


def test_connection_without_status_is_left_alone():
    c = _Conn()
    del c.info.transaction_status
    api_cache.get(c, "x", "/y", {})
    assert c.commits == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
