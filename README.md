# Robust RAG

面向中英双语通用企业知识库的完整 RAG 项目。阶段 2（Parser Router、各格式解析与 Canonical Model）已完成，下一步进入阶段 3 的 Cleaning Pipeline。

完整实施方案见 [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)。

## 当前工程组成

- `backend/`：FastAPI、SQLAlchemy/Alembic、Celery Worker、Parser Router、Canonical Model、配置、日志与测试。
- `frontend/`：Vite、React 19、TypeScript 与前端测试基线。
- `data/`：本地原始文件和派生产物目录，运行时内容不提交 Git。
- `evals/`：黄金集、Rubric 与评测报告。

## 本机前置条件

- Python 3.12
- uv
- Node.js 22+
- pnpm 10+
- PostgreSQL 17
- Redis 8（Celery Broker）
- PDF 解析另需可访问的 `mineru-api`；旧版 Office 文件建议安装 LibreOffice

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

## 上传与任务

```bash
curl -F 'file=@./example.pdf' -F 'display_name=示例文档' \
  http://127.0.0.1:8000/api/v1/documents/uploads
```

上传成功立即返回不可变文档版本和持久化 Job。Worker 完成解析后，Job 会推进到 `cleaning`，等待阶段 3；Parse Artifact 和 Canonical JSON 均已持久化。

- 同一业务文档相同 SHA-256 会返回 `DUPLICATE_VERSION`。
- 不同文档的相同内容会返回 `DUPLICATE_CONTENT`，明确传入 `allow_duplicate_content=true` 才允许保留。
- Worker 或 Redis 短暂中断不会丢失 Job；PostgreSQL 保留最终状态，恢复扫描可重新投递。
- PDF 路由要求配置 `MINERU_BASE_URL`；未配置时任务以可解释的 `MINERU_UNAVAILABLE` 失败。
- DOC、PPT、XLS 通过 LibreOffice 转换；DOCX、PPTX、XLSX、HTML、Markdown、TXT 使用本地解析器。

解析完成后可以读取结果：

```text
GET /api/v1/documents/{document_id}/versions/{version_id}/parse-runs
GET /api/v1/documents/{document_id}/versions/{version_id}/canonical/metadata
GET /api/v1/documents/{document_id}/versions/{version_id}/canonical
```

阶段 2 的设计和运行说明见 [docs/STAGE2_PARSING.md](docs/STAGE2_PARSING.md)。

## 工程检查

```bash
make check
```

默认检查不会调用 Voyage、GPT、MinerU、Dingo 或 Aiven，不产生外部服务费用。
