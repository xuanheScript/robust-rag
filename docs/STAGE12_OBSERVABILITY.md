# 阶段 12：可观测性、异常恢复与安全加固

## 1. 当前状态

阶段 12 已完成第一轮工程实现：统一 HTTP/Celery Trace、Langfuse Cloud Adapter、LLM/RAG/评测观测、敏感字段脱敏、运行时健康检查、Celery 心跳与可靠投递配置已经接入。Langfuse 默认启用，但本机尚未配置 Cloud Public Key 和 Secret Key，因此本地状态为 `degraded`，核心业务不受影响；取得凭据后仍需完成一次真实 Cloud 上报验收。

Alembic `20260818_0012` 已应用，用于对齐阶段 11 评测字段的存储类型和空值声明；本机迁移一致性检查没有待生成操作。

## 2. 责任边界

- PostgreSQL 是文档、任务、检索 Trace、模型调用审计、评测结果和恢复状态的事实来源。
- Langfuse Cloud 用于查看 LLM/RAG Trace、Span、Generation、Token、成本、延迟和评测 Score。
- structlog 保存本地结构化运行日志，所有事件在渲染前执行敏感字段脱敏。
- `/health/*` 暴露安全的运行状态，不返回 Secret Key、连接串或认证 Header。
- Langfuse 上报失败不得回滚业务事务、阻塞流式回答、阻止 Worker 退出或改变评测报告结果。

## 3. Langfuse Cloud 配置

```dotenv
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_SAMPLE_RATE=1.0
LANGFUSE_CAPTURE_CONTENT=false
LANGFUSE_TIMEOUT_SECONDS=3
LANGFUSE_FLUSH_AT=20
LANGFUSE_FLUSH_INTERVAL_SECONDS=5
```

默认行为：

- `LANGFUSE_ENABLED=true`：应用默认尝试启用 Langfuse。
- 凭据为空：状态为 `degraded`，不创建网络客户端，业务正常运行。
- `LANGFUSE_SAMPLE_RATE=1.0`：首版规模下记录全部 Trace；数据量增长后可以下调。
- `LANGFUSE_CAPTURE_CONTENT=false`：Prompt、问题和回答只上报类型、字符数或字段名摘要。
- 即使显式开启内容采集，字段白名单和凭据脱敏仍然执行。
- Cloud 区域通过 `LANGFUSE_BASE_URL` 选择，真实企业数据接入前必须确认数据驻留和外发政策。

## 4. Trace 模型

### 4.1 HTTP 与 Celery

- API 接受 `X-Request-ID`，生成稳定的 128-bit `X-Trace-ID`，并在响应头返回两者。
- HTTP 请求创建 `http.request` 根 Span，结构化日志同时绑定 `request_id` 和 `trace_id`。
- 入库任务使用 `ingestion:{job_id}` 生成稳定 Trace ID，Worker 日志绑定 `task_id`。
- 图谱抽取使用 `graph-build:{graph_build_request_id}`，恢复扫描使用固定恢复 Trace。
- 每个 Celery 子进程在 fork 后丢弃继承的 Langfuse 客户端并初始化独立 exporter，避免复用父进程中已经失效的后台线程。

### 4.2 RAG 与模型

- `retrieval.bm25`：BM25 命中数、重试和耗时。
- `retrieval.query_embedding`：Embedding 模型、Token、重试和向量数量。
- `retrieval.dense`：Dense 命中数与重试。
- `retrieval.graph`：图谱命中数与回退原因。
- `llm.text_to_cypher`：Cypher 生成 Prompt、输出、Token、耗时和错误码。
- `graph.neo4j.explain`：验证后的 Cypher、执行计划和估算行数。
- `graph.neo4j.query`：实际查询耗时、返回行数和开发环境下的结果详情。
- `retrieval.rerank`：候选数、结果数、模型、Token 与重试。
- `llm.query_rewrite`：Prompt 版本、模型、Token、成本、耗时和降级。
- `llm.rag_generation`：上传实际 Responses API JSON Body 对应的完整安全参数，包括 model、instructions、完整 input/context、max output、reasoning、metadata、tools/text format/tool choice（若存在），以及完整聚合响应、Usage、重试、引用和耗时。

`ModelInvocation.trace_id` 保存应用 Trace ID；上游 Provider 的 `response_id` 保存在 `response_snapshot`，两者不得混用。
所有子 Observation 显式继承当前 Observation ID，HTTP Trace 应形成一棵连续的父子树，
不能为每个阶段生成独立的虚拟父节点。
流式 SSE 生成使用非上下文绑定的 Langfuse Observation，并显式传递 Trace/Parent Span ID；
不得让 OpenTelemetry 的 Context Token 跨越同步生成器 `yield`，避免 Starlette 在复制的
worker Context 中恢复生成器时出现 `Failed to detach context`。

### 4.3 评测

- 每个样本使用 `evaluation:{run_id}:{sample_id}` 生成稳定 Trace ID。
- 确定性 IR、引用、图谱指标和 Ragas 指标写入 Langfuse Trace Score。
- 黄金集、逐样本结果和正式 JSON/Markdown 报告仍保存在版本化文件与 PostgreSQL 中。

## 5. 数据保护

除下述显式诊断例外，默认禁止上报或记录：

- 完整原始文档、完整检索上下文、完整 Prompt 和回答。
- `Authorization`、Cookie、Password、Secret、API Key、Access/Refresh Token。
- PostgreSQL、Redis 和其他带凭据的连接串。

`llm.rag_generation` 是显式诊断例外：即使全局 `LANGFUSE_CAPTURE_CONTENT=false`，也会完整上传该次模型调用的 Prompt、来源 Context 和回答，以便核对实际模型参数。API Key、Authorization、Cookie、Password、Secret、Token 和带凭据连接串仍在应用进程内强制脱敏，且不会进入请求快照。

