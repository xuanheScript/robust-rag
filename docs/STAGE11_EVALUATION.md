# 阶段 11：Ragas 与黄金集

## 已实现范围

- `golden-dataset/1.0` 版本化 Schema、内容摘要和严格校验。
- `enterprise-golden-v1` 首批 50 条中英双语种子样本。
- HitRate、Recall、Precision、MRR、nDCG、父文档召回、引用定位和拒答指标。
- 实体关系 P/R/F1、路径命中、Text-to-Cypher 语法/Schema/执行/安全拒绝指标。
- 同问题 OpenSearch 基线与图谱增强结果配对，统计增益和退化率。
- Ragas 0.2.15 六项指标适配：Context Precision、Context Recall、Faithfulness、
  Response Relevancy、Answer Correctness 和 Noise Sensitivity。
- PostgreSQL `evaluation_runs` 与 `evaluation_sample_results` 审计记录。
- JSON 与 Markdown 报告，包含数据集摘要、配置、模型、成本、Trace 和失败样本。
- 历史运行基线比较与按指标允许下降阈值。

## 黄金集维护

黄金集位于 `evals/datasets/`。样本 ID 在版本内必须唯一；可回答问题必须至少包含
一个相关文档或 Node，且必须提供 `expected_answer` 或 `rubric`。

仓库中的首批 50 条是种子语料基准。`metadata.seed_document_key` 标识预期文件，
`relevant_document_ids` 是稳定的种子语料身份。阶段 13 导入真实 50 份文件时，应先把
这些身份映射为实际 `Document.id`；Ground Truth 发生变化时创建新数据集版本，不覆盖 v1。

## 运行方式

仅运行确定性检索评测，不产生 LLM 费用：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/evaluations \
  -H 'content-type: application/json' \
  -d '{"dataset_version":"enterprise-golden-v1","mode":"hybrid_rerank"}'
```

启用生成和 Ragas 前安装可选依赖：

```bash
cd backend
uv sync --extra eval
```

随后显式请求付费评测：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/evaluations \
  -H 'content-type: application/json' \
  -d '{
    "dataset_version":"enterprise-golden-v1",
    "mode":"hybrid_rerank",
    "include_generation":true,
    "include_ragas":true
  }'
```

Ragas Judge 使用配置的 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`，语义向量复用生产 Voyage
Adapter，不会静默改用另一个 Provider。请求未显式设置 `include_generation` 和
`include_ragas` 时，评测保持零 LLM 费用。

API：

- `POST /api/v1/evaluations`：执行评测并创建运行记录。
- `GET /api/v1/evaluations`：列出历史运行。
- `GET /api/v1/evaluations/{evaluation_id}`：查看聚合指标和逐题证据。

报告默认写入 `evals/reports/`，路径可通过 `EVALUATION_REPORT_ROOT` 修改。每次运行同时
产生 `evaluation-report/1.0` JSON 和便于人工阅读的 Markdown。

## 回归门槛

传入 `baseline_run_id` 后，候选运行会与历史基线比较。默认门槛：

| 指标 | 允许最大下降 |
|---|---:|
| HitRate@10 | 0 |
| Recall@10 | 0.02 |
| MRR | 0.02 |
| nDCG@10 | 0.02 |
| Faithfulness | 0.03 |
| Answer Correctness | 0.03 |

报告的 `regression.passed` 和 `regression.failures` 给出最终判定及逐项差异。解析、清洗、
分块、Embedding、Analyzer、融合、Reranker、图谱 Schema、Cypher、Prompt 或模型变更后，
都应以同一数据集版本运行并保存报告。

## 费用和失败语义

- Retrieval 中已知的 Embedding/Rerank 成本与生成成本按样本累计，Token/重试数据保存在逐题 `usage` 中。
- Ragas 0.2.15 不返回完整 Judge Token 用量，因此报告明确记录 `ragas_judge_cost_included=false`，不会把部分成本伪装成全量成本。
- 未配置单价时不猜测价格；模型与 Token 使用仍保留在生产 Trace 中。
- 单题异常保存为失败样本，不丢失已完成结果。
- Ragas 批次本身失败时整次运行标记 `failed`，避免把不完整语义指标误认为基线。
- 无答案样本不计入 HitRate/Recall 分母，单独统计检索空结果和拒答准确率。

## 验证

默认测试使用内存 OpenSearch、Fake Voyage、Fake Answer Generator 和 Fake Ragas，验证
数据集摘要、六项 Ragas 映射、持久化、报告、成本、失败样本及三个评测 API。真实 Ragas
导入契约已用锁定的 0.2.15 接口验证，测试过程不调用外部模型。
