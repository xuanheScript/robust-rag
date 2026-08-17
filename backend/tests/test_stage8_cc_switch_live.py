"""Opt-in live contract for the configured cc switch gpt-5.6-luna route."""

import json
import os

import pytest

from robust_rag.core.settings import Settings
from robust_rag.generation.provider import CCSwitchResponsesProvider, LLMRequest

pytestmark = [
    pytest.mark.integration_live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_CC_SWITCH_TESTS") != "1",
        reason="set RUN_LIVE_CC_SWITCH_TESTS=1 to allow real model calls",
    ),
]


def test_cc_switch_gpt_5_6_luna_responses_contract() -> None:
    settings = Settings()
    assert settings.llm_model == "gpt-5.6-luna"
    provider = CCSwitchResponsesProvider(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        reasoning_effort=settings.llm_reasoning_effort,
        api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
        timeout_seconds=settings.llm_timeout_seconds,
    )

    non_stream = provider.generate(
        LLMRequest(
            instructions="Return exactly CCSWITCH_OK and nothing else.",
            input=[{"role": "user", "content": "Run the contract check."}],
            max_output_tokens=50,
        )
    )
    assert non_stream.text.strip() == "CCSWITCH_OK"
    assert non_stream.response_id
    assert non_stream.usage.input_tokens is not None
    assert non_stream.usage.output_tokens is not None

    stream = list(
        provider.stream(
            LLMRequest(
                instructions="Return exactly STREAM_OK and nothing else.",
                input=[{"role": "user", "content": "Run the streaming contract check."}],
                max_output_tokens=50,
            )
        )
    )
    assert "".join(event.delta for event in stream if event.type == "text_delta").strip() == (
        "STREAM_OK"
    )
    assert stream[-1].type == "completed"
    assert stream[-1].usage.output_tokens is not None

    multi_turn = provider.generate(
        LLMRequest(
            instructions="Answer the last question using only this conversation.",
            input=[
                {"role": "user", "content": "Remember the code ORBIT-731."},
                {"role": "assistant", "content": "Understood."},
                {"role": "user", "content": "What was the code? Return only the code."},
            ],
            max_output_tokens=50,
        )
    )
    assert multi_turn.text.strip() == "ORBIT-731"

    structured = provider.generate(
        LLMRequest(
            instructions="Return JSON matching the supplied schema.",
            input=[{"role": "user", "content": "Report a successful contract check."}],
            max_output_tokens=100,
            text_format={
                "type": "json_schema",
                "name": "contract_result",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        )
    )
    assert json.loads(structured.text) == {"ok": True}
