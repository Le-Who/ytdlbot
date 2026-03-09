import time
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = ["TokenBucketLimiter", "RedisTokenBucketLimiter", "LimiterRegistry"]


class AsyncLimiter(Protocol):
    async def allow(self, key: str, cost: float = 1.0) -> bool: ...


@dataclass
class Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter(AsyncLimiter):
    def __init__(self, capacity: float, refill_rate: float, burst: float | None = None):
        self.capacity = burst if burst is not None else capacity
        self.refill_rate = refill_rate
        self._buckets: dict[str, Bucket] = {}
        self._max_idle_sec = max(300.0, 2 * self.capacity / self.refill_rate)
        self._last_prune = time.monotonic()
        self._prune_interval = 600.0  # prune at most every 10 min

    async def allow(self, key: str, cost: float = 1.0) -> bool:
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
            self._maybe_prune(now)
            return True
        self._maybe_prune(now)
        return False

    def _maybe_prune(self, now: float) -> None:
        if now - self._last_prune < self._prune_interval:
            return
        self._last_prune = now
        stale_keys = [
            k
            for k, b in self._buckets.items()
            if now - b.updated_at > self._max_idle_sec
        ]
        for k in stale_keys:
            del self._buckets[k]


class RedisTokenBucketLimiter(AsyncLimiter):
    # Lua script for atomic token bucket evaluation
    LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local ttl = math.ceil(capacity / refill_rate) * 2

local current = redis.call('HGETALL', key)
local tokens = capacity
local updated_at = now

if #current > 0 then
    local data = {}
    for i = 1, #current, 2 do
        data[current[i]] = tonumber(current[i+1])
    end
    tokens = data["tokens"]
    updated_at = data["updated_at"]
    
    local elapsed = now - updated_at
    if elapsed > 0 then
        tokens = math.min(capacity, tokens + elapsed * refill_rate)
        updated_at = now
    end
end

if tokens >= cost then
    tokens = tokens - cost
    redis.call('HMSET', key, 'tokens', tokens, 'updated_at', updated_at)
    redis.call('EXPIRE', key, ttl)
    return 1
else
    redis.call('HMSET', key, 'tokens', tokens, 'updated_at', updated_at)
    redis.call('EXPIRE', key, ttl)
    return 0
end
"""

    def __init__(
        self,
        redis_client: Any,
        capacity: float,
        refill_rate: float,
        burst: float | None = None,
    ):
        self.redis = redis_client
        self.capacity = burst if burst is not None else capacity
        self.refill_rate = refill_rate
        # Register script for faster execution (EVALSHA)
        self._script = self.redis.register_script(self.LUA_SCRIPT)

    async def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.time()
        try:
            result = await self._script(
                keys=[f"ratelimit:{key}"],
                args=[self.capacity, self.refill_rate, cost, now],
            )
            return bool(result)
        except Exception:
            # Fallback to allow if redis is down to prevent full outage
            return True


class LimiterRegistry:
    def __init__(
        self,
        user: AsyncLimiter,
        chat: AsyncLimiter,
        ip: AsyncLimiter,
        token: AsyncLimiter,
    ):
        self.user = user
        self.chat = chat
        self.ip = ip
        self.token = token

    async def allow_user(self, user_id: int | None) -> bool:
        if user_id is None:
            return True
        return await self.user.allow(f"u:{user_id}")

    async def allow_chat(self, chat_id: int | None) -> bool:
        if chat_id is None:
            return True
        return await self.chat.allow(f"c:{chat_id}")

    async def allow_ip(self, ip: str) -> bool:
        return await self.ip.allow(f"ip:{ip}")

    async def allow_token(self, token: str) -> bool:
        return await self.token.allow(f"t:{token}")
