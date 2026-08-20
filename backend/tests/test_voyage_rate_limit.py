from typing import cast
from unittest.mock import MagicMock

import pytest
from redis import Redis
from redis.exceptions import RedisError

from robust_rag.core.settings import Settings
from robust_rag.indexing.rate_limit import (
    NoopVoyageRateLimiter,
    RateLimiterUnavailable,
    RedisVoyageRateLimiter,
    build_voyage_rate_limiter,
)


def test_redis_rate_limiter_reserves_or_returns_shared_window_delay() -> None:
    client = MagicMock()
    client.eval.side_effect = [0, 42_001]
    limiter = RedisVoyageRateLimiter(
        client=cast(Redis, client),
        scope="test-scope",
        requests_per_minute=3,
        tokens_per_minute=9000,
        window_seconds=60,
    )

    assert limiter.reserve(8000) == 0
    assert limiter.reserve(8000) == 43
    assert client.eval.call_count == 2
    first_call = client.eval.call_args_list[0].args
    assert first_call[1:4] == (2, limiter.events_key, limiter.tokens_key)
    assert first_call[4:8] == ("60000", "3", "9000", "8000")


def test_redis_rate_limiter_fails_closed_when_budget_or_redis_is_invalid() -> None:
    client = MagicMock()
    limiter = RedisVoyageRateLimiter(
        client=cast(Redis, client),
        scope="test-scope",
        requests_per_minute=3,
        tokens_per_minute=9000,
        window_seconds=60,
    )

    with pytest.raises(ValueError, match="above the shared"):
        limiter.reserve(9001)

    client.eval.side_effect = RedisError("offline")
    with pytest.raises(RateLimiterUnavailable, match="unavailable"):
        limiter.reserve(100)


def test_rate_limiter_can_be_explicitly_disabled() -> None:
    settings = Settings(_env_file=None).model_copy(
        update={"voyage_embedding_rate_limit_enabled": False}
    )

    limiter = build_voyage_rate_limiter(settings)

    assert isinstance(limiter, NoopVoyageRateLimiter)
    assert limiter.reserve(100) == 0
