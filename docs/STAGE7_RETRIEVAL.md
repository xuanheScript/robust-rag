# 阶段 7：混合召回与 Reranker

阶段 7 在阶段 6 的 READY Child 检索投影之上实现可独立对照、可降级、可复现的在线检索链路。PostgreSQL 保存完整 Retrieval Trace，OpenSearch 继续只承担可重建的 BM25/Dense 检索投影。

## 检索链路

```text
原始问题
  → Unicode NFKC、零宽字符和空白归一化
  → Query Rewrite Adapter
  → 文档 BM25 + Chunk BM25（标题路径/正文/结构关键词）/ Scope-aware Dense
  → 分层加权 RRF（文档先验 + Chunk 证据）
  → 候选卫生过滤（重复、低信息标题、低相关候选）
  → Voyage rerank-2.5（可选）
  → Cross-Encoder/RRF/词法/显式 Scope 相关性融合
  → 在扩大候选池上执行 MMR
  → Parent 自动合并/相邻窗口，再消耗最终 Context 槽位与 Token 预算
  → Retrieval Trace
```

`mode` 支持四种可独立运行的对照：

- `bm25`：只运行 OpenSearch BM25。
- `dense`：使用 `voyage-4` 和 `input_type=query` 生成查询向量，再运行 Dense 检索。
- `hybrid`：并行候选经过应用层 RRF、候选卫生过滤与 MMR。
- `hybrid_rerank`：在 Hybrid 候选上增加 `rerank-2.5`，是默认模式。

当前 Query Rewrite Adapter 使用确定性的 Identity 实现，只记录归一化问题和改写版本，不调用 LLM。阶段 8 接入多轮会话后可替换为基于对话的改写器，无需修改检索主流程。

## 文档相关性与 Chunk 相关性

文档信号和 Chunk 信号使用不同索引、不同字段和不同分数槽位：

- 文档 BM25 只查 `rag-documents-read` 的 `title`、`original_filename`，用于识别文档先验。
- Chunk BM25 查询 `heading_path`、`content` 和确定性提取的 `retrieval_keywords`；文档标题仍通过独立文档索引产生先验，不在 Chunk BM25 中无差别重复。
- Chunk Dense 使用 `Source document + Hierarchy + Retrieval keywords + Content` 的受控表示，并按 `embedding_config_version` 过滤旧向量。
- Chunk 精确命中只检查标题路径和正文；文档标题命中不会让整篇文档的全部 Chunk 被视为精确命中。
- Reranker 输入显式区分 `Source document`、`Hierarchy`、`Retrieval keywords` 和 `Evidence`，附件不会丢失来源范围，标题也不会在路径中重复拼接。

文档先验只传播给已经被 Chunk BM25、Dense 或 Graph 召回的节点，不单独制造任意 Chunk，也不是文档白名单。因此，文档召回失败不会挡住内容召回，文档召回成功也不会让该文档的无关 Chunk 获得相同 Chunk 排名。

## 分层 RRF 与候选卫生过滤

BM25 与 Dense 原始分数不可直接比较，因此融合使用排名而不是分数：

```text
chunk_rrf(c) = bm25_weight / (rank_constant + chunk_bm25_rank)
             + dense_weight / (rank_constant + dense_rank)
             + graph_weight / (rank_constant + graph_rank)

document_rrf(doc(c)) = document_weight / (rank_constant + document_rank)

RRF(c) = chunk_rrf(c) + document_rrf(doc(c))
```

默认 `rank_constant=60`，BM25/Dense 权重为 1，文档先验权重为 0.5。同一文档的所有候选获得完全相同的 `document_rrf`，所以它只能影响跨文档竞争，不会压平或改写同文档内部的 `chunk_rrf` 顺序。相同总分按最佳 Chunk 通道排名和稳定 Node ID 确定顺序，保证重复执行可复现。

RRF Top 60 到 Rerank Top 40 之间不再设置单文档或单 Parent 的硬配额。候选只会因为以下原因被排除：正文完全重复、同 Parent 下向量或文本高度相似、`附件 1` 这类低信息 heading-only 节点、相对最高 RRF 明显过低，或超出 Rerank 全局窗口。正文精确命中会绕过相对 RRF 阈值，但不会绕过重复和低信息检查。所有保留和排除原因都会进入 Trace。

## Voyage Reranker

