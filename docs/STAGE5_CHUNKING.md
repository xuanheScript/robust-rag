# 阶段 5：结构感知父子分块

阶段 5 将通过文档质量门禁的清洗后 Canonical Document 转换为与解析器、LlamaIndex 和索引引擎解耦的 `retrieval-node/1.0`。Child 用于后续召回，Parent 用于恢复完整上下文。

## 结构策略

- PDF、Word、HTML、Markdown 优先按标题路径形成章节 Parent；没有可靠标题时使用页面或文档根边界。
- PowerPoint 按 Slide 聚合内容，超长内容仍只在 Slide 内拆分。
- Excel 按 Logical Table 创建 Parent。
- 表格在 Parent 上限内保留完整逻辑表，超长表按行分片；每个 Parent/Child 均重复表头。
- 普通 Child 只在同一 Parent 内产生重叠，不跨章节、Slide 或逻辑表。
- 标题、标题路径、内容类型和页码/Slide/Sheet/Cell/行号上下文写入 `retrieval_text`。

默认窗口：

```dotenv
CHUNKING_CONFIG_VERSION=stage5-parent-child-v2
CHUNKING_PARENT_TARGET_TOKENS=1800
CHUNKING_PARENT_MAX_TOKENS=2500
CHUNKING_CHILD_TARGET_TOKENS=500
CHUNKING_CHILD_MAX_TOKENS=600
CHUNKING_CHILD_OVERLAP_TOKENS=64
```

短章节不会为了达到目标长度而跨结构合并。长段落先受 Parent 最大 Token 限制，Child 优先在目标长度附近寻找句末，且不会超过最大长度。表格 Child 不使用行重叠，以免重复业务数据。

## 稳定身份与来源

Parent Node ID 基于 DocumentVersion、Chunker/配置版本、顺序和内容哈希生成 UUIDv5；Child ID 基于 Parent、Child 顺序和内容哈希生成。同一不可变版本、相同输入和配置重复执行会得到相同 Node ID。

每个节点保存：

- Parent、前一节点和后一节点 ID。
- 来源 Canonical Block ID。
- 合并去重后的 SourceLocator。
- 标题路径、内容类型、语言和 Token 数。
- 文档质量决策、质量摘要和是否人工放行。
- `retrieval_text` 及其 SHA-256。
- 待后续阶段填写的 Embedding 和索引状态。

## 持久化与状态

- `chunking_runs` 保存输入哈希、Chunker/配置版本、运行状态、统计和产物地址。
- `retrieval_nodes` 保存当前版本化分块投影。
- 完整节点产物使用 `chunking-artifact/1.0`，报告使用 `chunking-report/1.0`。
- 产物路径为 `chunks/{document_id}/{version_id}/{chunking_run_id}/`。
- 相同成功输入幂等复用；失败重试创建新的审计运行，不会被唯一约束阻塞。

质量为 `passed` 或 `warning` 的文档可以分块；`quarantined` 只有存在明确人工放行审计时才可继续，`rejected` 无法绕过门禁。分块成功后任务进入 `chunk_evaluating`，阶段 6 再接续节点质量与 Embedding/索引链路。

## API

```text
GET /api/v1/documents/{document_id}/versions/{version_id}/chunking-runs
GET /api/v1/documents/{document_id}/versions/{version_id}/chunking-runs/{run_id}/artifact
GET /api/v1/documents/{document_id}/versions/{version_id}/chunking-runs/{run_id}/report
GET /api/v1/documents/{document_id}/versions/{version_id}/retrieval-nodes
GET /api/v1/documents/{document_id}/versions/{version_id}/retrieval-nodes/{node_id}
```

节点列表支持 `node_level=parent|child`、`parent_node_id`、`limit` 和 `offset`，可以从 Child 直接查询其 Parent 和相邻节点。

## 迁移与验证

```bash
make migrate
make check
```

默认测试不调用任何外部服务，覆盖章节隔离、Parent 内重叠、表头传播、来源恢复、稳定 ID、持久化、API、人工放行、幂等和失败重试。
