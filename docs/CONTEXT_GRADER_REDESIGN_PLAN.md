# Context Grader 改造计划

## 1. 文档状态

- 状态：保留提案，当前暂缓实施
- 日期：2026-08-19
- 适用范围：阶段 13 LangGraph Agentic RAG 在线 Chat
- 关联文档：`IMPLEMENTATION_PLAN.md`、`STAGE13_AGENTIC_RAG.md`、`STAGE11_EVALUATION.md`

本方案用于重新定义 Context Grader 在在线 RAG 中的职责、调用条件、输出契约和后续控制逻辑。2026-08-19 在线 Context Grader 及其触发的二次 Query Rewrite 已从当前阶段 13 代码中移除；本文保留移除前的问题诊断和未来可能重新引入时的设计方案，不属于当前发布范围。

---

## 2. 结论摘要

Context Grader 有保留价值，但不再作为所有检索请求的强制在线节点。

目标方案将现有单一的 `relevant / rewrite / insufficient` 三分类拆为三个相互独立的概念：

1. `retrieval_health`：检索链路是否健康。
2. `evidence_status`：当前证据对当前问题是否充分。
3. `next_action`：系统下一步应生成、部分回答、改写、澄清还是停止。

在线流程采用以下分工：

- Retrieval Controller 使用确定性、可解释的检索信号完成大部分判断。
- Context Grader 只处理确定性规则无法判断的灰区样本。
- Context Grader 只诊断当前证据，不直接执行或决定下一步动作。
- Grounded Generation 继续承担基于来源回答、部分回答、信息不足说明和引用生成职责。
- 离线 Context Grader 继续用于黄金集评测、阈值校准和失败分析。

在影子评测证明在线 Grader 能显著改善最终答案质量之前，在线 Grader 默认不影响用户响应。

---

## 3. 背景与问题

### 3.1 当前目标状态图

当前阶段 13 将 Context Grader 放在每次检索之后：

```text
Agent
  → Retrieval Tool
  → Context Grader
      ├─ relevant → Grounded Generation
      ├─ rewrite → Query Rewrite → Agent
      └─ insufficient → Refuse
```

该状态图默认 Context Grader 可以同时可靠判断：

- 检索结果是否相关；
- 当前证据是否足够；
- 查询是否值得改写；
- 是否应该拒答。

这四个问题所需的信号和决策责任不同，不应由一个三分类结果同时承载。

### 3.2 真实 Trace 暴露的问题

2026-08-19 的真实会话中，用户问题为“你知道住众公司吗”。检索返回了 10 个来源，但来源主要来自同一份内部选聘公告，只能支持公司全称和招聘相关事实，不能支持完整公司介绍。

该请求中的 Context Grader：

- 使用 `deepseek-v4-flash`；
- 使用全局 `medium` reasoning；
- `max_output_tokens=120`；
- 返回 `LLM_INCOMPLETE_RESPONSE`；
- 没有产生结构化结果；
- 当前代码将异常降级为 `relevant` 并继续生成；
- Grader 单独增加约 3.9 秒延迟，总请求约 21.5 秒。

这说明当前强制节点同时带来了质量控制失效、故障语义错误和在线延迟增加。

### 3.3 计划与实现不一致

总实施计划要求：

- 无上下文时确定性判断；
- 明显相关或不相关时优先使用可解释阈值；
- 只有边界样本使用 LLM Structured Output。

当前实现则是：

- 无来源时确定性 `insufficient`；
- 只要存在来源，就调用 LLM Context Grader；
- LLM 调用失败时默认 `relevant`。

因此当前实现不是“灰区裁判”，而是“所有检索请求的强制 LLM 关卡”。

### 3.4 当前三分类无法表达关键状态

以下情况无法被 `relevant / rewrite / insufficient` 准确表达：

