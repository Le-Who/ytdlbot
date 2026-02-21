import time
from dataclasses import dataclass


@dataclass
class Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(self, capacity: float, refill_rate: float, burst: float | None = None):
        self.capacity = burst if burst is not None else capacity
        self.refill_rate = refill_rate
        self._buckets: dict[str, Bucket] = {}

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key)

        if bucket is None:
            bucket = Bucket(tokens=self.capacity, updated_at=now)
            self._buckets[key] = bucket

        elapsed = now - bucket.updated_at
        if elapsed > 0:
            bucket.tokens = min(
                self.capacity, bucket.tokens + elapsed * self.refill_rate
            )
            bucket.updated_at = now

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True
        return False


class LimiterRegistry:
    def __init__(
        self,
        user: TokenBucketLimiter,
        chat: TokenBucketLimiter,
        ip: TokenBucketLimiter,
        token: TokenBucketLimiter,
    ):
        self.user = user
        self.chat = chat
        self.ip = ip
        self.token = token

    def allow_user(self, user_id: int | None) -> bool:
        if user_id is None:
            return True
        return self.user.allow(f"u:{user_id}")

    def allow_chat(self, chat_id: int | None) -> bool:
        if chat_id is None:
            return True
        return self.chat.allow(f"c:{chat_id}")

    def allow_ip(self, ip: str) -> bool:
        return self.ip.allow(f"ip:{ip}")

    def allow_token(self, token: str) -> bool:
        return self.token.allow(f"t:{token}")
