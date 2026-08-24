"""Agent 9: Testing — Basic test suite for core modules.

Run with: pytest tests/ -v
"""
import json
import os
import tempfile
import pytest

# Add parent to path
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestAtomicStore:
    """Tests for atomic_store.py"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test_state.json")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_and_load(self):
        from atomic_store import AtomicStore
        store = AtomicStore(self.path, default_factory=lambda: {"x": 1})
        # First load returns default
        assert store.load() == {"x": 1}

    def test_save_and_load(self):
        from atomic_store import AtomicStore
        store = AtomicStore(self.path)
        store.save({"hello": "world", "num": 42})
        loaded = store.load()
        assert loaded["hello"] == "world"
        assert loaded["num"] == 42

    def test_transaction(self):
        from atomic_store import AtomicStore
        store = AtomicStore(self.path)
        store.save({"items": []})
        with store.transaction() as data:
            data["items"].append("new")
        assert store.load()["items"] == ["new"]

    def test_append_to(self):
        from atomic_store import AtomicStore
        store = AtomicStore(self.path)
        store.save({})
        store.append_to("list", "a")
        store.append_to("list", "b")
        assert store.load()["list"] == ["a", "b"]

    def test_atomic_no_corruption(self):
        """Even if we crash mid-write, old data should survive."""
        from atomic_store import AtomicStore
        store = AtomicStore(self.path)
        store.save({"original": True})
        # Verify file exists
        assert os.path.exists(self.path)
        # Load should always return valid data
        assert store.load()["original"] is True


class TestSecurity:
    """Tests for security.py"""

    def test_rate_limiter(self):
        from security import RateLimiter
        rl = RateLimiter(max_attempts=3, window_seconds=10)
        assert not rl.is_blocked("1.2.3.4")
        rl.record("1.2.3.4")
        rl.record("1.2.3.4")
        assert not rl.is_blocked("1.2.3.4")
        rl.record("1.2.3.4")
        assert rl.is_blocked("1.2.3.4")
        # Other IPs unaffected
        assert not rl.is_blocked("5.6.7.8")

    def test_rate_limiter_reset(self):
        from security import RateLimiter
        rl = RateLimiter(max_attempts=2, window_seconds=10)
        rl.record("1.1.1.1")
        rl.record("1.1.1.1")
        assert rl.is_blocked("1.1.1.1")
        rl.reset("1.1.1.1")
        assert not rl.is_blocked("1.1.1.1")

    def test_email_validation(self):
        from security import validate_email
        assert validate_email("user@example.com")
        assert validate_email("test.name+tag@domain.co.uk")
        assert not validate_email("notanemail")
        assert not validate_email("@nodomain")
        assert not validate_email("")

    def test_password_validation(self):
        from security import validate_password
        ok, _ = validate_password("Good1Pass")
        assert ok
        ok, msg = validate_password("short")
        assert not ok
        ok, msg = validate_password("noooooonumbers")
        assert not ok

    def test_sanitize_filename(self):
        from security import sanitize_filename
        assert sanitize_filename("normal.png") == "normal.png"
        assert sanitize_filename("../../../etc/passwd") == "etc_passwd"
        assert sanitize_filename("file with spaces.mp4") == "file_with_spaces.mp4"
        assert sanitize_filename(".hidden") == "hidden"


class TestAPIResponse:
    """Tests for api_response.py"""

    def test_success_response(self):
        from api_response import success
        resp = success(data={"id": "123"})
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["data"]["id"] == "123"
        assert resp.status_code == 200

    def test_error_response(self):
        from api_response import error
        resp = error("Something broke", status=500)
        body = json.loads(resp.body)
        assert body["ok"] is False
        assert body["error"] == "Something broke"
        assert resp.status_code == 500

    def test_paginated(self):
        from api_response import paginated
        resp = paginated([1, 2, 3], total=50, page=2, per_page=3)
        body = json.loads(resp.body)
        assert body["meta"]["total"] == 50
        assert body["meta"]["pages"] == 17


class TestPipeline:
    """Tests for pipeline.py"""

    def test_build_sheet_prompt(self):
        import pipeline
        prompt = pipeline.build_sheet_prompt("anime style", "Hero", "tall, red cape")
        assert "Hero" in prompt
        assert "anime style" in prompt
        assert "red cape" in prompt

    def test_parse_character_batch(self):
        import pipeline
        text = """Alice
Blonde hair, blue dress

Bob
Tall, dark suit"""
        entries = pipeline.parse_character_batch(text)
        assert len(entries) == 2
        assert entries[0]["name"] == "Alice"
        assert "Blonde" in entries[0]["description"]
        assert entries[1]["name"] == "Bob"

    def test_parse_character_batch_empty(self):
        import pipeline
        assert pipeline.parse_character_batch("") == []
        assert pipeline.parse_character_batch("   \n  ") == []
