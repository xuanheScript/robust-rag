# 阶段 7：混合召回与 Reranker

阶段 7 在阶段 6 的 READY Child 检索投影之上实现可独立对照、可降级、可复现的在线检索链路。PostgreSQL 保存完整 Retrieval Trace，OpenSearch 继续只承担可重建的 BM25/Dense 检索投影。

## 检索链路

```text
原始问题
  → Unicode NFKC、零宽字符和空白归一化
  → Query Rewrite Adapter
  → 文档 BM25（标题/文件名） + Chunk BM25（标题路径/正文）/ Dense（标题路径/正文）
  → 分层加权 RRF（文档先验 + Chunk 证据）
  → 文档与 Parent 多样性控制
  → Voyage rerank-2.5（可选）
  → 最终 Child
  → Parent/相邻 Child 扩展、去重与 Token 预算
  → Retrieval Trace
```

`mode` 支持四种可独立运行的对照：

- `bm25`：只运行 OpenSearch BM25。
- `dense`：使用 `voyage-4` 和 `input_type=query` 生成查询向量，再运行 Dense 检索。
- `hybrid`：并行候选经过应用层 RRF 与多样性控制。
- `hybrid_rerank`：在 Hybrid 候选上增加 `rerank-2.5`，是默认模式。

当前 Query Rewrite Adapter 使用确定性的 Identity 实现，只记录归一化问题和改写版本，不调用 LLM。阶段 8 接入多轮会话后可替换为基于对话的改写器，无需修改检索主流程。

## 文档相关性与 Chunk 相关性

文档信号和 Chunk 信号使用不同索引、不同字段和不同分数槽位：

- 文档 BM25 只查 `rag-documents-read` 的 `title`、`original_filename`，用于识别文档先验。
- Chunk BM25 只查 `rag-chunks-read` 的 `heading_path`、`content`。`title` 和包含标题/来源信息的兼容字段 `retrieval_text` 不参与 Chunk 排名。
- Chunk Dense 的文档向量只编码标题路径和正文，并按 `embedding_config_version` 过滤，旧版“标题 + Chunk”向量不会混入新链路。
- Chunk 精确命中只检查标题路径和正文；文档标题命中不会让整篇文档的全部 Chunk 绕过多样性限制。
- Reranker 输入只包含 Chunk 标题路径、内容类型和正文，文档标题不会在后段再次污染同文档内部排序。

文档先验只传播给已经被 Chunk BM25、Dense 或 Graph 召回的节点，不单独制造任意 Chunk，也不是文档白名单。因此，文档召回失败不会挡住内容召回，文档召回成功也不会让该文档的无关 Chunk 获得相同 Chunk 排名。

## 分层 RRF 与多样性

BM25 与 Dense 原始分数不可直接比较，因此融合使用排名而不是分数：

```text
chunk_rrf(c) = bm25_weight / (rank_constant + chunk_bm25_rank)
             + dense_weight / (rank_constant + dense_rank)
             + graph_weight / (rank_constant + graph_rank)

document_rrf(doc(c)) = document_weight / (rank_constant + document_rank)

RRF(c) = chunk_rrf(c) + document_rrf(doc(c))
```

默认 `rank_constant=60`，BM25/Dense 权重为 1，文档先验权重为 0.5。同一文档的所有候选获得完全相同的 `document_rrf`，所以它只能影响跨文档竞争，不会压平或改写同文档内部的 `chunk_rrf` 顺序。相同总分按最佳 Chunk 通道排名和稳定 Node ID 确定顺序，保证重复执行可复现。

融合后限制单文档与单 Parent 的 Child 数量，避免一个长文档淹没结果。正文精确命中会绕过这两个多样性上限，但仍受全局候选上限约束。所有保留和排除原因都会进入 Trace。

## Voyage Reranker

