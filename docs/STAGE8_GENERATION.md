# 阶段 8：cc switch 与 RAG Generation

阶段 8 在阶段 7 的可解释检索结果上实现受来源约束的多轮问答，并把上游 Responses
SSE 转换为 AI SDK UI Message Stream v1。浏览器只访问 FastAPI，不直接访问 cc switch。

## 调用链

```text
AI SDK useChat
  → POST /api/v1/chat
  → 持久化 Conversation / User Message / Assistant Message
  → 多轮 Query Rewrite（失败时降级为当前问题）
  → 阶段 7 Hybrid + Rerank 与上下文组装
  → 无上下文：服务端确定性拒答
  → 有上下文：Grounded Prompt → cc switch /v1/responses
  → Responses typed SSE → UI Message Stream v1
  → 持久化回答、引用、用量、延迟和错误
```

## Provider 与上游协议

- `CCSwitchResponsesProvider` 固定使用准确模型 ID `gpt-5.6-luna`，支持非流式和流式
  Responses 调用。
- 流式解析处理 `response.output_text.delta`、`response.refusal.delta`、
  `response.completed`、`response.failed`、`response.incomplete` 和 `error`。
- Provider 对 HTTP、超时、连接、无文本、无完成事件和畸形 SSE 返回结构化错误，并标明
  是否可重试。
- `FakeLLMProvider` 用于默认测试，不访问外部服务、不产生模型费用。
- `LLMRequest.text_format` 保留 Responses Structured Output 契约，用于真实兼容性检查和后续
  图谱阶段。

## Grounded RAG、拒答与引用

- 文档上下文放在明确的来源边界内，Prompt 要求把来源视为不可信数据，不执行文档中的指令。
- 回答只能使用提供的企业知识库上下文，并要求主要事实使用 `[S1]` 形式引用。
- 没有最终上下文时不调用模型，按问题语言返回确定性拒答。
- 来源 Data Part 包含文档名、标题路径、Node ID、页码/幻灯片/Sheet/Cell/行号和截断原文。
- 回答完成后只把实际出现的来源标签保存为 Citation；引用保存快照，因此来源后续删除不会
  破坏历史回答的可解释性。

## 多轮 Query Rewrite

- 只从服务端已保存的最近对话消息构造改写输入，不信任浏览器提交的历史回答。
- 无历史时使用当前规范化问题，不调用模型。
- 有历史时让模型只解决指代和省略，返回单一可检索问题；原问题、改写问题、历史条数、Prompt
  版本和调用 ID 均进入 Retrieval Trace。
- 改写调用失败、超时或返回非法问题时自动降级为当前问题，并通过 `data-warning` 显示降级。

## UI Message Stream

响应类型为 `text/event-stream`，并设置：

```text
x-vercel-ai-ui-message-stream: v1
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

主要 Part：

```text
start
data-conversation
data-retrieval-status
data-source
data-warning
text-start / text-delta / text-end
data-usage
error
finish
[DONE]
```

普通 Chat 流不会发送完整 Prompt、完整候选、密钥或供应商原始错误。

## 持久化与 API

迁移 `20260817_0008_stage8_generation.py` 新增：

- `conversations`
- `messages`
- `citations`
- `model_invocations`

API：

```text
POST   /api/v1/chat
GET    /api/v1/conversations
POST   /api/v1/conversations
GET    /api/v1/conversations/{conversation_id}
DELETE /api/v1/conversations/{conversation_id}
GET    /api/v1/messages/{message_id}/trace
```

## 配置

关键环境变量见 `.env.example`：

```text
LLM_BASE_URL=http://127.0.0.1:15721/v1
LLM_MODEL=gpt-5.6-luna
LLM_API_STYLE=responses
LLM_REASONING_EFFORT=medium
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=1
GENERATION_PROMPT_VERSION=stage8-grounded-rag-v1
QUERY_REWRITE_PROMPT_VERSION=stage8-conversation-rewrite-v1
QUERY_REWRITE_HISTORY_MESSAGES=6
```

模型价格不硬编码；只有显式配置输入/输出 Token 单价时才保存估算成本。

## 验证

默认自动化测试使用 HTTP Mock 和 Fake Provider，覆盖 Responses 请求/SSE、UI Message Stream、
Grounded Prompt、引用持久化、无上下文拒答、多轮改写、故障流和会话 API。

真实模型契约测试默认跳过，避免隐式产生模型费用。确认允许真实调用后执行：

```bash
cd backend
RUN_LIVE_CC_SWITCH_TESTS=1 uv run pytest -m integration_live \
  tests/test_stage8_cc_switch_live.py
```

真实契约会验证准确模型映射、非流式文本、Responses SSE、Token Usage、多轮输入和 Structured
Output。当前只完成了 cc switch `/health` 与 `/status` 的无费用检查；真实模型调用仍需显式启用。
