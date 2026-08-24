"""Agent 9: Basic test suite. Run: pytest tests/ -v"""
import json, os, tempfile, shutil
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

class TestAtomicStore:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "state.json")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load(self):
        from atomic_store import AtomicStore
        s = AtomicStore(self.path)
        s.save({"x": 42})
        assert s.load()["x"] == 42

    def test_transaction(self):
        from atomic_store import AtomicStore
        s = AtomicStore(self.path)
        s.save({"items": []})
        with s.transaction() as data:
            data["items"].append("a")
        assert s.load()["items"] == ["a"]

    def test_default_factory(self):
        from atomic_store import AtomicStore
        s = AtomicStore(self.path, default_factory=lambda: {"default": True})
        assert s.load() == {"default": True}

class TestSecurity:
    def test_rate_limiter(self):
        from security import RateLimiter
        rl = RateLimiter(max_attempts=3, window_seconds=10)
        assert not rl.is_blocked("1.2.3.4")
        rl.record("1.2.3.4")
        rl.record("1.2.3.4")
        rl.record("1.2.3.4")
        assert rl.is_blocked("1.2.3.4")
        assert not rl.is_blocked("5.6.7.8")

    def test_validate_email(self):
        from security import validate_email
        assert validate_email("user@example.com")
        assert not validate_email("notanemail")

    def test_sanitize_filename(self):
        from security import sanitize_filename
        assert sanitize_filename("../../../etc/passwd") == "etc_passwd"
        assert sanitize_filename("normal.png") == "normal.png"

class TestAPIResponse:
    def test_success(self):
        from api_response import success
        resp = success(data={"id": "x"})
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["data"]["id"] == "x"

    def test_error(self):
        from api_response import error
        resp = error("broke", status=500)
        body = json.loads(resp.body)
        assert body["ok"] is False
        assert resp.status_code == 500
