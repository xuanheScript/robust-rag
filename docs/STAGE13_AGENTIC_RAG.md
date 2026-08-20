# 阶段 13：LangGraph Agentic RAG

## 1. 当前状态

阶段 13 已完成简化版工程实现。在线 Chat 可以通过 `AGENTIC_RAG_ENABLED` 在阶段 8 固定 RAG 与 LangGraph Agentic RAG 之间切换。新流程使用 LangGraph 1.2.11；默认仍关闭，待目标 Responses-compatible 模型完成真实 Tool Call Contract Test 和黄金路由评测后逐环境开启。

2026-08-19 已从在线主流程移除 Context Grader 及其触发的二次 Query Rewrite。后续是否重新引入证据评估能力，单独依据 `CONTEXT_GRADER_REDESIGN_PLAN.md` 评审，不作为当前阶段 13 的交付前提。

## 2. 编排模型

```text
START
  → Agent（LLM + 受控 Tools）
      ├─ 普通文本 → Direct Response → END
      ├─ retrieve_enterprise_documents → Hybrid + Rerank
      │    ├─ 有来源 → Grounded Generation → END
      │    └─ 无来源 → Deterministic Refusal → END
      └─ retrieve_enterprise_relationships → Hybrid + Graph + Rerank
           ├─ 有来源 → Grounded Generation → END
           └─ 无来源 → Deterministic Refusal → END
```

这里没有 `fast_path`，也没有独立的问候/闲聊分类器。Agent LLM 根据服务端保存的对话历史自行决定直接回答还是调用工具；条件边只读取普通文本或一个受控 Tool Call。Agent 在 Tool Call 中直接生成完整、可独立检索的 Query，不再增加检索后的 LLM 评分和循环改写节点。

## 3. 工具边界

- `retrieve_enterprise_documents`：用于制度、定义、日期、流程、操作说明和普通文档事实，显式设置本次请求不运行图查询。
- `retrieve_enterprise_relationships`：用于归属、依赖、影响路径和多跳关系，复用阶段 9 的 OpenSearch + 受控 Text-to-Cypher 融合检索。
- 工具只接受规范化自然语言 `query`，不接受 OpenSearch DSL、Cypher、索引名、连接参数或凭据。
- Agent 不直接生成或执行 Cypher；图查询仍经过只读白名单、Schema、路径深度、LIMIT、EXPLAIN、超时和结果大小限制。
- 未知工具、多 Tool Call、非法参数或 Agent 调用失败时安全回退文档检索。

## 4. 简化与安全终止

- 单 Turn 只执行一次 Agent 决策和最多一次受控检索，不形成 Agent 循环。
- 无来源时不再调用其他 LLM，直接返回确定性的信息不足响应。
- 有来源时进入 Grounded Generation，由现有 Grounded Prompt 负责只依据来源回答、部分回答或说明信息不足。
- Agent 产生的 Tool Query 可以使用有限、可信的服务端历史消解指代。
- 历史消息按 `created_at`、`finished_at` 和角色进行确定性排序；同一事务创建的 User/Assistant 消息即使 `created_at` 相同，也必须按 User → Assistant 顺序传入 Agent。
- Direct Response 由 Agent Prompt 限定为问候、感谢、能力说明和普通交流，不允许陈述未经工具检索的企业事实。
- Context Grader 改造方案作为独立设计文档保留，但当前代码、配置、SSE 和发布门槛均不依赖它。

## 5. 配置与发布

```dotenv
AGENTIC_RAG_ENABLED=false
AGENT_GRAPH_VERSION=stage13-langgraph-agentic-rag-v1
AGENT_PROMPT_VERSION=stage13-agent-decision-v3-zh
AGENT_MAX_OUTPUT_TOKENS=500
AGENT_REASONING_EFFORT=none
AGENT_RECURSION_LIMIT=12
```

`AGENT_REASONING_EFFORT` 只覆盖初始路由决策；默认关闭推理以缩短关键路径，最终有来源回答仍使用全局 `LLM_REASONING_EFFORT`。

发布顺序建议：开发环境真实模型 Contract Test → `agent-routing-v1` 黄金集基线 → 预发布小流量 → 生产灰度。任何路由、引用或延迟回归都可以将 `AGENTIC_RAG_ENABLED=false` 并重启 API，立即恢复固定 RAG；数据库结构不需要回滚。

## 6. 持久化与观测

- PostgreSQL 继续保存 Conversation、Message、RetrievalTrace、GraphQueryTrace、ModelInvocation 和 Citation，是业务与审计事实来源。
- LangGraph State 仅存在于单次请求中，不配置 Checkpointer，不与现有会话存储形成双重事实来源。
- Agent 决策、受控工具、文档/图谱检索和 Grounded Generation 分别建立 Langfuse Observation。
- Message 元数据保存 Graph/Prompt 版本、Action、Tool、Tool Call ID、工具调用次数、模型调用 ID 和告警摘要。
- 管理端消息 Trace 只暴露安全的路由摘要，不暴露 Prompt、完整工具参数、Cypher 或凭据。

## 7. 流式事件

现有 AI SDK UI Message Stream 保持兼容，并新增：

- `data-agent-status`：最终 Agent Action 与 Graph 版本。
- `data-tool-status`：受控工具名称以及 `running`/`completed` 状态。

Agentic Chat 在返回 `StreamingResponse` 后才执行 LangGraph。Responses API 的 Direct Response 文本增量通过 LangGraph custom stream 直接转换为 `text-delta`，不再等待完整 Agent 回答生成后一次性发送；模型调用记录 `first_token_ms`。Tool Call 只在 `response.completed` 后解析和执行，不向浏览器暴露函数参数。

Direct Response 不发送 `data-retrieval-status` 或来源事件；Grounded Response 继续发送检索状态、来源、引用和 Usage。无来源时发送检索状态后返回确定性拒答。Direct Response 已产生部分文本后若上游流中断，消息和模型调用均保存为 `failed`，保留已发送的部分文本且不再回退检索，避免混合两条回答。

## 8. 验证

```bash
cd backend
uv run ruff check src tests
uv run mypy src tests
uv run pytest

cd ../frontend
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

正式启用前还必须用目标模型确认：纯问候 Direct、混合问候与知识问题走 Documents、关系/多跳问题走 Relationships、多轮指代生成独立 Tool Query、知识库无答案稳定拒答、非法 Tool 和依赖故障按预期回退。