- 来源相关且证据充分；
- 来源相关但只能部分回答；
- 来源不相关，但查询已经清晰，改写没有明显收益；
- 查询表达不佳，重写可能改善检索；
- 用户意图过于宽泛，应向用户澄清；
- 多个来源相互冲突；
- Reranker 或图检索已降级，证据质量未知；
- 当前 Top-K 没有答案，但不能证明整个知识库没有答案。

### 3.5 与 Grounded Generation 职责重叠

现有 Grounded Generation 已要求：

- 只依据提供的来源回答企业事实；
- 不使用模型记忆补充缺失信息；
- 信息不足时明确说明；
- 为主要事实生成引用。

如果 Context Grader 在生成前再次决定“是否足够回答”，两个 LLM 节点可能产生不一致结论，并增加一次模型调用和新的故障点。

---

## 4. 设计原则

### 4.1 诊断与动作分离

Context Grader 只输出当前证据诊断，不直接输出最终路由动作。`generate`、`rewrite`、`clarify`、`abstain` 等动作由 Retrieval Controller 决定。

### 4.2 当前证据不等于整个知识库

Context Grader 只能声明“当前提供的证据不足”，不能声明“知识库不存在答案”。后者至少需要多策略检索、受控重试或数据集级事实才能判断。

### 4.3 确定性优先

无来源、检索失败、精确编号命中、明显高置信结果等情况优先使用可解释规则。LLM 只处理灰区，不能替代已有的 BM25、Dense、RRF、Rerank 和图检索信号。

### 4.4 部分回答是一等状态

企业知识库经常只包含问题的一部分证据。系统必须支持 `partial`，不能强迫所有请求在完整回答和拒答之间二选一。

### 4.5 澄清与改写分离

- 用户意图不明确时，向用户澄清。
- 用户意图明确但检索 Query 表达不佳时，自动改写。
- 不能通过 Query Rewrite 擅自替用户选择一个未表达的意图。

### 4.6 故障不能伪装成成功判断

Context Grader 超时、Incomplete、非法 JSON 或 Schema 不匹配时，结果必须为 `unknown/ungraded`。禁止把故障映射为 `relevant`。

### 4.7 用真实收益决定是否上线

在线 Grader 是否启用由黄金集和影子流量数据决定。没有质量增益证据时，不允许仅因为流程图完整而将其加入所有请求的关键路径。

---

## 5. 目标架构

```text
START
  → Agent
      ├─ Direct Response → END
      └─ Retrieval Tool
           → Retrieval Controller
                ├─ deterministic_accept → Grounded Generation
                ├─ deterministic_rewrite → Query Rewrite（最多一次）
                ├─ clarify → Clarification Response
                ├─ abstain → Insufficient Evidence Response
                └─ gray_zone
                     → Optional Context Grader
                          → Retrieval Controller
                               ├─ generate
                               ├─ partial_answer
                               ├─ rewrite（最多一次）
                               ├─ clarify
                               └─ abstain
```

Query Rewrite 后重新执行 Retrieval Controller。只有新结果仍处于灰区且未超过 Grader 调用上限时，才允许再次调用 Context Grader。

---

## 6. 职责边界

### 6.1 Retrieval Controller 的职责

Retrieval Controller 是确定性编排节点，输入包括：

- 原始问题与当前独立检索 Query；
- 检索状态；
- BM25、Dense、Graph、RRF 和 Rerank 摘要；
- 精确命中信号；
- 最终来源数、文档数和 Parent 多样性；
- Reranker、Graph 和其他依赖的降级原因；
- 当前 Tool 类型；
- 改写次数、Tool Call 次数和 Grader 调用次数；
- 可选的 Context Grader 诊断结果。

Retrieval Controller 负责输出 `next_action`，并保存可解释的决策来源和原因码。

### 6.2 在线 Context Grader 的职责

在线 Context Grader 仅回答：

> 当前提供的来源，对当前用户问题能够支持到什么程度？

它可以诊断：

