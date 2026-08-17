# Robust RAG

面向中英双语通用企业知识库的完整 RAG 项目。当前处于阶段 0：工程骨架与开发基线。

完整实施方案见 [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)。

## 当前工程组成

- `backend/`：FastAPI、Celery Worker、配置、日志与测试基线。
- `frontend/`：Vite、React 19、TypeScript 与前端测试基线。
- `data/`：本地原始文件和派生产物目录，运行时内容不提交 Git。
- `evals/`：黄金集、Rubric 与评测报告。

## 本机前置条件

- Python 3.12
- uv
- Node.js 22+
- pnpm 10+
- PostgreSQL 17（阶段 1 开始使用）
- Redis 8（Celery Broker）

## 初始化

```bash
cp .env.example .env
make setup
```

创建 PostgreSQL 数据库将在阶段 1 完成；阶段 0 的 `/health/live` 不依赖数据库。

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

## 工程检查

```bash
make check
```

默认检查不会调用 Voyage、GPT、MinerU、Dingo 或 Aiven，不产生外部服务费用。
