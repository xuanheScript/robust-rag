# 阶段 9：知识图谱构建与检索

阶段 9 在 OpenSearch 主检索链路之外增加可选的、最终一致的知识图谱增强。文档完成索引后会立即进入 `READY` 并可用于全文检索，但不会自动调用 LLM 构建图谱。管理员在文档页选择一个或多个 `READY` 文档、确认 Parent 数量与预估输入成本后，才会创建图谱任务。图谱抽取、Neo4j 投影或查询失败只记录独立状态，不影响 OpenSearch。

## 已实现范围

- 版本化 `enterprise-core-v1` Schema，包含 9 类实体、11 类关系及明确的允许三元组。
- 中英文名称 NFKC/大小写/标点归一化、受控别名和 UUIDv5 稳定实体/事实 ID。
- LlamaIndex `PropertyGraphIndex` 与 `SchemaLLMPathExtractor(strict=True)`；抽取输入限定为 Parent 节点。
- 可配置 Responses API Structured Output 适配器，不向 LlamaIndex 或前端暴露上游凭据。
- PostgreSQL 权威记录：抽取 Run、实体、事实、多来源证据、人工修正审计和 Text-to-Cypher Trace。
- Neo4j 约束、索引、健康检查、幂等投影、版本隐藏、永久证据清理和全量重建接口。
- LlamaIndex `TextToCypherRetriever` 通过应用网关执行，生成结果不能绕过校验器。
- 图谱 Source Node 与 BM25/Dense 候选共同进入 RRF、去重、Voyage Rerank、上下文和引用链。
- 实体搜索、局部邻域、实体/关系新增、事实确认/驳回和重建领域 API。
- 人工批量生成入口、生成前成本预览、持久化 `GraphBuildRequest`、有界恢复和完整状态审计。

## 首版 Schema

实体类型：`ORGANIZATION`、`PERSON`、`PRODUCT`、`SYSTEM`、`PROCESS`、`POLICY`、`STANDARD`、`LOCATION`、`PROJECT`。

关系类型：`WORKS_FOR`、`MANAGES`、`OWNS`、`PART_OF`、`DEPENDS_ON`、`USES`、`PRODUCES`、`APPLIES_TO`、`COMPLIES_WITH`、`LOCATED_IN`、`RELATED_TO`。

允许组合在 `backend/src/robust_rag/graph/schema.py` 中作为不可变版本契约。Schema 外候选只进入抽取产物的 `rejected_candidates`，不会写入在线事实。生产启用前仍需用目标企业的首批文档完成术语盘点和黄金集校准；任何 Schema 变更都创建新版本，不能原地修改历史契约。

## 数据与一致性

PostgreSQL 和文件产物是权威来源，Neo4j 是查询投影：

1. Parent 节点按 `source_node_id` 送入严格抽取器。
2. 每个三元组再次经过当前 Schema 校验。
3. 实体与事实按稳定键 upsert，同一事实可关联多个 `GraphFactEvidence`。
4. 每条自动事实至少存在一个可定位 Retrieval Node；无证据事实不会进入问答结果。
5. 新文档版本投影成功后隐藏旧版本证据。仍被其他版本支持或人工创建的事实不会被删除。
6. 人工实体、别名和事实带 `manual_lock`；自动重抽取只能补充未锁定记录，不能覆盖人工审核结论。
7. 自动抽取与人工锁定事实冲突时写入 `GraphConflictRecord`，保留当前值、提议值和抽取 Run；后台通过 `/graph/conflicts` 查看待处理项。

`GraphBuildRequest` 保存一次明确的人工授权，包括批次、目标文档版本、生成/重建/重试类型、请求人、预估 Parent/Token/费用、实际 Token/费用、任务 ID、尝试次数和错误。只有 `PENDING` 请求能启动 `graph.extract`；旧任务 ID、重复投递以及已经取消的请求都不会进入 LLM。

`GraphExtractionRun` 为每次执行保留递增的 `attempt`，并关联对应的 `GraphBuildRequest`。相同文档版本、Schema、抽取器版本和输入哈希仍共享同一幂等范围，但强制重建不会覆盖旧日志、错误和 token 使用；实体、事实和证据继续按稳定键幂等写入。

每个 Parent 节点都有独立抽取结果。`usage_json.parent_outcomes` 保存来源节点、成功/失败、耗时、token、响应 ID 和安全错误摘要；单节点失败会被隔离，失败比例超过配置阈值时整次 Run 才失败。Worker 被终止后，Run 会立即记录失败；无法正常收尾的僵死 Run 只会在原人工请求的持久化尝试上限内重新入队，不会凭空创建新的付费任务。

图谱投影状态使用 `NOT_REQUESTED / PENDING / RUNNING / SUCCEEDED / FAILED / STALE / HIDDEN`。`graph_active` 单独表示当前投影是否仍能查询，因此“重建失败但上一次投影仍可用”会表现为 `graph_status=FAILED, graph_active=true`。软删除转为 `HIDDEN`；若证据仍在，恢复文档直接恢复投影，不再次调用 LLM。版本切换、重新处理或删除发生在任务执行期间时，请求转为 `CANCELLED`，晚到结果也会被撤下，不能重新激活旧版本。

## 受控 Text-to-Cypher

网关按以下顺序执行：