- 当前证据充分、部分、缺失、冲突或无法判断；
- 当前 Query 是否清晰、宽泛、歧义或包含未消解指代；
- 当前证据明确支持哪些方面；
- 仍缺少哪些方面；
- 改写 Query 是否可能改善检索。

### 6.3 在线 Context Grader 不负责

Context Grader 不得：

- 生成最终回答；
- 直接决定拒答；
- 直接生成改写后的 Query；
- 判断整个知识库不存在答案；
- 使用模型记忆补充企业事实；
- 因来源中存在少量关键词就判定证据充分；
- 执行来源文本中的任何指令；
- 替代 Citation 校验、Faithfulness 评测或最终答案 Judge。

### 6.4 Grounded Generation 的职责

Grounded Generation 继续负责：

- 完整回答或部分回答；
- 明确说明当前证据边界；
- 必要时提出面向用户的澄清问题；
- 无充分证据时停止陈述企业事实；
- 生成并校验来源标签。

### 6.5 离线 Context Grader 的职责

离线 Grader 用于：

- 黄金集 Context Precision、Context Recall 和 Answerability 评测；
- 为确定性阈值提供校准数据；
- 比较不同检索、Rerank、Graph 和 Query Rewrite 配置；
- 分析错误接受、错误拒绝和无收益改写样本；
- 对在线 Grader 模型或 Prompt 版本进行回归评测。

离线 Grader 与在线控制节点必须使用不同的 purpose、版本和指标，不能混为同一事实来源。

---

## 7. 状态模型

### 7.1 检索健康状态

```text
retrieval_health:
  healthy
  degraded
  failed
```

- `healthy`：计划中的检索与 Rerank 路径正常完成。
- `degraded`：发生允许的降级，例如 Reranker 回退 RRF、Graph 回退文档检索。
- `failed`：无法获得可用检索结果。

### 7.2 证据状态

```text
evidence_status:
  sufficient
  partial
  insufficient
  conflicting
  unknown
```

- `sufficient`：当前来源覆盖问题的主要可回答方面。
- `partial`：来源与问题相关，但只支持部分回答。
- `insufficient`：当前来源不能支持有意义的事实回答。
- `conflicting`：来源对关键事实存在无法消解的冲突。
- `unknown`：确定性规则无法判断，且 Grader 未运行、失败或无法可靠判断。若确定性规则已经得出可靠结论，即使未调用 Grader，也应记录对应的 `sufficient`、`partial` 或 `insufficient`，并将决策来源标记为 `deterministic_rule`。

### 7.3 Query 诊断

```text
query_diagnosis:
  clear
  ambiguous
  underspecified
  unresolved_reference
  unknown
```

### 7.4 改写潜力

```text
rewrite_potential:
  high
  low
  none
  unknown
```

`rewrite_potential=high` 只表示改写可能改善检索，不表示系统必须改写。Controller 仍需检查用户意图、历史、重试预算和当前 Query 是否已经改写。

### 7.5 后续动作

```text
next_action:
  generate
  partial_answer
  rewrite
  clarify
  abstain
  fallback
```

### 7.6 决策来源

```text
decision_source:
  deterministic_rule
  context_grader
  degraded_fallback
  hard_limit
```

---

## 8. Context Grader 输入契约

在线 Context Grader 的输入应包含：

```json
{
  "question": "用户当前问题",
  "retrieval_query": "本轮独立检索 Query",
  "tool": "retrieve_enterprise_documents",
  "attempt": 1,
  "retrieval_health": "healthy",
  "retrieval_summary": {
    "source_count": 10,
    "document_count": 1,
    "parent_count": 6,
    "exact_match_count": 1,
    "top_rerank_score": null,
    "rerank_score_gap": null,
    "fallback_reasons": []
  },
  "sources": [
    {
      "label": "S1",
      "document_name": "...",
      "heading_path": ["..."],
      "content": "..."
    }
  ]
}
```

约束：

