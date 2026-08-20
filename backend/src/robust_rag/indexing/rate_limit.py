"""Shared Voyage embedding rate limiting backed by Redis."""

from __future__ import annotations

import hashlib
import math
import uuid
from typing import Protocol, cast

from redis import Redis
from redis.exceptions import RedisError

from robust_rag.core.settings import Settings


class VoyageRateLimiter(Protocol):
    """Reserve one embedding request inside the shared rolling window."""

    def reserve(self, estimated_tokens: int) -> float:
        """Return zero when reserved, otherwise seconds until a safe retry."""


class NoopVoyageRateLimiter:
    def reserve(self, estimated_tokens: int) -> float:
        del estimated_tokens
        return 0.0


class RateLimiterUnavailable(RuntimeError):
    """Raised when the shared limiter cannot safely coordinate requests."""


_RESERVE_SCRIPT = """
local events_key = KEYS[1]
local tokens_key = KEYS[2]
local window_ms = tonumber(ARGV[1])
local request_limit = tonumber(ARGV[2])
local token_limit = tonumber(ARGV[3])
local requested_tokens = tonumber(ARGV[4])
local member = ARGV[5]

local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local cutoff_ms = now_ms - window_ms
local expired = redis.call('ZRANGEBYSCORE', events_key, '-inf', cutoff_ms)
for _, expired_member in ipairs(expired) do
  redis.call('HDEL', tokens_key, expired_member)
end
redis.call('ZREMRANGEBYSCORE', events_key, '-inf', cutoff_ms)

local members = redis.call('ZRANGE', events_key, 0, -1, 'WITHSCORES')
local request_count = #members / 2
local used_tokens = 0
for index = 1, #members, 2 do
  used_tokens = used_tokens + tonumber(redis.call('HGET', tokens_key, members[index]) or '0')
end

if request_count < request_limit and used_tokens + requested_tokens <= token_limit then
  redis.call('ZADD', events_key, now_ms, member)
  redis.call('HSET', tokens_key, member, requested_tokens)
  local ttl_seconds = math.ceil(window_ms / 1000) + 5
  redis.call('EXPIRE', events_key, ttl_seconds)
  redis.call('EXPIRE', tokens_key, ttl_seconds)
  return 0
end

local simulated_count = request_count
local simulated_tokens = used_tokens
for index = 1, #members, 2 do
  simulated_count = simulated_count - 1
  simulated_tokens = simulated_tokens - tonumber(
    redis.call('HGET', tokens_key, members[index]) or '0'
  )
  if simulated_count < request_limit and simulated_tokens + requested_tokens <= token_limit then
    return math.max(1, tonumber(members[index + 1]) + window_ms - now_ms)
  end
end
return window_ms
"""


class RedisVoyageRateLimiter:
    def __init__(
        self,
        *,
        client: Redis,
        scope: str,
        requests_per_minute: int,
        tokens_per_minute: int,
        window_seconds: int,
    ) -> None:
        self.client = client
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        self.window_seconds = window_seconds
        key_scope = f"{{{scope}}}"
        self.events_key = f"robust-rag:voyage:embedding:{key_scope}:events"
        self.tokens_key = f"robust-rag:voyage:embedding:{key_scope}:tokens"

    def reserve(self, estimated_tokens: int) -> float:
        if estimated_tokens < 1:
            raise ValueError("estimated_tokens must be positive")
        if estimated_tokens > self.tokens_per_minute:
            raise ValueError(
                f"Embedding request estimates {estimated_tokens} tokens, above the shared "
                f"{self.tokens_per_minute} TPM budget"
            )
        try:
            wait_ms = cast(
                int,
                self.client.eval(
                    _RESERVE_SCRIPT,
                    2,
                    self.events_key,
                    self.tokens_key,
                    str(self.window_seconds * 1000),
                    str(self.requests_per_minute),
                    str(self.tokens_per_minute),
                    str(estimated_tokens),
                    uuid.uuid4().hex,
                ),
            )
        except RedisError as exc:
            raise RateLimiterUnavailable("Voyage rate limiter is unavailable") from exc
        return math.ceil(wait_ms / 1000) if wait_ms > 0 else 0.0


def build_voyage_rate_limiter(settings: Settings) -> VoyageRateLimiter:
    if not settings.voyage_embedding_rate_limit_enabled:
        return NoopVoyageRateLimiter()
    api_key = settings.voyage_api_key.get_secret_value() if settings.voyage_api_key else "missing"
    scope_seed = f"{settings.voyage_base_url}|{settings.voyage_embedding_model}|{api_key}"
    scope = hashlib.sha256(scope_seed.encode()).hexdigest()[:16]
    return RedisVoyageRateLimiter(
        client=Redis.from_url(settings.redis_url, decode_responses=True),
        scope=scope,
        requests_per_minute=settings.voyage_embedding_rate_limit_rpm,
        tokens_per_minute=settings.voyage_embedding_rate_limit_tpm,
        window_seconds=settings.voyage_embedding_rate_limit_window_seconds,
    )
