# 阶段 4：QualityEngine 与 Dingo

阶段 4 在清洗后 Canonical Document 与后续分块之间增加版本化质量门禁。质量判断只读取清洗产物，不修改文档；完整报告写入文件存储，评估摘要、执行状态和人工操作写入 PostgreSQL。

## 执行链

```text
SchemaValidator
  → DeterministicRuleEvaluator
  → DingoRuleEvaluator（可选）
  → DingoLLMEvaluator（可选）
  → QualityPolicyEngine
```

每次评估会输出八个 0～1 的维度分数：解析完整性、文本完整性、结构完整性、重复度、信息密度、上下文完整性、来源可追溯性和检索就绪度。每个问题保留来源、评估器及其版本、严重级别、阈值和 Block 证据。

准入结果如下：

- `passed`：进入 `chunking`。
- `warning`：带警告进入 `chunking`。
- `quarantined`：停止流水线，等待人工放行、拒绝或重新评估。
- `rejected`：停止流水线并标记失败。

因此，高风险文档不会到达 Embedding。阶段 5 尚未实现，正常文档当前会停在 `chunking`。

## Dingo Adapter

项目锁定可选依赖 `dingo-python==2.5.0`。Adapter 只把官方 `EvalDetail` 转换为内部 Issue、DimensionScore 和执行审计，不让 Dingo 类型进入领域模型，也不允许 Dingo 直接改变文档状态。

默认关闭 Dingo，因此本地开发和测试不会产生模型费用。启用规则评估：

```bash
cd backend
uv sync --all-groups --extra dingo
```

```dotenv
DINGO_ENABLED=true
DINGO_RULE_ENABLED=true
DINGO_RULE_NAMES=RuleAbnormalChar,RuleAbnormalHtml,RuleContentNull
```

启用 LLM 评估时，Dingo 使用现有 OpenAI-compatible `LLM_BASE_URL` 和 `LLM_MODEL`，并单独读取密钥：

```dotenv
DINGO_LLM_ENABLED=true
DINGO_LLM_API_KEY=your-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-5.4
```

调用失败会形成失败的 QualityAssessment 和 StageRun，保存错误码、可重试标记和消息，不会伪装成质量拒绝。可以通过现有 `POST /api/v1/jobs/{job_id}/retry` 重新执行。

## 持久化与幂等

- `quality_assessments` 保存输入哈希、引擎/规则/策略版本、配置快照、维度、问题、各评估器执行结果和报告地址。
- `quality_review_actions` 保存人工动作、操作者、原因、原任务/版本/质量状态及完整质量快照。
- 报告契约为 `quality-report/1.0`，路径为 `quality/{document_id}/{version_id}/{assessment_id}/quality-report.json`。
- 同一清洗输入和相同引擎、规则、策略版本会复用成功结果；明确的重新评估操作会创建新记录。

数据库升级：

```bash
make migrate
```

## API

```text
GET  /api/v1/documents/{document_id}/quality
GET  /api/v1/documents/{document_id}/versions/{version_id}/quality-assessments
GET  /api/v1/documents/{document_id}/versions/{version_id}/quality-assessments/{assessment_id}/report
GET  /api/v1/documents/{document_id}/quality/review-actions
POST /api/v1/documents/{document_id}/release
POST /api/v1/documents/{document_id}/reject
POST /api/v1/documents/{document_id}/quality/re-evaluate
```

三个人工操作接口的请求体均为：

```json
{
  "actor": "local-admin",
  "reason": "已对照原始文件复核"
}
```

放行和拒绝仅接受当前为 `quarantined` 的评估；重新评估接受 `quarantined` 或 `rejected`。所有操作均不可静默覆盖原质量结论，而是追加审计记录。

## 验证

```bash
make check
```

常规测试使用内置 FakeDingoAdapter，不访问 Dingo、外部 LLM API 或其他外部服务。Dingo 官方 SDK 的状态、分数和 Token Usage 转换由无网络契约测试覆盖。
