# 阶段 10：管理后台与 Chat UI

阶段 10 提供覆盖知识库日常运营的 Vite 管理端，并补齐文档生命周期、图谱人工治理和冲突处理 API。所有高风险操作都通过受控 API 完成，前端不暴露任意 SQL、Cypher、内部 Prompt 或服务凭据。

## 页面与核心流程

管理端包含六个入口：

- `/overview`：文档、可检索版本、处理中任务、失败任务和依赖健康状态。
- `/documents`：上传、搜索、状态筛选、版本与质量详情、放行/拒绝、重新评估、重处理、检索/图谱重建、软删除、恢复和永久删除。
- `/jobs`：任务状态、阶段进度、错误信息和失败任务重试。
- `/chat`、`/chat/:conversationId`：新建对话、历史、流式回答、停止、重新生成、Markdown/代码/表格、复制回答、来源抽屉和管理员 Trace。
- `/graph`：实体搜索、局部图、实体/关系新增与修改、事实审核、实体合并/拆分和冲突处理。
- `/system`：应用、PostgreSQL、Redis、OpenSearch 与 Neo4j 状态和搜索能力。

桌面端使用固定工作区布局；窄屏下导航、列表、详情抽屉和图谱操作会重新排列，不产生横向页面溢出。

## AI Elements 的 Vite 适配

AI Elements 按源码组件方式集成。`frontend/src/components/ai-elements/message.tsx` 是本项目可修改的 `MessageResponse` 适配层，使用 Streamdown 处理不完整的流式 Markdown、GFM 表格和代码块。

- Vite 使用 `@` 路径别名加载本地组件。
- Tailwind v4 扫描 Streamdown 组件源码并加载其动画样式。
- Chat 页面按需加载，避免 Markdown/图表依赖进入管理后台首屏包。
- 后端继续输出 AI SDK UI Message Stream v1；浏览器只消费公开 Data Part 和文本增量。

## 文档生命周期

新增或扩展的管理 API：

```text
GET    /api/v1/documents?q=&status=&include_deleted=
POST   /api/v1/documents/{document_id}/reprocess
POST   /api/v1/documents/{document_id}/restore
DELETE /api/v1/documents/{document_id}/purge
```

- 软删除先移除 OpenSearch 在线投影并隐藏 Neo4j 来源，业务数据和文件仍可恢复。
- 恢复会重建当前版本的 OpenSearch 投影；配置 Neo4j 时同步恢复图谱投影。
- 永久删除只允许作用于已软删除文档，并要求确认文本与文档显示名完全一致。
- 永久删除会清理可重建投影、派生产物、原始文件和 PostgreSQL 业务记录；共享图谱事实按其剩余证据保留。

## 图谱治理

阶段 10 在阶段 9 的图谱读写与审核 API 上新增：

```text
POST  /api/v1/graph/entities/merge
POST  /api/v1/graph/entities/{entity_id}/split
PATCH /api/v1/graph/relations/{relation_id}
POST  /api/v1/graph/conflicts/{conflict_id}/resolve
POST  /api/v1/graph/conflicts/{conflict_id}/dismiss
```

实体合并会重连事实与证据；拆分只移动明确选中的事实；关系修改仍受版本化 Schema 校验。所有操作保存操作者、原因和变更前后快照。冲突处理另外保存结构化处理结果和处理人。

## 数据库迁移

阶段 10 迁移为 `20260817_0010_stage10_admin_ui.py`，在 `graph_conflicts` 增加：

- `resolution_json`：结构化处理结果。
- `resolved_by`：本机管理员标识，未来可迁移为真实用户 ID。

运行：

```bash
make migrate
```

## 启动与验证

分别运行 API、Worker 和管理端：

```bash
make api
make worker
make web
```

验证结果（2026-08-17）：

- 后端 Ruff、Mypy 和完整测试通过：98 passed，1 个真实外部集成测试默认跳过，覆盖率 83.17%。
- 前端 ESLint、TypeScript、生产构建和 15 项测试通过，语句覆盖率 94.66%。
- Alembic 全链路 PostgreSQL 离线 SQL 编译通过，项目数据库已升级并核验为 `20260817_0010 (head)`。
- 已使用浏览器检查总览、文档、Chat 及 390×844 窄屏布局；无控制台错误和横向溢出。

默认测试不会调用 MinerU、Dingo、Voyage、cc switch、OpenSearch 或 Neo4j，不产生外部模型费用。
