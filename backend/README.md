# Robust RAG Backend

FastAPI API、SQLAlchemy/Alembic、LocalFileStorage、Celery Worker、Parser Router 与 Canonical Document 实现。完整项目说明见仓库根目录的 `README.md`。

常用命令从仓库根目录执行：

```bash
make migrate
make api
make worker
make migration-check
```

PDF 解析需要在 `.env` 中配置 `MINERU_BASE_URL`。其他现代 Office、HTML、Markdown 与 TXT 格式无需额外解析服务；旧版 Office 文件需要 `soffice`。
