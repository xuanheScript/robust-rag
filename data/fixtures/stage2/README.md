# Stage 2 parser fixtures

This directory contains human-readable parser contract fixtures. Binary DOCX, PPTX and XLSX
fixtures are generated deterministically in `backend/tests/test_parsing.py` so they do not become
opaque repository blobs. MinerU precision success is tested against its documented asynchronous
upload/status/ZIP contracts; `invalid.pdf` is the explicit signature-failure fixture.

| Route | Success fixture | Failure path |
| --- | --- | --- |
| TXT | `mixed.txt` | invalid UTF-8/null-byte upload tests |
| Markdown | `mixed.md` | invalid UTF-8/null-byte upload tests |
| HTML | `page.html` plus mocked MinerU `main.html` ZIP | invalid UTF-8/null-byte upload tests |
| DOCX/PPTX | generated files plus mocked precision API | invalid OOXML/API contract tests |
| XLSX | generated in parser tests | invalid OOXML upload tests |
| DOC/PPT | mocked MinerU precision API | invalid OLE/API contract tests |
| XLS | LibreOffice conversion adapter | unavailable/conversion failure |
| PDF | `mineru-content-list.json` plus mocked precision API | submit/upload/poll/output failures |
