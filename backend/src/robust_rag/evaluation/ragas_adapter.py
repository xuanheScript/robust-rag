"""Optional Ragas bridge kept outside the zero-cost default dependency set."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Protocol

from robust_rag.core.settings import Settings
from robust_rag.indexing.embedding import EmbeddingAdapter


class RagasEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RagasSample:
    sample_id: str
    question: str
    answer: str
    contexts: list[str]
    reference: str


class RagasEvaluator(Protocol):
    name: str
    version: str

    def evaluate(self, samples: list[RagasSample]) -> dict[str, dict[str, float]]: ...


class LegacyRagasEvaluator:
    """Ragas 0.2 adapter using the stable Dataset/evaluate compatibility API.

    LLM and embedding wrappers must be supplied explicitly. This prevents Ragas from
    silently using an unrelated provider or API key.
    """

    name = "ragas"
    version = "0.2-compatible"

    def __init__(self, *, llm: object, embeddings: object) -> None:
        self.llm = llm
        self.embeddings = embeddings

    def evaluate(self, samples: list[RagasSample]) -> dict[str, dict[str, float]]:
        if not samples:
            return {}
        try:
            datasets = importlib.import_module("datasets")
            ragas = importlib.import_module("ragas")
            metrics_module = importlib.import_module("ragas.metrics")
        except ImportError as exc:
            raise RagasEvaluationError(
                "Ragas dependencies are unavailable; install the backend eval extra"
            ) from exc

        metric_names = {
            "context_precision": ("context_precision",),
            "context_recall": ("context_recall",),
            "faithfulness": ("faithfulness",),
            "response_relevancy": ("answer_relevancy", "response_relevancy"),
            "answer_correctness": ("answer_correctness",),
            "noise_sensitivity": ("noise_sensitivity_relevant", "noise_sensitivity"),
        }
        metrics: list[object] = []
        aliases: dict[str, str] = {}
        for canonical_name, candidates in metric_names.items():
            value = next(
                (
                    getattr(metrics_module, name)
                    for name in candidates
                    if hasattr(metrics_module, name)
                ),
                None,
            )
            if value is None and canonical_name == "noise_sensitivity":
                metric_class = getattr(metrics_module, "NoiseSensitivity", None)
                value = metric_class(mode="relevant") if metric_class is not None else None
            if value is None:
                raise RagasEvaluationError(f"Installed Ragas lacks metric {canonical_name}")
            metrics.append(value)
            aliases[getattr(value, "name", canonical_name)] = canonical_name

        dataset = datasets.Dataset.from_list(
            [
                {
                    "sample_id": sample.sample_id,
                    "question": sample.question,
                    "answer": sample.answer,
                    "contexts": sample.contexts,
                    "ground_truth": sample.reference,
                }
                for sample in samples
            ]
        )
        try:
            result = ragas.evaluate(
                dataset,
                metrics=metrics,
                llm=self.llm,
                embeddings=self.embeddings,
                raise_exceptions=False,
            )
            rows = result.to_pandas().to_dict(orient="records")
        except Exception as exc:
            raise RagasEvaluationError(f"Ragas evaluation failed: {exc}") from exc

        output: dict[str, dict[str, float]] = {}
        for sample, row in zip(samples, rows, strict=True):
            scores: dict[str, float] = {}
            for key, value in row.items():
                score_name = aliases.get(str(key))
                if score_name is not None and isinstance(value, (int, float)):
                    scores[score_name] = float(value)
            output[sample.sample_id] = scores
        return output


class VoyageEmbeddingBridge:
    """Minimal LangChain-compatible bridge over the production Voyage adapter."""

    def __init__(self, adapter: EmbeddingAdapter) -> None:
        self.adapter = adapter

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.adapter.embed(texts, input_type="document").vectors

    def embed_query(self, text: str) -> list[float]:
        return self.adapter.embed([text], input_type="query").vectors[0]


class ConfiguredRagasEvaluator:
    """Lazily connects Ragas to the configured LLM and production Voyage adapter."""

    name = "ragas"
    version = "0.2.15-stage11"

    def __init__(self, *, settings: Settings, embeddings: EmbeddingAdapter) -> None:
        self.settings = settings
        self.embeddings = embeddings

    def evaluate(self, samples: list[RagasSample]) -> dict[str, dict[str, float]]:
        if self.settings.llm_api_key is None:
            raise RagasEvaluationError("LLM_API_KEY is required for Ragas evaluation")
        try:
            langchain_openai = importlib.import_module("langchain_openai")
            ragas_llms = importlib.import_module("ragas.llms")
            ragas_embeddings = importlib.import_module("ragas.embeddings")
        except ImportError as exc:
            raise RagasEvaluationError(
                "Ragas dependencies are unavailable; run uv sync --extra eval"
            ) from exc
        chat = langchain_openai.ChatOpenAI(
            model=self.settings.llm_model,
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key.get_secret_value(),
            timeout=self.settings.llm_timeout_seconds,
            max_retries=self.settings.llm_max_retries,
        )
        llm = ragas_llms.LangchainLLMWrapper(chat)
        embeddings = ragas_embeddings.LangchainEmbeddingsWrapper(
            VoyageEmbeddingBridge(self.embeddings)
        )
        return LegacyRagasEvaluator(llm=llm, embeddings=embeddings).evaluate(samples)
