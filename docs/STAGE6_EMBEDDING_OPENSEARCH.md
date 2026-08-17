# 阶段 6：Embedding 与 OpenSearch

阶段 6 将通过节点门禁的 Retrieval Node 转换为可重建的检索投影。PostgreSQL 继续保存业务事实、向量和同步审计；OpenSearch 只保存当前 READY 版本的在线检索副本。

## 处理链路

```text
CHUNK_EVALUATING
  → retrieval-node-quality-gate
  → EMBEDDING（Voyage document embedding）
  → INDEXING（OpenSearch inactive projection）
  → 数量校验
  → 激活新版本并删除旧版本投影
  → READY
```

节点门禁检查 Parent/Child 关系、正文、来源位置、文本哈希、质量决策和表格表头传播。人工放行的隔离文档保留原质量状态与放行审计，但允许继续处理。

## Voyage Adapter

- 默认模型为 `voyage-4`，输出维度为 1024。
- 文档嵌入固定使用 `input_type=document`；阶段 7 的查询嵌入应使用 `input_type=query`。
- 按最大条数与估算 Token 双重限制动态分批。
- 429、5xx 和网络错误使用有抖动的指数退避；4xx 契约错误立即失败。
- 返回结果按 `index` 复原输入顺序，并严格校验数量和向量维度。
- `EmbeddingRun` 保存模型、配置、总量、Provider Token 和估算成本；`EmbeddingBatch` 保存每批节点、Token、重试和错误。
- Retrieval Node 保存向量及 `provider/model/dimension/config_version/retrieval_text_hash/embedded_at`。因此 OpenSearch 丢失后无需再次调用 Voyage 即可重建。

价格不会硬编码。只有配置 `VOYAGE_EMBEDDING_PRICE_PER_MILLION_TOKENS` 后才计算估算成本，以避免供应商价格调整后产生错误账目。

## OpenSearch 投影

默认物理索引和别名：

```text
rag-documents-read → rag-documents-v1
rag-chunks-read    → rag-chunks-v1
rag-chunks-write   → rag-chunks-v1
```

启动索引阶段时会读取集群版本和插件列表，并要求 k-NN 与 ICU Analysis 可用。Neural Search 能力会记录，但阶段 6 不依赖它。Aiven 服务需要启用对应版本兼容的插件，并使用 TLS、CA 验证和最小权限账号。

Chunk Mapping 使用：

- `icu_analyzer` 作为中英混合主分析器，同时提供 `.icu`、`.standard` 和必要的 `.keyword` 子字段。
- `embedding` 使用 1024 维 `knn_vector`、Faiss HNSW 和 cosine space。
- 文档、版本、Parent/相邻节点、来源定位、质量、Embedding 模型和更新时间均为严格字段；未知字段会被拒绝。
- 读别名带 `is_active=true` 过滤器。批量写入和数量核对完成前，新投影不可见。
- `_id` 使用稳定的 Version ID 或 Node ID，重复执行覆盖已有文档，不创建重复记录。

Adapter 提供 BM25 与 Dense 低层检索契约供验收和阶段 7 复用；RRF、查询改写、多样性和 Reranker 仍属于阶段 7。

## 删除、重建与别名切换

```text
GET    /api/v1/system/search-capabilities
POST   /api/v1/system/search-indexes/rebuild
POST   /api/v1/system/search-indexes/switch
POST   /api/v1/documents/{document_id}/search-projection/rebuild
DELETE /api/v1/documents/{document_id}/search-projection
DELETE /api/v1/documents/{document_id}
```

- 全量或单文档重建只读取 PostgreSQL 中 READY 当前版本和已保存向量。
- 删除业务文档会先传播删除到文档/Chunk 索引，再把 PostgreSQL 文档标记为 `deleted` 并清空当前版本。
- 新业务版本成功后，先激活新投影，再删除并验证旧版本投影，最后原子提交 READY/current version 状态。
- Alias 切换接口只接受已存在的物理索引，使用 OpenSearch `_aliases` 一次请求完成读写别名切换。

这些是本机管理接口。项目尚未实现认证和 ACL，因此服务必须继续仅监听 `127.0.0.1`。

## 配置

完整变量见 `.env.example`，关键项如下：

```text
VOYAGE_API_KEY
VOYAGE_EMBEDDING_MODEL=voyage-4
VOYAGE_EMBEDDING_DIMENSION=1024
VOYAGE_EMBEDDING_CONFIG_VERSION=stage6-voyage-v1
OPENSEARCH_URL
OPENSEARCH_USERNAME
OPENSEARCH_PASSWORD
OPENSEARCH_CA_CERT
OPENSEARCH_INDEX_CONFIG_VERSION=stage6-opensearch-v1
```

缺少 Voyage Key 或 OpenSearch URL 时，Worker 不会伪造成功，也不会静默跳过；Job 会保存明确的不可重试配置错误。

## 数据库迁移

迁移文件为 `20260817_0006_stage6_search_projection.py`。它新增：

- `embedding_runs`
- `embedding_batches`
- `indexing_runs`
- Retrieval Node 的向量及版本字段

实现阶段只生成并离线校验迁移；需要变更本机 PostgreSQL 时运行：

```bash
make migrate
make migration-check
```

## 验证

自动化测试使用 Voyage 与 OpenSearch 契约替身，不访问外部服务、不产生费用。覆盖：

- Voyage 请求字段、返回顺序和维度。
- 动态批次、429 重试、Token/成本审计和不可重试失败。
- 重复 Embedding/Indexing 不重复调用或生成文档。
- READY 文档 BM25 与 Dense 命中。
- 物理索引删除后的 PostgreSQL 全量重建。
- v1 到 v2 的三个 Alias 切换。
- 删除传播和 Worker 的 `CHUNK_EVALUATING → EMBEDDING → INDEXING → READY` 串联。

真实 Aiven/Voyage 联调需要有效凭据；本仓库当前没有 `.env`，因此本阶段没有向外部服务发送请求。