Adapter 按 [Voyage Reranker 官方契约](https://docs.voyageai.com/docs/reranker) 调用：

```text
POST /v1/rerank
model=rerank-2.5
query=<normalized/rewritten query>
documents=<filtered candidates>
top_k=<candidate count>
truncation=true
```

默认最多重排 40 个候选。Adapter 校验返回索引唯一且位于输入范围内，并记录 Provider Token、重试次数、耗时和可选成本。价格不会硬编码；只有设置 `VOYAGE_RERANK_PRICE_PER_MILLION_TOKENS` 才估算成本。

429、5xx 和网络错误按配置退避重试。默认允许 Reranker 失败后退回 RRF 顺序，响应与 Trace 标记为 `degraded` 并保存降级原因；关闭降级后，错误直接返回且 Trace 标记为 `failed`。

## 混合相关性与 MMR

Cross-Encoder 不再覆盖第一阶段信号。每个候选分别记录并归一化 `rerank_score`、`rrf_score`、词法分数和显式实体的 Scope 匹配，默认按 `0.55/0.25/0.10/0.10` 融合；某个通道不可用时，其余通道自动重新归一化。

随后在最多 24 个 Context 候选上执行 MMR：

```text
final_score = lambda * hybrid_relevance
            - (1 - lambda) * max_similarity_to_selected
```

默认 `lambda=0.85`。相似度优先使用 Child Embedding 余弦相似度；向量缺失或维度不一致时退回文本 shingle。MMR 产生的是 Parent/窗口组装候选池，不在此处截断最终 Child。

## 上下文组装

- Parent/连续窗口合并发生在最终 Context Top K 截断前。同一 Parent 至少命中配置数量的 Child，且命中比例达到阈值时，恢复完整 Parent；合并后的 Parent 只占一个 Context 槽位，所有命中 Child 写入 `supporting_child_ids`。
- 未达到 Parent 合并阈值时，连续命中的相邻 Child 会按文档顺序拼成一个 `window`，同样只占一个 Context 槽位。
- 单个 Child 或不连续命中保留 Child；需要连续语义时最多扩展配置数量的同 Parent 相邻 Child。
- Parent 超过单 Parent 上限、合并窗口超过剩余预算时自动回退到更小粒度证据。
- 内容哈希用于跨节点去重，所有节点都必须满足全局 Token 预算。
- 请求可以降低 `top_k` 和上下文预算，但不能超过服务端配置上限。
- 每个上下文节点保留标题路径、内容类型、SourceLocator、选择原因和支持它的 Child。

## API 与 Retrieval Trace

```text
POST /api/v1/retrieval/search
GET  /api/v1/retrieval/traces?limit=20
GET  /api/v1/retrieval/traces/{trace_id}
```

`POST /search` 可设置 `debug=true`，返回 Document BM25、Chunk BM25、Dense、RRF、候选过滤、原始 Cross-Encoder、混合相关性、MMR Context 候选、最终代表 Child 和上下文快照。数据库字段 `diversified_candidates_json` 为兼容既有 API 暂时保留，但其内容已经是候选过滤快照。

Langfuse 额外记录 `retrieval.relevance_fusion`、`retrieval.mmr` 和 `retrieval.context_assembly`，可以直接查看候选在 Cross-Encoder 之后的融合排名、MMR 分数和最终 Parent/窗口选择，不再必须查询 PostgreSQL 才能定位截断阶段。

- 原始、归一化、改写后的问题和改写器快照。
- 检索模式、配置版本和完整配置快照。
- Embedding/Reranker Provider、模型和维度。
- 每个候选阶段的排名、分数、保留或排除原因；RRF 候选分别记录 `document_rrf_score`、`chunk_rrf_score` 和总 `rrf_score`。
- 最终上下文、预算、实际 Token、耗时、用量、重试、成本与降级原因。
- `running`、`succeeded`、`degraded`、`failed` 状态和结构化错误。

在线检索只会补全 PostgreSQL 中当前 READY、ACTIVE、索引成功的 Child。OpenSearch 中残留的旧版本或异常 Node ID 不会进入最终上下文。

## 配置

完整变量见 `.env.example`，关键项如下：

```text
VOYAGE_API_KEY
VOYAGE_RERANK_MODEL=rerank-2.5
VOYAGE_RERANK_MAX_RETRIES=2
RETRIEVAL_CONFIG_VERSION=stage7-hierarchical-v4
RETRIEVAL_BM25_TOP_K=100
RETRIEVAL_DOCUMENT_BM25_TOP_K=50
RETRIEVAL_DENSE_TOP_K=100
RETRIEVAL_RRF_TOP_K=60
RETRIEVAL_RRF_RANK_CONSTANT=60
RETRIEVAL_BM25_WEIGHT=1
RETRIEVAL_DENSE_WEIGHT=1
RETRIEVAL_DOCUMENT_WEIGHT=0.5
RETRIEVAL_SIBLING_DUPLICATE_SIMILARITY_THRESHOLD=0.96
RETRIEVAL_MIN_RRF_SCORE_RATIO=0.25
RETRIEVAL_RERANK_CANDIDATE_TOP_K=40
RETRIEVAL_FINAL_CHILD_TOP_K=10
RETRIEVAL_MMR_LAMBDA=0.85
RETRIEVAL_RELEVANCE_RERANK_WEIGHT=0.55
RETRIEVAL_RELEVANCE_RRF_WEIGHT=0.25
RETRIEVAL_RELEVANCE_LEXICAL_WEIGHT=0.1
RETRIEVAL_RELEVANCE_SCOPE_WEIGHT=0.1
RETRIEVAL_CONTEXT_CANDIDATE_TOP_K=24
RETRIEVAL_CONTEXT_MAX_TOKENS=8000
RETRIEVAL_PARENT_MERGE_MIN_CHILDREN=2
RETRIEVAL_PARENT_MERGE_RATIO=0.5
```

## 数据库迁移

基础迁移 `20260817_0007_stage7_retrieval.py` 新增 `retrieval_traces` 表；本次分层相关性迁移 `20260821_0015_document_chunk_relevance.py` 新增 `document_candidates_json`，使文档召回可以独立审计。

升级后必须把 `CHUNKING_CONFIG_VERSION` 更新为 `stage5-parent-child-v3`、`VOYAGE_EMBEDDING_CONFIG_VERSION` 更新为 `stage6-scoped-chunk-v3`，并对现有文档执行重新处理，以生成结构关键词、重新向量化并投影。单独重建 OpenSearch 索引不能生成新节点属性或新向量。迁移期间旧向量不会混入 Dense 结果，BM25 仍可工作。

需要执行时运行：

```bash
make migrate
make migration-check
```

## 验证

自动化测试覆盖受控 Scope、表格结构关键词、Query facet 扩展、Cross-Encoder/RRF/词法/Scope 融合、单文档超过 8 个候选、重复/低信息过滤、MMR、Top-K 前 Parent 合并、连续窗口、四模式对照、Trace、Reranker 降级和 Dense 失败审计。

真实 Aiven/Voyage 联调需要有效凭据；仓库当前没有 `.env`，因此本阶段没有向外部服务发送检索请求。