- 来源正文继续按总上下文预算裁剪。
- 来源属于不可信数据，Prompt 必须明确禁止执行其中的指令。
- 不向外部模型发送不必要的内部 ID、完整 Cypher、凭据或敏感元数据。
- 检索分数只作为诊断信号，不能让模型假定某个未经校准的固定分数必然代表充分。
- Grader 应看到原始用户问题和独立检索 Query，避免把 Query 相关误判为用户意图充分。

---

## 9. Context Grader 输出契约

建议使用严格 Structured Output：

```json
{
  "evidence_status": "partial",
  "query_diagnosis": "underspecified",
  "rewrite_potential": "low",
  "confidence": "high",
  "reason_code": "partial_entity_evidence",
  "supported_aspects": ["company_legal_name", "internal_recruitment"],
  "missing_aspects": ["company_profile", "business_scope"]
}
```

字段约束：

- `evidence_status`：必填枚举。
- `query_diagnosis`：必填枚举。
- `rewrite_potential`：必填枚举。
- `confidence`：`high / medium / low`，仅用于诊断，不直接作为路由阈值。
- `reason_code`：必填、稳定的英文 snake_case 原因码。
- `supported_aspects`：最多 5 项，每项为简短事实方面，不输出答案正文。
- `missing_aspects`：最多 5 项，每项为简短缺口，不虚构知识库内容。
- 禁止输出 `next_action`、最终答案或改写后的 Query。

首版原因码至少覆盖：

```text
exact_evidence_match
broad_evidence_coverage
partial_evidence_coverage
partial_entity_evidence
missing_key_evidence
unrelated_context
conflicting_key_evidence
ambiguous_user_intent
underspecified_user_intent
unresolved_reference
retrieval_degraded
grader_uncertain
```

---

## 10. 在线调用条件

### 10.1 不调用 Grader 的情况

以下情况由确定性 Controller 直接处理：

- Retrieval 没有返回任何来源；
- Retrieval 失败；
- 已达到改写、Tool Call 或递归上限；
- 已识别明确的检索依赖故障，无法可靠判断证据质量；
- 黄金集校准后被判定为明显高置信的精确问题与精确证据；
- 黄金集校准后被判定为明显低置信且 Query 已经清晰的结果；
- 在线 Context Grader 功能开关关闭。

### 10.2 调用 Grader 的情况

只有同时满足以下条件才调用：

- Retrieval 至少返回一个来源；
- Retrieval Controller 判定为灰区；
- 当前证据相关性与充分性无法通过规则可靠区分；
- 尚未超过本轮 Grader 调用上限；
- 当前延迟预算允许调用；
- 目标模型已通过 Structured Output Contract Test。

### 10.3 灰区示例

- Top 结果相关，但来源只覆盖问题的一部分；
- 多个来源对关键事实表述不一致；
- Query 很宽泛，来源只覆盖其中一个可能意图；
- Rerank 分数位于黄金集校准得到的接受与拒绝阈值之间；
- 图谱和文档检索均返回证据，但覆盖范围或关系路径存在不确定性。

阈值不得凭经验直接写死。第一版阈值必须从黄金集和影子 Trace 中校准，并进入版本化配置。

---

## 11. Controller 决策表

| Retrieval/Grader 状态 | 附加条件 | next_action | 说明 |
|---|---|---|---|
| `failed` | 无可用来源 | `fallback` 或 `abstain` | 区分系统失败与知识不足 |
| `degraded` | 仍有高置信证据 | `generate` | 回答中无需暴露内部错误，Trace 必须记录降级 |
| `degraded` | 证据处于灰区 | `fallback`、`clarify` 或 `abstain` | 不用 Grader 掩盖依赖故障 |
| `sufficient` | 任意 | `generate` | 使用当前 Context |
| `partial` | 用户意图明确、部分答案有价值 | `partial_answer` | 明确说明未覆盖部分 |
| `partial` | Query 明确且改写潜力高、未重试 | `rewrite` | 重搜一次后重新评估 |
| `partial` | 用户意图宽泛或歧义 | `clarify` | 不擅自选择用户意图 |
| `insufficient` | 改写潜力高、未重试 | `rewrite` | 最多一次 |
| `insufficient` | Query 已清晰或已经重试 | `abstain` | 只能声明当前证据不足 |
| `conflicting` | 低风险且冲突可引用展示 | `partial_answer` | 明确展示来源差异，不强行裁决 |
| `conflicting` | 高风险或冲突不可解释 | `clarify` 或 `abstain` | 避免错误结论 |
| `unknown` | 确定性规则可接受 | `generate` 或 `partial_answer` | 标记 `decision_source=degraded_fallback` |
| `unknown` | 确定性规则不可接受 | `clarify` 或 `abstain` | 禁止默认映射为 `relevant` |

