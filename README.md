# Kova Finance

An internal operations tool for a Portuguese mortgage intermediary — document organisation, AI-assisted search over client dossiers, mortgage simulation, and client correspondence, in one self-hosted app.

Built and maintained solo, with [Claude Code](https://claude.com/claude-code). It runs in daily operations rather than as a demo.

## Why it exists

Mortgage intermediation runs on paperwork. A single client dossier can hold dozens of PDFs — payslips, bank statements, tax returns, property deeds, credit reports — spread across folders, in two languages, with a compliance requirement that specific categories are present before a file can be submitted.

The manual work was real but was never going to justify a place on a product roadmap. It needed a tool, not a project.

## What it does

**Document organisation.** Watches client folders on disk, normalises them into a standard structure (`Documentos Pessoais`, `Rendimentos`, `Extratos Bancários`, `Mapa CRC`, `RGPD`, …) and flags which required categories are still missing, per person and per dossier.

**Indexed search.** Extracts text from PDFs and images, indexes it in SQLite full-text search, and makes the whole dossier searchable.

**AI Q&A over documents.** Ask a question in Portuguese or English and get an answer grounded in the client's own documents, with citations. Uses the Claude API, and degrades to plain FTS excerpts when no key is configured.

The prompt design is the interesting part. Extracted PDF text loses the column layout of the original form, so a joint tenancy agreement can read as one person's income when it is actually two people's. The assistant is instructed to detect that ambiguity and refuse to produce a confident number — naming the second party it found and telling the operator to check the original document. It applies the same treatment to non-recurring payments (*subsídio de férias*, *subsídio de Natal*, back-pay) which otherwise inflate whichever month they land in.

That behaviour is deliberate: for this use case, a wrong number stated confidently is far more expensive than a hedge.

**Mortgage simulation.** French-amortisation schedules for fixed and variable rates, with a generated PDF for the client.

**Correspondence.** Gmail integration for sending and tracking dossier-related email, plus PDF packaging of a complete dossier for submission.

## Stack

- **Backend** — Python, FastAPI, SQLite (with FTS), Jinja2 templates
- **AI** — Claude API, retrieval over locally indexed document text
- **Integrations** — Gmail API
- **Mobile shell** — Capacitor (`kova-app/`)
- **Auth** — JWT sessions with a persisted secret

## Layout

```
webapp/
  main.py           FastAPI app, startup sync + indexing
  routers/          HTTP routes by domain (clients, documents, ai, simulation, gmail, …)
  services/
    scanner.py      folder sync and normalisation
    indexer.py      text extraction and FTS indexing
    identifier.py   client identification from folder names
    ai_service.py   prompt construction and grounded Q&A
    simulation.py   amortisation maths and PDF output
    watcher.py      filesystem change detection
  templates/        server-rendered UI
kova-app/           Capacitor wrapper
```

## Running it

Configure paths in `webapp/config.py`, then:

```bash
pip install -r requirements.txt
python webapp/run.py
```

An admin account is created on first start. The Claude API key is set in-app under Settings; without one, AI answers fall back to search excerpts.

## Notes

Client data lives outside the repository and is never committed. Paths in `config.py` point at a local working directory and are meant to be changed.

This is working internal software, not a library — published so the approach is inspectable, not for reuse as-is.
