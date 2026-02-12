from app.core.limiter import TokenBucketLimiter


def test_token_bucket_consumption_and_refill(monkeypatch):
    t = {"v": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: t["v"])

    limiter = TokenBucketLimiter(capacity=2, refill_rate=1)
    assert limiter.allow("u1")
    assert limiter.allow("u1")
    assert not limiter.allow("u1")

    t["v"] = 1.1
    assert limiter.allow("u1")


def test_token_bucket_burst(monkeypatch):
    monkeypatch.setattr("time.monotonic", lambda: 0.0)
    limiter = TokenBucketLimiter(capacity=10, refill_rate=1, burst=3)
    assert limiter.allow("u2")
    assert limiter.allow("u2")
    assert limiter.allow("u2")
    assert not limiter.allow("u2")