### 11.1 澄清与改写的判定原则

- 缺少的是用户意图：`clarify`。
- 用户意图明确，但 Query 包含未消解指代：使用可信服务端历史改写。
- 用户意图明确，但检索表达过宽、过窄或缺少关键实体：`rewrite`。
- 当前 Query 已明确且已经重试：不得继续循环改写。

### 11.2 “你知道住众公司吗”示例

期望诊断：

```json
{
  "retrieval_health": "healthy",
  "evidence_status": "partial",
  "query_diagnosis": "underspecified",
  "rewrite_potential": "low",
  "next_action": "partial_answer"
}
```

期望回答策略：

- 说明知识库当前可以确认的公司全称和资料类型；
- 明确当前资料主要涉及内部选聘；
- 询问用户希望了解公司基本情况、招聘信息还是具体业务；
- 不擅自把问题改写为“公司简介”并假定用户意图。

---

## 12. 故障与降级策略

### 12.1 Grader 故障类型

- Provider 超时或连接错误；
- HTTP 错误或限流；
- `status=incomplete`；
- 无文本输出；
- Structured Output Schema 不匹配；
- JSON 解析失败；
- 输出枚举或字段非法。

### 12.2 故障处理

首版在线策略：

1. Grader 故障统一产生 `evidence_status=unknown`。
2. 保存准确错误码和上游 `incomplete_details.reason`。
3. 不把故障映射为 `sufficient`、`partial` 或旧的 `relevant`。
4. 回到 Retrieval Controller，使用确定性信号选择安全动作。
5. Grader 故障不直接导致整个 Chat 请求 500。
6. 首版不在同一请求内用相同参数盲目重试 Grader，以控制尾延迟。
7. Provider 层只对明确的瞬时错误执行既有有限重试。

### 12.3 模型配置

Context Grader 使用独立的按用途配置，不再完全复用全局生成参数：

```text
AGENT_CONTEXT_GRADER_ENABLED=false
AGENT_CONTEXT_GRADER_MODE=shadow
AGENT_CONTEXT_GRADER_REASONING_EFFORT=low
AGENT_CONTEXT_GRADER_MAX_OUTPUT_TOKENS=400
AGENT_CONTEXT_GRADER_TIMEOUT_SECONDS=8
AGENT_CONTEXT_GRADER_MAX_CALLS=1
AGENT_CONTEXT_GRADER_PROMPT_VERSION=stage13-context-evidence-v3-zh
AGENT_RETRIEVAL_CONTROLLER_VERSION=stage13-retrieval-controller-v1
```

说明：

- 具体 token 与超时值在真实模型 Contract Test 后确认。
- 如果 Provider 不支持按请求设置 reasoning，需要扩展 Provider 契约或使用独立 Grader Provider 配置。
- 不能仅通过增加 token 解决职责模型错误。

---

## 13. 可观测性与持久化

### 13.1 Langfuse Observation

新增或调整：

```text
agent.retrieval_controller   EVALUATOR/SPAN
llm.context_grader           GENERATION（仅实际调用时产生）
agent.context_grader         EVALUATOR（汇总诊断与故障）
```

`agent.retrieval_controller` 至少记录：

