from pytest import MonkeyPatch

from robust_rag.core.settings import Settings


def test_settings_defaults_are_local_only() -> None:
    settings = Settings(_env_file=None)

    assert settings.api_host == "127.0.0.1"
    assert settings.redis_url.startswith("redis://127.0.0.1")
    assert settings.mineru_base_url == "https://mineru.net/api/v4"
    assert settings.mineru_token is None
    assert settings.mineru_model_version == "vlm"
    assert settings.mineru_ocr_enabled is True
    assert settings.cleaning_config_version == "stage3-cleaning-v1"
    assert settings.cleaning_near_duplicate_threshold == 0.92
    assert settings.quality_rule_set_version == "stage4-quality-rules-v1"
    assert settings.quality_policy_version == "stage4-quality-policy-v1"
    assert settings.quality_sparse_extraction_min_chars_per_mb == 100
    assert settings.dingo_enabled is False
    assert settings.dingo_llm_enabled is False
    assert settings.chunking_config_version == "stage5-parent-child-v2"
    assert settings.chunking_parent_target_tokens == 1800
    assert settings.chunking_child_overlap_tokens == 64
    assert settings.voyage_embedding_model == "voyage-4"
    assert settings.voyage_embedding_dimension == 1024
    assert settings.voyage_embedding_batch_tokens == 8000
    assert settings.voyage_embedding_rate_limit_rpm == 3
    assert settings.voyage_embedding_rate_limit_tpm == 9000
    assert settings.voyage_embedding_rate_limit_window_seconds == 60
    assert settings.voyage_rerank_model == "rerank-2.5"
    assert settings.opensearch_chunks_index == "rag-chunks-v1"
    assert settings.opensearch_chunks_read_alias == "rag-chunks-read"
    assert settings.retrieval_rrf_rank_constant == 60
    assert settings.retrieval_final_child_top_k == 10
    assert settings.graph_query_timeout_seconds == 3
    assert settings.graph_text_to_cypher_timeout_seconds == 8
    assert settings.graph_extractor_version == "llama-schema-v3"
    assert settings.graph_prompt_version == "stage9-extraction-v3"
    assert settings.graph_llm_reasoning_effort == "none"
    assert settings.graph_llm_max_output_tokens == 8000
    assert settings.graph_llm_max_retries == 3
    assert settings.graph_llm_retry_base_seconds == 2
    assert settings.graph_llm_retry_max_seconds == 15
    assert settings.graph_max_failed_parent_ratio == 0.2
    assert settings.graph_run_stale_seconds == 900
    assert settings.graph_build_max_attempts == 2
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_model == "gpt-5.4"
    assert settings.agentic_rag_enabled is False
    assert settings.agent_graph_version == "stage13-langgraph-agentic-rag-v2"
    assert settings.agent_reasoning_effort == "none"
    assert settings.langfuse_enabled is True
    assert settings.langfuse_base_url == "https://cloud.langfuse.com"
    assert settings.langfuse_sample_rate == 1.0
    assert settings.langfuse_capture_content is False


def test_settings_can_be_overridden(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("API_PORT", "9000")
    monkeypatch.setenv("MINERU_TOKEN", "secret-token")
    monkeypatch.setenv("CLEANING_CONFIG_VERSION", "test-cleaning-v2")
    monkeypatch.setenv("QUALITY_POLICY_VERSION", "test-quality-v2")
    monkeypatch.setenv("DINGO_ENABLED", "true")
    monkeypatch.setenv("CHUNKING_CHILD_TARGET_TOKENS", "420")
    monkeypatch.setenv("AGENTIC_RAG_ENABLED", "true")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "low")

    settings = Settings(_env_file=None)

    assert settings.app_env == "test"
    assert settings.api_port == 9000
    assert settings.mineru_token is not None
    assert settings.mineru_token.get_secret_value() == "secret-token"
    assert settings.cleaning_config_version == "test-cleaning-v2"
    assert settings.quality_policy_version == "test-quality-v2"
    assert settings.dingo_enabled is True
    assert settings.chunking_child_target_tokens == 420
    assert settings.agentic_rag_enabled is True
    assert settings.agent_reasoning_effort == "low"
    assert "secret-token" not in repr(settings)


def test_blank_optional_external_settings_are_treated_as_missing(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "")
    monkeypatch.setenv("VOYAGE_EMBEDDING_PRICE_PER_MILLION_TOKENS", "")
    monkeypatch.setenv("VOYAGE_RERANK_PRICE_PER_MILLION_TOKENS", "")
    monkeypatch.setenv("OPENSEARCH_URL", "")
    monkeypatch.setenv("OPENSEARCH_CA_CERT", "")

    settings = Settings(_env_file=None)

    assert settings.voyage_api_key is None
    assert settings.voyage_embedding_price_per_million_tokens is None
    assert settings.voyage_rerank_price_per_million_tokens is None
    assert settings.opensearch_url is None
    assert settings.opensearch_ca_cert is None
