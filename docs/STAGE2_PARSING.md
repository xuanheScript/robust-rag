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
| PDF | MinerU HTTP Adapter | Page、BBox、标题层级、正文、表格、公式、代码、列表、脚注、原生标题文字 |
| DOCX | python-docx | 原始正文顺序、标题、段落、列表项、表格、段落/表格序号 |
| PPTX | python-pptx | Slide、标题、文本框、表格、Speaker Notes、Shape 序号 |
| XLSX | openpyxl | 可见 Sheet、逻辑表、Display Value、Formula、Cell Range；隐藏 Sheet 排除 |
| DOC/PPT/XLS | LibreOffice → OOXML Parser | 转换来源写入 Parse Artifact；不可用或转换失败时显式失败 |
| HTML | BeautifulSoup | 标题、段落、列表、表格、引用、代码、链接、DOM Path；移除导航和脚本等噪声 |
| Markdown | markdown-it-py | 标题、段落、嵌套列表、代码与行号/字符范围 |
| TXT | 原生文本 Parser | 段落、行号与字符范围 |

Router 同时校验 MIME、扩展名与文件签名，不只信任文件名。第一期不进行图片理解或图片文本化；PDF 中已有的文字标题可以保留，图片主体不会进入检索文本。

## 3. MinerU 边界

PDF 通过独立运行的 `mineru-api` 调用同步 `/file_parse` 接口。Adapter 请求 ZIP 输出，并读取官方定义的 `content_list.json`，保存为内部 Parse Artifact 后再生成 Canonical Document。

选择 v1 Content List 的原因：它提供按阅读顺序排列的扁平内容块，并包含 `page_idx` 和 `bbox`；官方目前将 `content_list_v2.json` 标记为开发版本、可能变化。因此 V2 不进入内部长期契约，MinerU 升级只影响 Adapter。

配置：

```dotenv
MINERU_BASE_URL=http://127.0.0.1:8001
MINERU_API_KEY=
MINERU_TIMEOUT_SECONDS=600
MINERU_BACKEND=vlm-auto-engine
```

未配置或 MinerU 不可用时，PDF Job 会进入 FAILED，并记录可重试错误；不会静默降级成低质量 PDF 抽取。

官方参考：

- [MinerU Quick Usage](https://github.com/opendatalab/MinerU/blob/master/docs/en/usage/quick_usage.md)
- [MinerU Output Files](https://opendatalab.github.io/MinerU/reference/output_files/)
- [MinerU Repository](https://github.com/opendatalab/MinerU)

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

- 27 个后端测试全部通过，总覆盖率 89%。
- Ruff、mypy strict、Alembic check 全部通过。
- 已在本机 PostgreSQL、Redis 和 Celery 上完成真实 Markdown 上传解析：Job 从 Parsing 推进到 Cleaning，生成 7 个可追溯 Block，并通过 API 读取 Canonical JSON。
- E2E 临时数据库记录与产物已清理，仓库只保留可重复 Fixture。

阶段 3 从 Canonical Document 开始实现可插拔 Cleaning Pipeline，不回写或覆盖 `original_text`。
