import time
from dataclasses import dataclass


@dataclass
class Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(self, capacity: float, refill_rate: float, burst: float | None = None):
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.burst = float(burst if burst is not None else capacity)
        self._buckets: dict[str, Bucket] = {}

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = Bucket(tokens=self.burst, updated_at=now)
            self._buckets[key] = bucket

        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_rate)
        bucket.updated_at = now

        if bucket.tokens < cost:
            return False

        bucket.tokens -= cost
        return True
