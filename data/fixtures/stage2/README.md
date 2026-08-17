# Stage 2 parser fixtures

This directory contains human-readable parser contract fixtures. Binary DOCX, PPTX and XLSX
fixtures are generated deterministically in `backend/tests/test_parsing.py` so they do not become
opaque repository blobs. PDF success is tested against MinerU's documented `content_list.json`
contract; `invalid.pdf` is the explicit signature-failure fixture.

| Route | Success fixture | Failure path |
| --- | --- | --- |
| TXT | `mixed.txt` | invalid UTF-8/null-byte upload tests |
| Markdown | `mixed.md` | invalid UTF-8/null-byte upload tests |
| HTML | `page.html` | invalid UTF-8/null-byte upload tests |
| DOCX/PPTX/XLSX | generated in parser tests | invalid OOXML upload tests |
| DOC/PPT/XLS | LibreOffice conversion adapter | unavailable/conversion failure |
| PDF | `mineru-content-list.json` | `invalid.pdf`, MinerU request/output failures |