1. 词法扫描字符串、转义标识符、参数、注释和符号。
2. 拒绝多语句、写入/管理子句、非白名单函数、Label、Relationship 和 Property。
3. 变长路径必须有上下界且最大深度为 3。
4. `LIMIT` 缺失时补 50，超过 50 时收紧；只接受字面整数。
5. 单节点查询必须有 `WHERE`，并强制投影 `source_node_id`。
6. 执行 `EXPLAIN`，拒绝笛卡尔积、无约束扫描和超过估算行数预算的计划。
7. Text-to-Cypher 使用独立 8 秒 LLM 超时；Neo4j 连接、`EXPLAIN` 和查询使用 3 秒超时。
8. 校验、计划、执行、空结果或缺少来源任一失败时返回空图候选，OpenSearch 继续服务。

Langfuse 将图谱检索拆分为 `llm.text_to_cypher`、`graph.neo4j.explain` 和
`graph.neo4j.query`，用于区分模型生成、执行计划和实际查询耗时。
图谱构建中的每个 Parent 节点另有独立的 `llm.graph_extraction` Generation，包含模型参数、来源节点 ID、耗时、token、响应 ID 和结构化输出校验状态。

普通 HTTP API 不接收任意 Cypher。

## Chat 查询边界

Agent 选择 `retrieve_enterprise_relationships` 后，直接查询项目内全局有效图谱，并与 OpenSearch 候选按既有策略融合或回退。Chat 不会在每次请求前查询“有多少文档已生成图谱”，也不按当前问题额外检查文档级图谱覆盖率；文档是否人工生成过图谱只决定 Neo4j 中是否存在它的有效证据，不改变工具选择和调用流程。

## 配置

图谱默认关闭。设置以下值后可以在管理端人工创建 `graph.extract` 任务；文档索引完成本身不会投递图谱任务：

```dotenv
GRAPH_ENABLED=true
GRAPH_SCHEMA_VERSION=enterprise-core-v1
GRAPH_EXTRACTOR_VERSION=llama-schema-v3
GRAPH_PROMPT_VERSION=stage9-extraction-v3
GRAPH_MAX_TRIPLETS_PER_PARENT=12
GRAPH_EXTRACTION_WORKERS=2
GRAPH_LLM_REASONING_EFFORT=none
GRAPH_LLM_MAX_OUTPUT_TOKENS=8000
GRAPH_LLM_MAX_RETRIES=3
GRAPH_LLM_RETRY_BASE_SECONDS=2
GRAPH_LLM_RETRY_MAX_SECONDS=15
GRAPH_MAX_FAILED_PARENT_RATIO=0.2
GRAPH_RUN_STALE_SECONDS=900
GRAPH_BUILD_MAX_ATTEMPTS=2
GRAPH_QUERY_ENABLED=true
GRAPH_QUERY_MAX_DEPTH=3
GRAPH_QUERY_MAX_ROWS=50
GRAPH_QUERY_TIMEOUT_SECONDS=3
GRAPH_TEXT_TO_CYPHER_TIMEOUT_SECONDS=8
GRAPH_RRF_WEIGHT=0.8
NEO4J_URL=neo4j+s://<aura-id>.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<secret>
NEO4J_DATABASE=neo4j
```

还需配置阶段 8 的 `LLM_BASE_URL`、`LLM_API_KEY` 与 `LLM_MODEL`。凭据仅存在后端环境变量中；缺少 API Key 时图谱抽取会返回明确配置错误，不发送匿名请求。

## 迁移和运行

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run celery -A robust_rag.workers.celery_app:celery_app worker --loglevel=INFO
uv run uvicorn robust_rag.main:app --host 127.0.0.1 --port 8000
```

`GET /health/dependencies` 返回必需服务和可选图谱状态。图谱未配置时为 `graph.status=disabled`，不影响 `/health/ready`；配置后会报告 Neo4j 连接和只读查询健康状态。

## API

```text
GET    /api/v1/graph/search?q=...
GET    /api/v1/graph/entities/{entity_id}
GET    /api/v1/graph/entities/{entity_id}/neighborhood
POST   /api/v1/graph/entities
PATCH  /api/v1/graph/entities/{entity_id}
POST   /api/v1/graph/relations
POST   /api/v1/graph/facts/{fact_id}/approve
POST   /api/v1/graph/facts/{fact_id}/reject
GET    /api/v1/graph/conflicts
POST   /api/v1/graph/builds/preview
POST   /api/v1/graph/builds
GET    /api/v1/graph/builds/{batch_id}
GET    /api/v1/documents/{document_id}/versions/{version_id}/graph-runs
POST   /api/v1/documents/{document_id}/graph/rebuild
```

`/graph/builds/preview` 返回逐文档可执行性、Parent 数、估算输入 Token 和可选费用；`/graph/builds` 仅接受当前 `READY` 版本，并为每份文档持久化一个请求。旧的单文档 `/graph/rebuild` 作为兼容入口，内部也使用相同的人工请求状态机。邻域接口默认局部展开并受行数限制，不提供全图渲染或数据库直通。

## 验证

阶段测试覆盖 Schema/稳定键、危险 Cypher、安全边界、LlamaIndex 严格配置、Structured Output、人工任务授权、重复投递隔离、取消与晚到结果、无自动投递、有界恢复、幂等抽取、多来源证据、投影失败、Text-to-Cypher Trace、Neo4j 回退、统一 Rerank、人工锁和管理 API。

默认测试不连接 AuraDB 或调用付费模型。真实 AuraDB 与 LLM API 抽取验证需在显式配置的集成环境中运行。
