# 阶段 2：解析与 Canonical Document

## 1. 完成范围

阶段 2 已打通从不可变原始文件到可重放 Canonical JSON 的完整后台链路：

```text
Original Asset
  → Parser Router
  → Format Adapter
  → Parse Artifact (parse-artifact/1.0)
  → Canonicalizer
  → Canonical Document (canonical-document/1.0)
  → Cleaning（阶段 3）
```

PostgreSQL 保存 ParseRun、CanonicalDocumentRecord、Job 和 StageRun；LocalFileStorage 保存 Parse Artifact 与 Canonical JSON。Redis 只负责 Celery 消息，不保存唯一事实。

## 2. 格式路由

| 输入 | Parser | 关键结构与定位 |
| --- | --- | --- |
| PDF | MinerU 云端精准 API | Page、BBox、标题层级、正文、表格、公式、代码、列表、脚注、原生标题文字 |
| DOC/DOCX | MinerU 云端精准 API | 页面、标题、段落、列表、表格和公式；云端负责旧格式转换 |
| PPT/PPTX | MinerU 云端精准 API | Slide/Page、标题、文本框、表格和公式 |
| XLSX | openpyxl | 可见 Sheet、逻辑表、Display Value、Formula、Cell Range；隐藏 Sheet 排除 |
| XLS | LibreOffice → XLSX Parser | 转换来源写入 Parse Artifact；不可用或转换失败时显式失败 |
| HTML/HTM | MinerU 云端精准 API（MinerU-HTML） | 从结果 `main.html` 生成标题、段落、列表、表格、引用、代码、链接和 DOM Path |
| Markdown | markdown-it-py | 标题、段落、嵌套列表、代码与行号/字符范围 |
| TXT | 原生文本 Parser | 段落、行号与字符范围 |

Router 同时校验 MIME、扩展名与文件签名，不只信任文件名。第一期不进行图片理解或图片文本化；PDF 中已有的文字标题可以保留，图片主体不会进入检索文本。

## 3. MinerU 边界

项目使用 MinerU 云端**精准解析 API**，不部署本地 MinerU，也不使用免 Token 的 Agent 轻量解析 API。PDF、DOC/DOCX、PPT/PPTX、HTML/HTM 原件会上传至 MinerU 云端。精准 API 当前官方格式列表没有 XLS/XLSX、Markdown、TXT，因此这些格式保持本地解析。

管理后台上传的本地文件采用精准 API 的签名上传流程：

```text
POST /api/v4/file-urls/batch
  → PUT signed file_url
  → GET /api/v4/extract-results/batch/{batch_id}
  → GET full_zip_url
```

只有 MinerU API 请求携带 `Authorization: Bearer <MINERU_TOKEN>`；签名上传和 CDN 结果下载不得携带 Token。Adapter 对非 HTML 读取官方 `content_list.json`，对 HTML 读取 `main.html`，保存为内部 Parse Artifact 后再生成 Canonical Document。

选择 v1 Content List 的原因：它提供按阅读顺序排列的扁平内容块，并包含 `page_idx` 和 `bbox`；V2 不进入内部长期契约，MinerU 升级只影响 Adapter。

配置：

```dotenv
MINERU_BASE_URL=https://mineru.net/api/v4
MINERU_TOKEN=
MINERU_TIMEOUT_SECONDS=600
MINERU_POLL_INTERVAL_SECONDS=3
MINERU_MODEL_VERSION=vlm
MINERU_OCR_ENABLED=true
```

Token 缺失、鉴权失败、上传失败、轮询超时、远程任务失败或结果格式异常时，Job 会进入 FAILED 并记录明确错误；不会静默降级到 Agent 轻量 API 或本地低质量解析器。初期文件量使用 Worker 轮询，后续吞吐量提升时再引入持久化远程任务状态与回调。

官方参考：

- [MinerU 云端 API 文档](https://mineru.net/doc/docs/)
- [MinerU API Token](https://mineru.net/apiManage/token)
- [MinerU Output Files](https://opendatalab.github.io/MinerU/reference/output_files/)

## 4. Canonical Contract

Canonical Document 与任何具体 Parser、LlamaIndex 或 OpenSearch 数据结构解耦。每个 Block 同时保存：

- 稳定 ID、父级、同级前后关系和语义顺序；
- `original_text` 与 `normalized_text`；
- 标题路径、语言、Token 估算和 Parser 置信度；
- SourceLocator：页码/BBox、Slide/Shape、Word 段落/表格、Sheet/Cell、DOM Path 或行号；
- 质量状态与质量标记占位，供阶段 4 QualityEngine 使用。

同一 DocumentVersion 和相同 Parse Artifact 重放会生成相同 Block ID。Canonical JSON 使用稳定序列化并保存 SHA-256，可检测非预期变化。

## 5. 持久化与幂等

- ParseRun 记录 Parser 名称、版本、模式、配置、状态、产物和错误。
- StageRun 记录解析阶段输入原件和输出 Canonical URI。
- CanonicalDocumentRecord 记录 Schema、标题、语言、Block 数、URI 和内容哈希。
- PostgreSQL advisory lock 串行化同一 Job 的跨进程解析；Celery 重投不会产生并发重复解析。
- 已存在同版本 Canonical Record 时直接推进到 Cleaning，不再次调用 Parser。
- 原始文件、Parse Artifact 和 Canonical JSON 均保留，可以独立重放后续阶段。

## 6. API

```text
GET /api/v1/documents/{document_id}/versions/{version_id}/parse-runs
GET /api/v1/documents/{document_id}/versions/{version_id}/canonical/metadata
GET /api/v1/documents/{document_id}/versions/{version_id}/canonical
```

## 7. 验收结果

- 29 个后端测试全部通过，总覆盖率 89%。
- Ruff、mypy strict、Alembic check 全部通过。
- 已在本机 PostgreSQL、Redis 和 Celery 上完成真实 Markdown 上传解析：Job 从 Parsing 推进到 Cleaning，生成 7 个可追溯 Block，并通过 API 读取 Canonical JSON。
- E2E 临时数据库记录与产物已清理，仓库只保留可重复 Fixture。

阶段 3 从 Canonical Document 开始实现可插拔 Cleaning Pipeline，不回写或覆盖 `original_text`。
