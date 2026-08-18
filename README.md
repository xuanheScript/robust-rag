# Robust RAG

面向中英双语通用企业知识库的完整 RAG 项目。阶段 12 正在实施，已接入默认启用、可失败降级且默认不采集正文的 Langfuse Cloud 可观测层，并补充 HTTP/Celery Trace、运行时健康状态与任务恢复加固。

完整实施方案见 [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)。

## 当前工程组成

- `backend/`：FastAPI、SQLAlchemy/Alembic、Celery Worker、完整入库流水线、OpenSearch 检索、RAG Generation、Neo4j 图谱、管理 API、配置、日志与测试。
- `frontend/`：Vite、React 19、TypeScript、AI Elements 源码适配，以及总览、文档、任务、Chat、图谱和系统状态页面。
- `data/`：本地原始文件和派生产物目录，运行时内容不提交 Git。
- `evals/`：黄金集、Rubric 与评测报告。

## 本机前置条件

- Python 3.12
- uv
- Node.js 22+
- pnpm 10+
- PostgreSQL 17
- Redis 8（Celery Broker）
- PDF、Word、PowerPoint、HTML 解析需要 MinerU 云端精准 API Token；旧版 XLS 建议安装 LibreOffice

## 初始化

```bash
cp .env.example .env
make setup
createdb robust_rag
make migrate
```

如果 `robust_rag` 数据库已经存在，可以跳过 `createdb`。迁移是幂等的，后续更新继续运行 `make migrate`。

## 启动

分别在三个终端运行：

```bash
make api
make worker
make web
```

- API：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>
- 前端：<http://127.0.0.1:5173>
- 存活检查：<http://127.0.0.1:8000/health/live>
- 就绪检查：<http://127.0.0.1:8000/health/ready>
- 依赖与降级状态：<http://127.0.0.1:8000/health/dependencies>
- Langfuse 状态：<http://127.0.0.1:8000/health/observability>

## 上传与任务

```bash
curl -F 'file=@./example.pdf' -F 'display_name=示例文档' \
  http://127.0.0.1:8000/api/v1/documents/uploads
```

上传成功立即返回不可变文档版本和持久化 Job。Worker 会连续完成解析、清洗、文档质量评估、结构感知父子分块、节点门禁、Embedding 和 OpenSearch 索引；只有投影写入与数量校验成功后文档才进入 `ready`，高风险文档会隔离或拒绝。各阶段产物、向量、报告和同步审计均会持久化。

- 同一业务文档相同 SHA-256 会返回 `DUPLICATE_VERSION`。
- 不同文档的相同内容会返回 `DUPLICATE_CONTENT`，明确传入 `allow_duplicate_content=true` 才允许保留。
- Worker 或 Redis 短暂中断不会丢失 Job；PostgreSQL 保留最终状态，恢复扫描可重新投递。
- PDF、DOC/DOCX、PPT/PPTX、HTML/HTM 通过 MinerU 云端精准 API 解析，必须配置 `MINERU_TOKEN`；原件会上传至 MinerU 云端。
- XLSX 使用本地结构化解析器，XLS 通过 LibreOffice 转为 XLSX；Markdown、TXT 使用本地解析器。
- MinerU 失败时会记录明确错误和可重试性，不会静默切换到 Agent 轻量 API。

解析完成后可以读取结果：

```text
GET /api/v1/documents/{document_id}/versions/{version_id}/parse-runs
GET /api/v1/documents/{document_id}/versions/{version_id}/canonical/metadata
GET /api/v1/documents/{document_id}/versions/{version_id}/canonical
GET /api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs
GET /api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs/{run_id}/document
GET /api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs/{run_id}/report
GET /api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs/{run_id}/compare?against_run_id={run_id}
GET /api/v1/documents/{document_id}/quality
GET /api/v1/documents/{document_id}/versions/{version_id}/quality-assessments
GET /api/v1/documents/{document_id}/versions/{version_id}/quality-assessments/{assessment_id}/report
GET /api/v1/documents/{document_id}/quality/review-actions
POST /api/v1/documents/{document_id}/release
POST /api/v1/documents/{document_id}/reject
POST /api/v1/documents/{document_id}/quality/re-evaluate
POST /api/v1/documents/{document_id}/reprocess
POST /api/v1/documents/{document_id}/restore
DELETE /api/v1/documents/{document_id}/purge
GET /api/v1/documents/{document_id}/versions/{version_id}/chunking-runs
GET /api/v1/documents/{document_id}/versions/{version_id}/chunking-runs/{run_id}/artifact
GET /api/v1/documents/{document_id}/versions/{version_id}/chunking-runs/{run_id}/report
GET /api/v1/documents/{document_id}/versions/{version_id}/retrieval-nodes
GET /api/v1/documents/{document_id}/versions/{version_id}/retrieval-nodes/{node_id}
GET /api/v1/documents/{document_id}/versions/{version_id}/embedding-runs
GET /api/v1/documents/{document_id}/versions/{version_id}/indexing-runs
GET /api/v1/system/search-capabilities
POST /api/v1/system/search-indexes/rebuild
POST /api/v1/system/search-indexes/switch
POST /api/v1/documents/{document_id}/search-projection/rebuild
DELETE /api/v1/documents/{document_id}/search-projection
DELETE /api/v1/documents/{document_id}
POST /api/v1/retrieval/search
GET /api/v1/retrieval/traces
GET /api/v1/retrieval/traces/{trace_id}
POST /api/v1/evaluations
GET  /api/v1/evaluations
GET  /api/v1/evaluations/{evaluation_id}
```

阶段 2 的设计和运行说明见 [docs/STAGE2_PARSING.md](docs/STAGE2_PARSING.md)。
阶段 3 的设计和运行说明见 [docs/STAGE3_CLEANING.md](docs/STAGE3_CLEANING.md)。
阶段 4 的设计和运行说明见 [docs/STAGE4_QUALITY.md](docs/STAGE4_QUALITY.md)。
阶段 5 的设计和运行说明见 [docs/STAGE5_CHUNKING.md](docs/STAGE5_CHUNKING.md)。
阶段 6 的设计和运行说明见 [docs/STAGE6_EMBEDDING_OPENSEARCH.md](docs/STAGE6_EMBEDDING_OPENSEARCH.md)。
阶段 7 的设计和运行说明见 [docs/STAGE7_RETRIEVAL.md](docs/STAGE7_RETRIEVAL.md)。
阶段 8 的生成、会话和引用说明见 [docs/STAGE8_GENERATION.md](docs/STAGE8_GENERATION.md)。
阶段 9 的知识图谱构建与检索说明见 [docs/STAGE9_KNOWLEDGE_GRAPH.md](docs/STAGE9_KNOWLEDGE_GRAPH.md)。
阶段 10 的管理后台、Chat UI 与生命周期操作说明见 [docs/STAGE10_ADMIN_UI.md](docs/STAGE10_ADMIN_UI.md)。
阶段 11 的黄金集、Ragas、确定性/图谱指标与回归报告说明见 [docs/STAGE11_EVALUATION.md](docs/STAGE11_EVALUATION.md)。
阶段 12 的 Langfuse、Trace、健康检查、恢复和故障处理说明见 [docs/STAGE12_OBSERVABILITY.md](docs/STAGE12_OBSERVABILITY.md)。

## 工程检查

```bash
make check
```

默认检查不会调用 Voyage、GPT、MinerU、Dingo 或 Aiven，不产生外部服务费用。