脱敏在应用进程内、数据进入 Langfuse SDK 和日志 Renderer 前执行。浏览器只读取状态、Base URL、采样率、最近 Trace 时间、错误类别和丢弃计数，不读取任何 Langfuse 凭据。

本地开发排障可临时设置 `LANGFUSE_CAPTURE_CONTENT=true`，以查看检索查询、命中详情、
Text-to-Cypher Prompt/输出及回答正文。共享测试、预发布和生产环境必须恢复为 `false`，
并清理开发阶段已经上传的不必要 Trace。

## 6. 健康检查

```text
GET /health/live
GET /health/ready
GET /health/dependencies
GET /health/observability
GET /health/observability?remote=true
```

- `/health/ready` 只检查业务必需的 PostgreSQL 与 Redis。
- `/health/dependencies` 返回数据库、Redis、Worker、Beat、队列、Neo4j、Provider 配置和 Langfuse 本地状态；`worker.observability` 另行报告最近一次 Worker flush 的任务、时间、结果和安全健康快照，不能再用 API 进程状态代替 Worker 状态。
- `remote=true` 调用 Langfuse `auth_check`，只用于人工诊断或低频探测，不应作为高频就绪检查。
- `degraded` 表示观测链路未配置或降级，不能据此摘除仍可提供业务服务的 API 实例。

## 7. Celery 恢复与可靠性

- `task_acks_late=true`、`task_reject_on_worker_lost=true`、`worker_prefetch_multiplier=1`。
- PostgreSQL 保存最终状态，Celery 消息只携带业务 ID。
- 恢复扫描使用数据库行锁和 `skip_locked`，避免多个恢复进程重复领取同一批任务。
- Worker 心跳写入 `robust-rag:worker:last_seen`；Beat 定时探针写入 `robust-rag:beat:last_seen`。
- 每个 Celery 任务无论成功或失败都会在 `task_postrun` 后有限等待 Langfuse `flush()`，确保 Worker 内的 Trace/Generation 在任务结束后交付；Worker 子进程退出时对已初始化客户端执行一次幂等 `shutdown()`。
- Worker 最近一次 flush 状态写入带 TTL 的 `robust-rag:worker:observability`，上报失败只产生结构化告警，不改变任务结果或数据库终态。
- 队列深度达到 `CELERY_QUEUE_WARNING_DEPTH` 后状态变为 `warning`。
- 所有阶段仍必须幂等；Late Ack 提供重新投递，不提供业务幂等保证。

## 8. 故障处理手册

### Langfuse 显示 `degraded`

1. 检查 `LANGFUSE_ENABLED` 是否为 `true`。
2. 检查 Public Key、Secret Key 和 Base URL 是否从后端环境注入。
3. 调用 `/health/observability?remote=true` 验证凭据；响应不包含密钥。
4. 检查本地日志中的 `langfuse_export_degraded`，按 `operation` 和 `error_type` 定位。
5. 检查 `/health/dependencies` 中的 `worker.observability.flush_ok`、`last_flush_at`、`task_name` 和 `last_error`，区分 API 正常但 Worker exporter 异常的情况。
6. Cloud 未恢复前继续以 PostgreSQL、本地日志和评测报告排障，不重跑成功业务只为补 Trace。

### Worker 或 Beat 不可用

1. 查看 `/health/dependencies` 中 `worker`、`scheduler` 和 `queue`。
2. Worker 心跳过期时检查 Worker 进程和 Redis Broker；Beat 心跳过期时检查 Beat 进程及 `system.record_beat_heartbeat`。
3. 服务恢复后等待 `ingestion.recover_pending`，或人工触发一次恢复任务。
4. 根据 PostgreSQL `IngestionJob.current_stage`、`StageRun` 和错误码确认恢复点，禁止直接手工改最终状态。

### 队列持续积压

1. 对比队列深度、Worker 心跳和最近 StageRun 耗时。
2. 检查外部 Provider 限流、超时与重试量，避免盲目增加 Worker 并发扩大限流。
3. 确认长任务已拆分阶段，`visibility_timeout` 大于最长单阶段耗时。
4. 扩容前验证任务幂等和 Provider 费用边界。

### Trace 疑似包含敏感信息

1. 立即设置 `LANGFUSE_CAPTURE_CONTENT=false`；如需完全停止外发，设置 `LANGFUSE_ENABLED=false` 并重启 API/Worker。
2. 在 Langfuse Cloud 按 Trace ID 删除相关数据，并依据组织策略轮换可能泄漏的凭据。
3. 检查字段是否绕过统一 Observability Adapter，补充脱敏规则和回归测试。
4. 记录事件范围、处置时间和数据保留结果。

## 9. 验证命令

```bash
cd backend
.venv/bin/ruff check src tests
.venv/bin/mypy src tests
.venv/bin/pytest

cd ../frontend
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

真实 Cloud 验收必须在配置有效凭据后执行：

1. 调用 `/health/observability?remote=true`，期望 `status=ok`。
2. 发起一次 Chat、一次人工图谱生成和一次小样本评测。
3. 在 Langfuse 确认 `graph.extract` 下存在逐 Parent 的 `llm.graph_extraction` Generation，并核对 Trace 层级、Token/成本、首 Token 耗时和 Score。
4. 确认除 `llm.rag_generation` 的显式完整 Prompt/Context 外，其他 Trace 遵循内容采集配置；所有 Observation 均不得出现密钥、Cookie 和带凭据连接串。
5. 临时配置错误 Base URL，确认 Chat、入库和本地评测报告仍成功，仅产生观测降级告警。
