# Robust RAG Backend

FastAPI API、SQLAlchemy/Alembic、LocalFileStorage、Celery Worker、Parser Router、Canonical Document、结构感知 Cleaning Pipeline、QualityEngine、父子分块、Voyage Embedding、OpenSearch 检索投影、混合召回、Reranker 与 Retrieval Trace 实现。完整项目说明见仓库根目录的 `README.md`。

常用命令从仓库根目录执行：

```bash
make migrate
make api
make worker
make migration-check
```

PDF、DOC/DOCX、PPT/PPTX、HTML/HTM 使用 MinerU 云端精准 API，需要在 `.env` 中配置 `MINERU_TOKEN`。XLSX、Markdown、TXT 保持本地解析；旧版 XLS 需要 `soffice` 转换。

清洗阶段不调用外部服务。每次运行使用原始 Canonical 产物作为输入，生成独立的清洗后 Canonical 文件和 `cleaning-report/1.0` 报告；同一输入、流水线版本和配置版本会幂等复用。

质量阶段执行 Schema、确定性规则、可选 Dingo 规则/LLM 和独立准入策略，生成 `quality-report/1.0`。Dingo 默认关闭；需要时使用 `uv sync --all-groups --extra dingo` 安装锁定的 `dingo-python==2.5.0`，再配置 `DINGO_ENABLED=true`。

分块阶段按章节、Slide 和 Logical Table 生成 Parent/Child Retrieval Node，表格 Child 自动携带表头，普通 Child 的 Token 重叠仅发生在同一 Parent 内。

阶段 6 对 Retrieval Node 执行确定性门禁，然后使用 Voyage `voyage-4` 分批生成版本化向量，并将 READY 当前版本投影到带 ICU 与 k-NN Mapping 的 OpenSearch 索引。向量保存在 PostgreSQL，可在物理索引丢失后无外部 Embedding 调用完成重建。运行前必须配置 `VOYAGE_API_KEY`、`OPENSEARCH_URL`、TLS CA 和最小权限账号；详见 `docs/STAGE6_EMBEDDING_OPENSEARCH.md`。

阶段 7 提供 BM25、Dense、Hybrid 和 Hybrid+Rerank 四种检索模式，以应用层 RRF 和多样性控制融合候选，使用 Voyage `rerank-2.5` 可降级重排，并按 Token 预算恢复 Parent/相邻 Child。每次检索持久化完整 Retrieval Trace；详见 `docs/STAGE7_RETRIEVAL.md`。

阶段 8 提供有来源回答、SSE、多轮会话与模型调用审计；阶段 9 提供版本化知识图谱、Neo4j 可重建投影、受控 Text-to-Cypher、OpenSearch 回退和人工审核领域 API；阶段 10 补齐文档生命周期、图谱合并/拆分/纠错/冲突处理以及管理端所需 API。详见 `docs/STAGE8_GENERATION.md`、`docs/STAGE9_KNOWLEDGE_GRAPH.md` 与 `docs/STAGE10_ADMIN_UI.md`。
