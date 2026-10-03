# Password-protected synthetic PDFs

Synthetic, deterministic input for `PDF_INGEST_PASSWORD` tests — no real-world content.
Each file is the same three-page PDF (`authored_pdf(page_count=3, label="Vesper 1H26 Hong Kong",
embedded_font=True)` from `tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py`, page *n*
prints `Vesper 1H26 Hong Kong page n`), encrypted by qpdf 12.3 **with object streams**, the way
real-world producers write them: before authentication pdfspine sees `page_count == 0`, not
just empty pages.

- user password: `zq-synthetic-pdf-pw-7Q4`
- owner password: `zq-synthetic-owner-pw-3K9`

qpdf is not a project dependency; the files were generated once and committed. To regenerate:

```bash
.venv/bin/python -c "from pathlib import Path; from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import authored_pdf; authored_pdf(Path('plain.pdf'), page_count=3, label='Vesper 1H26 Hong Kong', embedded_font=True)"
qpdf --static-id --static-aes-iv --object-streams=generate \
  --encrypt zq-synthetic-pdf-pw-7Q4 zq-synthetic-owner-pw-3K9 256 -- plain.pdf aes256-objstm.pdf
qpdf --static-id --allow-weak-crypto --object-streams=generate \
  --encrypt zq-synthetic-pdf-pw-7Q4 zq-synthetic-owner-pw-3K9 128 --use-aes=n -- plain.pdf rc4-128-objstm.pdf
```

AES-256 still draws random salts, so that file is not byte-identical across regenerations.