- Controller 版本；
- 是否进入灰区；
- 决策使用的脱敏特征；
- Grader 是否被调用及原因；
- `retrieval_health`；
- `evidence_status`；
- `next_action`；
- `decision_source`；
- 原因码；
- 改写、Tool 和 Grader 调用次数。

`llm.context_grader` 至少记录：

- 模型与 Prompt 版本；
- Structured Output 版本；
- latency、TTFT、usage 和 cost；
- finish reason；
- `incomplete_details.reason`；
- 重试和降级信息；
- 默认不上传完整来源正文。

### 13.2 Message 元数据

Assistant Message 建议保存：

```json
{
  "retrieval_controller_version": "...",
  "retrieval_health": "healthy",
  "evidence_status": "partial",
  "query_diagnosis": "underspecified",
  "rewrite_potential": "low",
  "next_action": "partial_answer",
  "decision_source": "context_grader",
  "decision_reason_code": "partial_entity_evidence",
  "context_grader_invoked": true,
  "context_grader_invocation_id": "...",
  "context_grader_fallback_reason": null
}
```

### 13.3 SSE 与前端兼容

建议新增版本化事件：

```text
data-retrieval-decision
data-context-assessment
```

- `data-retrieval-decision` 表示 Controller 的最终动作。
- `data-context-assessment` 只在 Grader 实际调用时发送。
- 现有 `data-context-grade` 保留一个兼容周期，但不再用于业务控制。
- 普通用户只展示“正在核验资料”“资料不足，正在优化检索”等可理解状态，不展示内部阈值和 Prompt。

---

## 14. 评测方案

### 14.1 黄金集扩展

现有 `agent-routing-v1` 主要验证 Agent Action 和 Tool 选择，不足以评估证据状态。新增 `context-assessment-v1`，每条样本至少包含：

```text
question
retrieval_query
retrieval_fixture 或 relevant_node_ids
expected_retrieval_health
expected_evidence_status
expected_query_diagnosis
expected_rewrite_potential
allowed_next_actions
forbidden_next_actions
answerable
partial_answer_allowed
reason_codes
tags
```

样本类型至少覆盖：

- 精确制度、编号、日期和流程；
- 多来源综合问题；
- 只覆盖部分问题的来源；
- 相关但无法完整回答；
- 完全不相关的高词面重叠；
- Query 可通过改写改善；
- 用户意图宽泛，应澄清而非改写；
- 多轮未消解指代；
- 来源冲突；
- 无来源；
- Reranker 或 Graph 降级；
- Prompt Injection 来源；
- Grader timeout、incomplete 和非法结构化输出。

### 14.2 离线指标

- Evidence Status Macro F1；
- `sufficient` 错误接受率；
- `insufficient` 错误拒绝率；
- `partial` 识别 Precision/Recall；
- Clarify 与 Rewrite 混淆率；
- 改写成功率和检索增益；
- 无收益改写率；
- Controller 与人工动作一致率；
- Grader Structured Output 成功率；
- Grader 故障率；
- Grader 相对确定性基线的净增益。

### 14.3 在线指标

- Grader 调用率；
- Grader P50/P95 延迟；
- Grader 平均 Token 与成本；
- 整体 Chat P50/P95 延迟增量；
- `unknown/ungraded` 比例；
- 改写触发率和改写后成功率；
- 部分回答率；
- 澄清率；
- 错误拒答率；
- 无来源事实率；
- 用户反馈或人工抽检通过率。

### 14.4 收益判断

在线 Grader 只有同时满足以下条件才能从 Shadow 切换为 Enforce：

- 在独立验证集上显著改善最终动作或答案指标；
- 没有造成不可接受的错误拒答增长；
- Structured Output 与故障率达到门槛；
- P95 延迟和单 Turn 成本处于产品预算内；
- 相比“确定性 Controller + Grounded Generation”基线存在稳定净收益。

具体数值门槛在获得黄金集和影子基线后确定，不在缺少数据时拍脑袋设定。