Adapter 按 [Voyage Reranker 官方契约](https://docs.voyageai.com/docs/reranker) 调用：

```text
POST /v1/rerank
model=rerank-2.5
query=<normalized/rewritten query>
documents=<diversified candidates>
top_k=<candidate count>
truncation=true
```

默认最多重排 40 个候选。Adapter 校验返回索引唯一且位于输入范围内，并记录 Provider Token、重试次数、耗时和可选成本。价格不会硬编码；只有设置 `VOYAGE_RERANK_PRICE_PER_MILLION_TOKENS` 才估算成本。

429、5xx 和网络错误按配置退避重试。默认允许 Reranker 失败后退回 RRF 顺序，响应与 Trace 标记为 `degraded` 并保存降级原因；关闭降级后，错误直接返回且 Trace 标记为 `failed`。

## 上下文组装

- 最终命中的 Child 优先恢复其 Parent，以保留完整章节语境。
- 同一 Parent 只加入一次，多个 Child 会合并到 `supporting_child_ids`。
- Parent 超过单 Parent 上限或剩余预算时改用 Child；需要连续语义时最多扩展配置数量的同 Parent 相邻 Child。
- 内容哈希用于跨节点去重，所有节点都必须满足全局 Token 预算。
- 请求可以降低 `top_k` 和上下文预算，但不能超过服务端配置上限。
- 每个上下文节点保留标题路径、内容类型、SourceLocator、选择原因和支持它的 Child。

## API 与 Retrieval Trace

```text
POST /api/v1/retrieval/search
GET  /api/v1/retrieval/traces?limit=20
GET  /api/v1/retrieval/traces/{trace_id}
```

`POST /search` 可设置 `debug=true`，直接返回 Document BM25、Chunk BM25、Dense、分层 RRF、多样性、Rerank、最终 Child 和上下文各阶段快照。无论是否开启调试，服务端都会持久化 Trace，包括：

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
RETRIEVAL_CONFIG_VERSION=stage7-hierarchical-v2
RETRIEVAL_BM25_TOP_K=100
RETRIEVAL_DOCUMENT_BM25_TOP_K=50
RETRIEVAL_DENSE_TOP_K=100
RETRIEVAL_RRF_TOP_K=60
RETRIEVAL_RRF_RANK_CONSTANT=60
RETRIEVAL_BM25_WEIGHT=1
RETRIEVAL_DENSE_WEIGHT=1
RETRIEVAL_DOCUMENT_WEIGHT=0.5
RETRIEVAL_RERANK_CANDIDATE_TOP_K=40
RETRIEVAL_FINAL_CHILD_TOP_K=10
RETRIEVAL_CONTEXT_MAX_TOKENS=8000
```

## 数据库迁移

基础迁移 `20260817_0007_stage7_retrieval.py` 新增 `retrieval_traces` 表；本次分层相关性迁移 `20260821_0015_document_chunk_relevance.py` 新增 `document_candidates_json`，使文档召回可以独立审计。

升级后必须把 `VOYAGE_EMBEDDING_CONFIG_VERSION` 更新为 `stage6-chunk-content-v2`，并对现有文档执行重新处理，使 Chunk 重新向量化并投影。单独重建 OpenSearch 索引只会复用 PostgreSQL 中的旧向量，不能替代重新向量化。新 Dense 查询会过滤不匹配的向量配置版本，因此迁移期间旧向量不会悄悄混入结果，BM25 仍可工作。

需要执行时运行：

```bash
make migrate
make migration-check
```

## 验证

自动化测试使用 Voyage、OpenSearch 和 Query Rewrite 契约替身，不访问外部服务、不产生费用。覆盖文档/Chunk 字段隔离、同文档先验不改变 Chunk 顺序、Chunk-only 向量文本、查询归一化、RRF 稳定排序、多样性与精确命中、Reranker 请求契约、Parent 去重、相邻块回退、四模式对照、预算上限、Trace API、Reranker 降级和 Dense 失败审计。

真实 Aiven/Voyage 联调需要有效凭据；仓库当前没有 `.env`，因此本阶段没有向外部服务发送检索请求。
