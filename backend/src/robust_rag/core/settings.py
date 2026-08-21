"""Typed application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by the API and background workers."""

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = Field(default="Robust RAG", validation_alias="APP_NAME")
    app_env: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="APP_ENV"
    )
    api_host: str = Field(default="127.0.0.1", validation_alias="API_HOST")
    api_port: int = Field(default=8000, validation_alias="API_PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_format: Literal["console", "json"] = Field(default="console", validation_alias="LOG_FORMAT")

    database_url: str = Field(
        default="postgresql+psycopg://localhost:5432/robust_rag",
        validation_alias="DATABASE_URL",
    )
    database_echo: bool = Field(default=False, validation_alias="DATABASE_ECHO")
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", validation_alias="REDIS_URL")
    celery_result_backend: str = Field(
        default="redis://127.0.0.1:6379/1",
        validation_alias="CELERY_RESULT_BACKEND",
    )
    storage_root: Path = Field(default=Path("../data"), validation_alias="STORAGE_ROOT")
    upload_max_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        validation_alias="UPLOAD_MAX_BYTES",
    )
    upload_chunk_bytes: int = Field(
        default=1024 * 1024,
        ge=64 * 1024,
        validation_alias="UPLOAD_CHUNK_BYTES",
    )
    job_recovery_age_seconds: int = Field(
        default=300,
        ge=0,
        validation_alias="JOB_RECOVERY_AGE_SECONDS",
    )
    celery_health_timeout_seconds: float = Field(
        default=1.0, gt=0, le=10, validation_alias="CELERY_HEALTH_TIMEOUT_SECONDS"
    )
    celery_queue_warning_depth: int = Field(
        default=100, ge=1, validation_alias="CELERY_QUEUE_WARNING_DEPTH"
    )
    celery_heartbeat_ttl_seconds: int = Field(
        default=120, ge=30, validation_alias="CELERY_HEARTBEAT_TTL_SECONDS"
    )

    mineru_base_url: str = Field(
        default="https://mineru.net/api/v4", validation_alias="MINERU_BASE_URL"
    )
    mineru_token: SecretStr | None = Field(default=None, validation_alias="MINERU_TOKEN")
    mineru_timeout_seconds: int = Field(
        default=600, ge=1, validation_alias="MINERU_TIMEOUT_SECONDS"
    )
    mineru_poll_interval_seconds: float = Field(
        default=3, ge=0.1, validation_alias="MINERU_POLL_INTERVAL_SECONDS"
    )
    mineru_model_version: Literal["pipeline", "vlm"] = Field(
        default="vlm", validation_alias="MINERU_MODEL_VERSION"
    )
    mineru_ocr_enabled: bool = Field(default=True, validation_alias="MINERU_OCR_ENABLED")
    libreoffice_path: str = Field(default="soffice", validation_alias="LIBREOFFICE_PATH")

    cleaning_config_version: str = Field(
        default="stage3-cleaning-v1", validation_alias="CLEANING_CONFIG_VERSION"
    )
    cleaning_boilerplate_min_occurrences: int = Field(
        default=3, ge=2, validation_alias="CLEANING_BOILERPLATE_MIN_OCCURRENCES"
    )
    cleaning_boilerplate_min_page_ratio: float = Field(
        default=0.6, ge=0, le=1, validation_alias="CLEANING_BOILERPLATE_MIN_PAGE_RATIO"
    )
    cleaning_near_duplicate_threshold: float = Field(
        default=0.92, gt=0, le=1, validation_alias="CLEANING_NEAR_DUPLICATE_THRESHOLD"
    )
    cleaning_near_duplicate_min_chars: int = Field(
        default=80, ge=1, validation_alias="CLEANING_NEAR_DUPLICATE_MIN_CHARS"
    )

    quality_rule_set_version: str = Field(
        default="stage4-quality-rules-v1", validation_alias="QUALITY_RULE_SET_VERSION"
    )
    quality_policy_version: str = Field(
        default="stage4-quality-policy-v1", validation_alias="QUALITY_POLICY_VERSION"
    )
    quality_corruption_warning_ratio: float = Field(
        default=0.001, ge=0, le=1, validation_alias="QUALITY_CORRUPTION_WARNING_RATIO"
    )
    quality_corruption_quarantine_ratio: float = Field(
        default=0.05, ge=0, le=1, validation_alias="QUALITY_CORRUPTION_QUARANTINE_RATIO"
    )
    quality_corruption_reject_ratio: float = Field(
        default=0.30, ge=0, le=1, validation_alias="QUALITY_CORRUPTION_REJECT_RATIO"
    )
    quality_duplicate_quarantine_ratio: float = Field(
        default=0.50, ge=0, le=1, validation_alias="QUALITY_DUPLICATE_QUARANTINE_RATIO"
    )
    quality_missing_locator_quarantine_ratio: float = Field(
        default=0.30, ge=0, le=1, validation_alias="QUALITY_MISSING_LOCATOR_QUARANTINE_RATIO"
    )
    quality_empty_page_quarantine_ratio: float = Field(
        default=0.50, ge=0, le=1, validation_alias="QUALITY_EMPTY_PAGE_QUARANTINE_RATIO"
    )
    quality_parser_confidence_warning: float = Field(
        default=0.50, ge=0, le=1, validation_alias="QUALITY_PARSER_CONFIDENCE_WARNING"
    )
    quality_low_confidence_quarantine_ratio: float = Field(
        default=0.50,
        ge=0,
        le=1,
        validation_alias="QUALITY_LOW_CONFIDENCE_QUARANTINE_RATIO",
    )
    quality_information_density_warning: float = Field(
        default=0.40, ge=0, le=1, validation_alias="QUALITY_INFORMATION_DENSITY_WARNING"
    )
    quality_information_density_quarantine: float = Field(
        default=0.20,
        ge=0,
        le=1,
        validation_alias="QUALITY_INFORMATION_DENSITY_QUARANTINE",
    )
    quality_sparse_extraction_min_bytes: int = Field(
        default=1_048_576,
        ge=1,
        validation_alias="QUALITY_SPARSE_EXTRACTION_MIN_BYTES",
    )
    quality_sparse_extraction_min_chars_per_mb: int = Field(
        default=100,
        ge=1,
        validation_alias="QUALITY_SPARSE_EXTRACTION_MIN_CHARS_PER_MB",
    )
    quality_reject_parse_threshold: float = Field(
        default=0.05, ge=0, le=1, validation_alias="QUALITY_REJECT_PARSE_THRESHOLD"
    )
    quality_reject_text_threshold: float = Field(
        default=0.20, ge=0, le=1, validation_alias="QUALITY_REJECT_TEXT_THRESHOLD"
    )
    quality_quarantine_dimension_threshold: float = Field(
        default=0.50,
        ge=0,
        le=1,
        validation_alias="QUALITY_QUARANTINE_DIMENSION_THRESHOLD",
    )
    quality_warning_dimension_threshold: float = Field(
        default=0.80,
        ge=0,
        le=1,
        validation_alias="QUALITY_WARNING_DIMENSION_THRESHOLD",
    )

    dingo_enabled: bool = Field(default=False, validation_alias="DINGO_ENABLED")
    dingo_rule_enabled: bool = Field(default=True, validation_alias="DINGO_RULE_ENABLED")
    dingo_llm_enabled: bool = Field(default=False, validation_alias="DINGO_LLM_ENABLED")
    dingo_rule_names: str = Field(
        default="RuleAbnormalChar,RuleAbnormalHtml,RuleContentNull",
        validation_alias="DINGO_RULE_NAMES",
    )
    dingo_llm_api_key: SecretStr | None = Field(default=None, validation_alias="DINGO_LLM_API_KEY")
    dingo_llm_max_chars: int = Field(default=30000, ge=1000, validation_alias="DINGO_LLM_MAX_CHARS")

    chunking_config_version: str = Field(
        default="stage5-parent-child-v3", validation_alias="CHUNKING_CONFIG_VERSION"
    )
    chunking_parent_target_tokens: int = Field(
        default=1800, ge=1, validation_alias="CHUNKING_PARENT_TARGET_TOKENS"
    )
    chunking_parent_max_tokens: int = Field(
        default=2500, ge=1, validation_alias="CHUNKING_PARENT_MAX_TOKENS"
    )
    chunking_child_target_tokens: int = Field(
        default=500, ge=1, validation_alias="CHUNKING_CHILD_TARGET_TOKENS"
    )
    chunking_child_max_tokens: int = Field(
        default=600, ge=1, validation_alias="CHUNKING_CHILD_MAX_TOKENS"
    )
    chunking_child_overlap_tokens: int = Field(
        default=64, ge=0, validation_alias="CHUNKING_CHILD_OVERLAP_TOKENS"
    )

    voyage_api_key: SecretStr | None = Field(default=None, validation_alias="VOYAGE_API_KEY")
    voyage_base_url: str = Field(
        default="https://api.voyageai.com/v1", validation_alias="VOYAGE_BASE_URL"
    )
    voyage_embedding_model: str = Field(
        default="voyage-4", validation_alias="VOYAGE_EMBEDDING_MODEL"
    )
    voyage_embedding_dimension: int = Field(
        default=1024, ge=1, validation_alias="VOYAGE_EMBEDDING_DIMENSION"
    )
    voyage_embedding_config_version: str = Field(
        default="stage6-scoped-chunk-v3", validation_alias="VOYAGE_EMBEDDING_CONFIG_VERSION"
    )
    voyage_embedding_batch_items: int = Field(
        default=128, ge=1, validation_alias="VOYAGE_EMBEDDING_BATCH_ITEMS"
    )
    voyage_embedding_batch_tokens: int = Field(
        default=8000, ge=1, validation_alias="VOYAGE_EMBEDDING_BATCH_TOKENS"
    )
    voyage_embedding_rate_limit_enabled: bool = Field(
        default=True, validation_alias="VOYAGE_EMBEDDING_RATE_LIMIT_ENABLED"
    )
    voyage_embedding_rate_limit_rpm: int = Field(
        default=3, ge=1, validation_alias="VOYAGE_EMBEDDING_RATE_LIMIT_RPM"
    )
    voyage_embedding_rate_limit_tpm: int = Field(
        default=9000, ge=1, validation_alias="VOYAGE_EMBEDDING_RATE_LIMIT_TPM"
    )
    voyage_embedding_rate_limit_window_seconds: int = Field(
        default=60, ge=1, validation_alias="VOYAGE_EMBEDDING_RATE_LIMIT_WINDOW_SECONDS"
    )
    voyage_embedding_rate_limit_fallback_seconds: int = Field(
        default=65, ge=1, validation_alias="VOYAGE_EMBEDDING_RATE_LIMIT_FALLBACK_SECONDS"
    )
    voyage_embedding_max_retries: int = Field(
        default=3, ge=0, le=10, validation_alias="VOYAGE_EMBEDDING_MAX_RETRIES"
    )
    voyage_embedding_retry_base_seconds: float = Field(
        default=1, ge=0, validation_alias="VOYAGE_EMBEDDING_RETRY_BASE_SECONDS"
    )
    voyage_embedding_retry_max_seconds: float = Field(
        default=30, ge=0, validation_alias="VOYAGE_EMBEDDING_RETRY_MAX_SECONDS"
    )
    voyage_embedding_price_per_million_tokens: float | None = Field(
        default=None, ge=0, validation_alias="VOYAGE_EMBEDDING_PRICE_PER_MILLION_TOKENS"
    )
    voyage_timeout_seconds: float = Field(
        default=60, gt=0, validation_alias="VOYAGE_TIMEOUT_SECONDS"
    )

    opensearch_url: str | None = Field(default=None, validation_alias="OPENSEARCH_URL")
    opensearch_username: str | None = Field(default=None, validation_alias="OPENSEARCH_USERNAME")
    opensearch_password: SecretStr | None = Field(
        default=None, validation_alias="OPENSEARCH_PASSWORD"
    )
    opensearch_ca_cert: Path | None = Field(default=None, validation_alias="OPENSEARCH_CA_CERT")
    opensearch_verify_tls: bool = Field(default=True, validation_alias="OPENSEARCH_VERIFY_TLS")
    opensearch_timeout_seconds: float = Field(
        default=30, gt=0, validation_alias="OPENSEARCH_TIMEOUT_SECONDS"
    )
    opensearch_index_config_version: str = Field(
        default="stage6-opensearch-v2", validation_alias="OPENSEARCH_INDEX_CONFIG_VERSION"
    )
    opensearch_documents_index: str = Field(
        default="rag-documents-v1", validation_alias="OPENSEARCH_DOCUMENTS_INDEX"
    )
    opensearch_chunks_index: str = Field(
        default="rag-chunks-v1", validation_alias="OPENSEARCH_CHUNKS_INDEX"
    )
    opensearch_documents_read_alias: str = Field(
        default="rag-documents-read", validation_alias="OPENSEARCH_DOCUMENTS_READ_ALIAS"
    )
    opensearch_chunks_read_alias: str = Field(
        default="rag-chunks-read", validation_alias="OPENSEARCH_CHUNKS_READ_ALIAS"
    )
    opensearch_chunks_write_alias: str = Field(
        default="rag-chunks-write", validation_alias="OPENSEARCH_CHUNKS_WRITE_ALIAS"
    )
    opensearch_bulk_actions: int = Field(
        default=250, ge=1, validation_alias="OPENSEARCH_BULK_ACTIONS"
    )
    opensearch_max_retries: int = Field(
        default=3, ge=0, le=10, validation_alias="OPENSEARCH_MAX_RETRIES"
    )
    opensearch_retry_base_seconds: float = Field(
        default=1, ge=0, validation_alias="OPENSEARCH_RETRY_BASE_SECONDS"
    )
    opensearch_retry_max_seconds: float = Field(
        default=30, ge=0, validation_alias="OPENSEARCH_RETRY_MAX_SECONDS"
    )

    graph_enabled: bool = Field(default=False, validation_alias="GRAPH_ENABLED")
    graph_schema_version: str = Field(
        default="enterprise-core-v1", validation_alias="GRAPH_SCHEMA_VERSION"
    )
    graph_extractor_version: str = Field(
        default="llama-schema-v3", validation_alias="GRAPH_EXTRACTOR_VERSION"
    )
    graph_prompt_version: str = Field(
        default="stage9-extraction-v3", validation_alias="GRAPH_PROMPT_VERSION"
    )
    graph_max_triplets_per_parent: int = Field(
        default=12, ge=1, le=50, validation_alias="GRAPH_MAX_TRIPLETS_PER_PARENT"
    )
    graph_extraction_workers: int = Field(
        default=2, ge=1, le=16, validation_alias="GRAPH_EXTRACTION_WORKERS"
    )
    graph_llm_reasoning_effort: Literal["none", "low", "medium", "high"] = Field(
        default="none", validation_alias="GRAPH_LLM_REASONING_EFFORT"
    )
    graph_llm_max_output_tokens: int = Field(
        default=8000, ge=256, le=32000, validation_alias="GRAPH_LLM_MAX_OUTPUT_TOKENS"
    )
    graph_llm_max_retries: int = Field(
        default=3, ge=0, le=10, validation_alias="GRAPH_LLM_MAX_RETRIES"
    )
    graph_llm_retry_base_seconds: float = Field(
        default=2, ge=0, le=60, validation_alias="GRAPH_LLM_RETRY_BASE_SECONDS"
    )
    graph_llm_retry_max_seconds: float = Field(
        default=15, ge=0, le=120, validation_alias="GRAPH_LLM_RETRY_MAX_SECONDS"
    )
    graph_max_failed_parent_ratio: float = Field(
        default=0.2, ge=0, le=1, validation_alias="GRAPH_MAX_FAILED_PARENT_RATIO"
    )
    graph_run_stale_seconds: int = Field(
        default=900, ge=60, validation_alias="GRAPH_RUN_STALE_SECONDS"
    )
    graph_build_max_attempts: int = Field(
        default=2, ge=1, le=5, validation_alias="GRAPH_BUILD_MAX_ATTEMPTS"
    )
    graph_query_enabled: bool = Field(default=True, validation_alias="GRAPH_QUERY_ENABLED")
    graph_query_max_depth: int = Field(
        default=3, ge=1, le=5, validation_alias="GRAPH_QUERY_MAX_DEPTH"
    )
    graph_query_max_rows: int = Field(
        default=50, ge=1, le=200, validation_alias="GRAPH_QUERY_MAX_ROWS"
    )
    graph_query_timeout_seconds: float = Field(
        default=3, gt=0, le=30, validation_alias="GRAPH_QUERY_TIMEOUT_SECONDS"
    )
    graph_text_to_cypher_timeout_seconds: float = Field(
        default=8,
        gt=0,
        le=30,
        validation_alias="GRAPH_TEXT_TO_CYPHER_TIMEOUT_SECONDS",
    )
    graph_rrf_weight: float = Field(default=0.8, gt=0, validation_alias="GRAPH_RRF_WEIGHT")
    neo4j_url: str | None = Field(default=None, validation_alias="NEO4J_URL")
    neo4j_username: str | None = Field(default=None, validation_alias="NEO4J_USERNAME")
    neo4j_password: SecretStr | None = Field(default=None, validation_alias="NEO4J_PASSWORD")
    neo4j_database: str = Field(default="neo4j", validation_alias="NEO4J_DATABASE")

    voyage_rerank_model: str = Field(default="rerank-2.5", validation_alias="VOYAGE_RERANK_MODEL")
    voyage_rerank_max_retries: int = Field(
        default=2, ge=0, le=10, validation_alias="VOYAGE_RERANK_MAX_RETRIES"
    )
    voyage_rerank_retry_base_seconds: float = Field(
        default=1, ge=0, validation_alias="VOYAGE_RERANK_RETRY_BASE_SECONDS"
    )
    voyage_rerank_retry_max_seconds: float = Field(
        default=15, ge=0, validation_alias="VOYAGE_RERANK_RETRY_MAX_SECONDS"
    )
    voyage_rerank_price_per_million_tokens: float | None = Field(
        default=None, ge=0, validation_alias="VOYAGE_RERANK_PRICE_PER_MILLION_TOKENS"
    )

    retrieval_config_version: str = Field(
        default="stage7-hierarchical-v4", validation_alias="RETRIEVAL_CONFIG_VERSION"
    )
    retrieval_query_max_chars: int = Field(
        default=2000, ge=1, validation_alias="RETRIEVAL_QUERY_MAX_CHARS"
    )
    retrieval_bm25_top_k: int = Field(
        default=100, ge=1, le=1000, validation_alias="RETRIEVAL_BM25_TOP_K"
    )
    retrieval_document_bm25_top_k: int = Field(
        default=50, ge=1, le=1000, validation_alias="RETRIEVAL_DOCUMENT_BM25_TOP_K"
    )
    retrieval_dense_top_k: int = Field(
        default=100, ge=1, le=1000, validation_alias="RETRIEVAL_DENSE_TOP_K"
    )
    retrieval_rrf_top_k: int = Field(
        default=60, ge=1, le=1000, validation_alias="RETRIEVAL_RRF_TOP_K"
    )
    retrieval_rrf_rank_constant: int = Field(
        default=60, ge=1, validation_alias="RETRIEVAL_RRF_RANK_CONSTANT"
    )
    retrieval_bm25_weight: float = Field(default=1, gt=0, validation_alias="RETRIEVAL_BM25_WEIGHT")
    retrieval_dense_weight: float = Field(
        default=1, gt=0, validation_alias="RETRIEVAL_DENSE_WEIGHT"
    )
    retrieval_document_weight: float = Field(
        default=0.5, ge=0, validation_alias="RETRIEVAL_DOCUMENT_WEIGHT"
    )
    retrieval_sibling_duplicate_similarity_threshold: float = Field(
        default=0.96,
        ge=0,
        le=1,
        validation_alias="RETRIEVAL_SIBLING_DUPLICATE_SIMILARITY_THRESHOLD",
    )
    retrieval_min_rrf_score_ratio: float = Field(
        default=0.25, ge=0, le=1, validation_alias="RETRIEVAL_MIN_RRF_SCORE_RATIO"
    )
    retrieval_rerank_candidate_top_k: int = Field(
        default=40, ge=1, le=1000, validation_alias="RETRIEVAL_RERANK_CANDIDATE_TOP_K"
    )
    retrieval_final_child_top_k: int = Field(
        default=10, ge=1, le=100, validation_alias="RETRIEVAL_FINAL_CHILD_TOP_K"
    )
    retrieval_mmr_lambda: float = Field(
        default=0.85, ge=0, le=1, validation_alias="RETRIEVAL_MMR_LAMBDA"
    )
    retrieval_relevance_rerank_weight: float = Field(
        default=0.55, ge=0, validation_alias="RETRIEVAL_RELEVANCE_RERANK_WEIGHT"
    )
    retrieval_relevance_rrf_weight: float = Field(
        default=0.25, ge=0, validation_alias="RETRIEVAL_RELEVANCE_RRF_WEIGHT"
    )
    retrieval_relevance_lexical_weight: float = Field(
        default=0.1, ge=0, validation_alias="RETRIEVAL_RELEVANCE_LEXICAL_WEIGHT"
    )
    retrieval_relevance_scope_weight: float = Field(
        default=0.1, ge=0, validation_alias="RETRIEVAL_RELEVANCE_SCOPE_WEIGHT"
    )
    retrieval_context_candidate_top_k: int = Field(
        default=24, ge=1, le=1000, validation_alias="RETRIEVAL_CONTEXT_CANDIDATE_TOP_K"
    )
    retrieval_rerank_fallback_enabled: bool = Field(
        default=True, validation_alias="RETRIEVAL_RERANK_FALLBACK_ENABLED"
    )
    retrieval_context_max_tokens: int = Field(
        default=8000, ge=1, validation_alias="RETRIEVAL_CONTEXT_MAX_TOKENS"
    )
    retrieval_context_parent_max_tokens: int = Field(
        default=2500, ge=1, validation_alias="RETRIEVAL_CONTEXT_PARENT_MAX_TOKENS"
    )
    retrieval_context_neighbor_limit: int = Field(
        default=1, ge=0, le=2, validation_alias="RETRIEVAL_CONTEXT_NEIGHBOR_LIMIT"
    )
    retrieval_parent_merge_min_children: int = Field(
        default=2, ge=2, validation_alias="RETRIEVAL_PARENT_MERGE_MIN_CHILDREN"
    )
    retrieval_parent_merge_ratio: float = Field(
        default=0.5, gt=0, le=1, validation_alias="RETRIEVAL_PARENT_MERGE_RATIO"
    )

    llm_base_url: str = Field(default="https://api.openai.com/v1", validation_alias="LLM_BASE_URL")
    llm_api_key: SecretStr | None = Field(default=None, validation_alias="LLM_API_KEY")
    llm_model: str = Field(default="gpt-5.4", validation_alias="LLM_MODEL")
    llm_api_style: Literal["responses", "chat_completions"] = Field(
        default="responses", validation_alias="LLM_API_STYLE"
    )
    llm_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = Field(
        default="medium", validation_alias="LLM_REASONING_EFFORT"
    )
    llm_timeout_seconds: float = Field(default=120, gt=0, validation_alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=1, ge=0, le=5, validation_alias="LLM_MAX_RETRIES")
    llm_retry_base_seconds: float = Field(
        default=1, ge=0, validation_alias="LLM_RETRY_BASE_SECONDS"
    )
    llm_max_output_tokens: int = Field(default=2000, ge=1, validation_alias="LLM_MAX_OUTPUT_TOKENS")
    llm_price_per_million_input_tokens: float | None = Field(
        default=None, ge=0, validation_alias="LLM_PRICE_PER_MILLION_INPUT_TOKENS"
    )
    llm_price_per_million_output_tokens: float | None = Field(
        default=None, ge=0, validation_alias="LLM_PRICE_PER_MILLION_OUTPUT_TOKENS"
    )
    generation_prompt_version: str = Field(
        default="stage8-grounded-rag-v3-zh", validation_alias="GENERATION_PROMPT_VERSION"
    )
    query_rewrite_prompt_version: str = Field(
        default="stage8-retrieval-query-plan-v1-zh",
        validation_alias="QUERY_REWRITE_PROMPT_VERSION",
    )
    query_rewrite_history_messages: int = Field(
        default=6, ge=0, le=20, validation_alias="QUERY_REWRITE_HISTORY_MESSAGES"
    )
    query_rewrite_max_output_tokens: int = Field(
        default=500, ge=1, le=1000, validation_alias="QUERY_REWRITE_MAX_OUTPUT_TOKENS"
    )
    citation_excerpt_max_chars: int = Field(
        default=500, ge=50, le=5000, validation_alias="CITATION_EXCERPT_MAX_CHARS"
    )

    agentic_rag_enabled: bool = Field(default=False, validation_alias="AGENTIC_RAG_ENABLED")
    agent_graph_version: str = Field(
        default="stage13-langgraph-agentic-rag-v3", validation_alias="AGENT_GRAPH_VERSION"
    )
    agent_prompt_version: str = Field(
        default="stage13-agent-query-plan-v5-zh", validation_alias="AGENT_PROMPT_VERSION"
    )
    agent_history_messages: int = Field(
        default=6, ge=0, le=20, validation_alias="AGENT_HISTORY_MESSAGES"
    )
    agent_max_output_tokens: int = Field(
        default=500, ge=1, le=4000, validation_alias="AGENT_MAX_OUTPUT_TOKENS"
    )
    agent_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = Field(
        default="none", validation_alias="AGENT_REASONING_EFFORT"
    )
    agent_recursion_limit: int = Field(
        default=12, ge=4, le=50, validation_alias="AGENT_RECURSION_LIMIT"
    )

    evaluation_dataset_root: Path = Field(
        default=Path("../evals/datasets"), validation_alias="EVALUATION_DATASET_ROOT"
    )
    evaluation_report_root: Path = Field(
        default=Path("../evals/reports"), validation_alias="EVALUATION_REPORT_ROOT"
    )

    langfuse_enabled: bool = Field(default=True, validation_alias="LANGFUSE_ENABLED")
    langfuse_public_key: SecretStr | None = Field(
        default=None, validation_alias="LANGFUSE_PUBLIC_KEY"
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None, validation_alias="LANGFUSE_SECRET_KEY"
    )
    langfuse_base_url: str = Field(
        default="https://cloud.langfuse.com", validation_alias="LANGFUSE_BASE_URL"
    )
    langfuse_sample_rate: float = Field(
        default=1.0, ge=0, le=1, validation_alias="LANGFUSE_SAMPLE_RATE"
    )
    langfuse_capture_content: bool = Field(
        default=False, validation_alias="LANGFUSE_CAPTURE_CONTENT"
    )
    langfuse_timeout_seconds: int = Field(
        default=3, ge=1, le=30, validation_alias="LANGFUSE_TIMEOUT_SECONDS"
    )
    langfuse_flush_at: int = Field(default=20, ge=1, validation_alias="LANGFUSE_FLUSH_AT")
    langfuse_flush_interval_seconds: float = Field(
        default=5.0, gt=0, validation_alias="LANGFUSE_FLUSH_INTERVAL_SECONDS"
    )

    @field_validator(
        "mineru_token",
        "dingo_llm_api_key",
        "llm_api_key",
        "voyage_api_key",
        "voyage_embedding_price_per_million_tokens",
        "voyage_rerank_price_per_million_tokens",
        "opensearch_url",
        "opensearch_username",
        "opensearch_password",
        "opensearch_ca_cert",
        "llm_price_per_million_input_tokens",
        "llm_price_per_million_output_tokens",
        "langfuse_public_key",
        "langfuse_secret_key",
        mode="before",
    )
    @classmethod
    def empty_optional_value_is_none(cls, value: object) -> object:
        """Allow checked-in env templates to use blank optional settings."""

        return None if value == "" else value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide immutable settings snapshot."""

    return Settings()