---

## 15. 测试计划

### 15.1 单元测试

- Retrieval 特征提取；
- 确定性 Controller 规则矩阵；
- 所有状态到 `next_action` 的映射；
- `partial`、`conflicting` 和 `unknown`；
- 澄清与改写边界；
- 改写、Tool 和 Grader 调用上限；
- Grader 未启用时流程正常；
- Grader 故障不映射为 `relevant`；
- 旧元数据与 SSE 兼容。

### 15.2 集成测试

- 高置信结果跳过 Grader并生成；
- 无来源跳过 Grader并执行一次受控改写或停止；
- 灰区调用 Grader；
- `partial` 进入部分回答；
- `underspecified` 进入澄清；
- Query 明确且改写潜力高时只改写一次；
- 改写后不进入无限循环；
- Grader timeout/incomplete/非法 JSON 走 `unknown`；
- Retrieval 降级与知识不足使用不同原因码；
- Langfuse、数据库和 SSE 状态一致。

### 15.3 真实模型 Contract Test

- Strict Structured Output；
- 中文和英文输入；
- 长上下文；
- Prompt Injection 来源；
- 输出 token 上限；
- reasoning 配置；
- incomplete reason；
- usage、成本和 finish reason；
- 目标 Provider 的超时与错误格式。

Contract Test 不作为默认测试执行，只有显式配置真实 Provider 凭据时运行。

---

## 16. 分阶段实施

### 阶段 A：定义与基线

1. 冻结当前真实 Trace 作为回归案例。
2. 创建 `context-assessment-v1` 黄金集。
3. 标注 `sufficient / partial / insufficient / conflicting` 和允许动作。
4. 统计当前固定 Grader 的准确率、故障率、延迟和成本。
5. 确定首版风险与延迟预算。

交付物：黄金集、基线报告、阈值校准方法。

### 阶段 B：确定性 Retrieval Controller

1. 从 `RetrievalSearchResponse` 提取版本化诊断特征。
2. 新增 Controller 状态和原因码。
3. 无来源、失败、降级和硬限制首先走确定性规则。
4. 保持现有用户响应不变，先只记录 Controller 建议动作。
5. 建立 Langfuse 和 Message 元数据。

交付物：不依赖 LLM 的 Controller、单元测试和 Shadow Trace。

### 阶段 C：Context Grader v3

1. 新增证据诊断 Prompt 与严格 Schema。
2. 增加按用途的模型、reasoning、token 和 timeout 配置。
3. 实现 `unknown/ungraded` 故障语义。
4. 只对 Controller 灰区样本调用。
5. 在 Shadow 模式记录结果，不影响在线动作。

交付物：可选 Grader、Contract Test、影子评测报告。

### 阶段 D：动作接管与灰度

1. 根据 Shadow 数据确定规则阈值和 Enforce 门槛。
2. 先让 `sufficient` 与 `partial` 建议影响低风险样本。
3. 再评估是否允许 Grader 触发 Rewrite、Clarify 或 Abstain。
4. 小流量灰度并持续比较关闭与开启组。
5. 任一高风险指标退化时切回 Shadow 或 Disabled。

交付物：灰度报告、发布结论和回滚记录。

### 阶段 E：文档和旧契约清理

1. 更新 `IMPLEMENTATION_PLAN.md` 阶段 13 状态图与任务。
2. 更新 `STAGE13_AGENTIC_RAG.md`。
3. 更新配置样例、后端 README 和管理端 Trace 说明。
4. 完成一个兼容周期后移除旧 `data-context-grade` 和旧三分类控制逻辑。

---

## 17. 预计代码影响范围

| 文件/模块 | 预计改造 |
|---|---|
| `generation/agent.py` | 拆分 Retrieval Controller、Grader 调用和动作路由 |
| `generation/prompts.py` | 新增 Context Evidence v3 Prompt 与 Schema |
| `generation/provider.py` | 支持按用途 reasoning/timeout，保留 incomplete 原因 |
| `generation/service.py` | 持久化新状态、调用信息和兼容字段 |
| `retrieval/schemas.py` | 提供版本化的 Controller 特征摘要 |
| `core/settings.py` | 新增 Grader 模式、调用上限和按用途配置 |
| `api/routes/chat.py` | 输出版本化 Controller/Assessment SSE 事件 |
| `frontend/src/lib/api.ts` | 新事件类型与兼容解析 |
| `frontend/src/pages/ChatPage.tsx` | 展示面向用户的检索、核验和澄清状态 |
| `evaluation/*` | 新数据集字段、指标和报告 |
| `tests/test_stage13_agentic_rag.py` | Controller、灰区、故障和动作矩阵测试 |

首版优先复用 Message metadata，不为本次改造单独增加数据库表。只有影子评测需要大规模结构化查询时，再评估新增专用 Assessment 表。

---

## 18. 发布与回滚

支持三种运行模式：

```text
disabled：不调用在线 Grader，Controller 独立工作
shadow：调用并记录 Grader，但不影响动作
enforce：Grader 诊断可以被 Controller 用于最终动作
```

发布顺序固定为：

```text
disabled 基线
  → shadow 开发环境
  → shadow 预发布/小流量
  → enforce 低风险小流量
  → 逐步扩大
```

回滚只需将模式切换为 `shadow` 或 `disabled`，不应要求数据库回滚，也不能影响固定阶段 8 RAG 回退路径。

---

## 19. 验收标准

### 19.1 功能验收

- Context Grader 不再是所有检索请求的必经节点。
- 无来源、检索失败和明显高/低置信样本不调用 LLM Grader。
- Grader 不直接输出或控制 `next_action`。
- `partial`、`conflicting` 和 `unknown` 成为正式状态。
- Grader 故障不会被记录为 `relevant` 或其他成功判断。
- Clarify 与 Rewrite 有明确、可测试的边界。
- 当前证据不足不会被表述为整个知识库没有答案。
- 所有循环和外部模型调用都有硬上限。

### 19.2 回归案例

“你知道住众公司吗”在只有招聘公告证据时：

- 不判定为完整充分；
- 不直接宣称知识库没有公司信息；
- 不擅自把用户意图固定为公司简介；
- 允许部分回答并请求澄清；
- Grader 故障时不会静默标记为 `relevant`。

### 19.3 发布验收

- Shadow 数据证明 Grader 对最终动作或答案存在稳定净增益。
- 黄金集、真实模型 Contract Test、SSE、持久化和 Trace 测试通过。
- 延迟、成本、错误拒答和 Grader 故障率满足经评审的发布门槛。
- `disabled / shadow / enforce` 切换和回滚演练通过。

---

## 20. 非目标

本次改造不包括：

- 使用 Context Grader 证明整个知识库不存在答案；
- 引入无限 Agent 自反思或无限 Query Rewrite；
- 使用 Grader 生成最终回答；
- 将 Context Grader 替代 Ragas、人工评审或 Citation/Faithfulness 校验；
- 在没有黄金集的情况下自动学习生产阈值；
- 引入新的通用互联网搜索；
- 改变 OpenSearch、Neo4j 或 Reranker 的核心检索实现。

---

## 21. 待评审决策

进入代码实施前，需要确认：

1. 首版在线模式是否固定为 `shadow`。
2. `partial_answer` 是否允许同时附带一个澄清问题。
3. 高风险问题是否需要独立风险标签和更保守动作矩阵。
4. Grader 是否使用与主生成相同的模型，还是单独配置低延迟模型。
5. 改写后的第二次灰区结果是否允许再次调用 Grader，还是直接由 Controller 决定。
6. SSE 采用新事件名，还是对现有 `data-context-grade` 升级 Schema。
7. 影子数据达到什么样本量后评审 Enforce。

在以上决策和黄金集基线完成前，不建议让 Context Grader 的结果直接控制生产回答。
