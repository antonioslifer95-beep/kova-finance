"""AI Q&A — uses Claude API when key available, FTS excerpts as fallback."""
import json, re, difflib
from typing import Optional
from database import setting, get_db
from services.indexer import search

SYSTEM_PROMPT = """You are a helpful assistant for Kova Finance, a Portuguese mortgage intermediary.
You have access to document extracts from client dossiers (PDFs, payslips, bank statements, property docs, etc.).
Answer questions accurately and concisely based only on the provided document context.
If you cannot find the answer in the context, say so clearly.
Always cite which document the information comes from.
Respond in the same language the user writes in (Portuguese or English)."""

# Common stop words to strip before FTS query
_STOP = {
    "what","is","the","of","a","an","in","for","to","and","or","can","you",
    "give","me","tell","show","find","who","how","when","where","do","does",
    "qual","o","a","de","da","do","em","para","com","que","um","uma","me",
    "diz","qual","quais","foi","tem","seu","sua","seus","suas",
}

# Keywords that indicate a specific document category
_CATEGORY_SIGNALS = {
    "Rendimentos": {
        "income", "salary", "payslip", "rendimento", "rendimentos", "salário",
        "salario", "vencimento", "recibo", "ordenado", "remuneração", "remuneracao",
        "wage", "wages", "pay", "earning", "earnings",
    },
    "Extratos Bancários": {
        "bank", "statement", "extrato", "extratos", "bancário", "bancario",
        "saldo", "balance", "transaction", "account", "conta",
    },
    "IRS": {
        "irs", "tax", "imposto", "fiscal", "declaração", "declaracao", "iRS",
    },
    "Imóvel": {
        "property", "imóvel", "imovel", "house", "casa", "escritura", "cpcv",
        "habitação", "habitacao", "artigo", "matricial",
    },
    "Documentos Pessoais": {
        "nif", "cc", "passport", "passaporte", "bi", "identity", "identificação",
        "identificacao", "citizen", "cidadão", "cidadao",
    },
    "Mapa CRC": {
        "crc", "crédito", "credito", "credit", "responsabilidades",
    },
}


def _to_fts_query(text: str) -> str:
    tokens = re.findall(r'\w+', text.lower())
    keywords = [t for t in tokens if t not in _STOP and len(t) > 2]
    if not keywords:
        keywords = [t for t in tokens if len(t) > 2]
    return " OR ".join(f'"{k}"' for k in keywords[:10])


def _detect_intent_category(query: str) -> Optional[str]:
    """Return the document category most relevant to the query, if any."""
    q_words = set(re.findall(r'\w+', query.lower()))
    for category, signals in _CATEGORY_SIGNALS.items():
        if q_words & signals:
            return category
    return None


def _resolve_client_from_query(query: str) -> tuple:
    """
    Detect a client mentioned by name in the query using fuzzy token matching.
    Returns (client_id, folder_name) or (None, None).
    """
    with get_db() as db:
        clients = db.execute(
            "SELECT id, folder_name FROM clients WHERE is_standby=0"
        ).fetchall()

    # Normalise query: strip possessives, map "and" → "e"
    q = query.lower()
    q = re.sub(r"'s?\b", "", q)
    q = re.sub(r"\band\b", "e", q)
    q_words = re.findall(r'\w+', q)

    best_id, best_name, best_score = None, None, 0.0  # type: ignore[assignment]

    for c in clients:
        # Skip short connector tokens like "e"
        folder_tokens = [t for t in re.findall(r'\w+', c["folder_name"].lower()) if len(t) > 2]
        if not folder_tokens:
            continue

        matched = 0
        for ft in folder_tokens:
            for qw in q_words:
                if difflib.SequenceMatcher(None, ft, qw).ratio() >= 0.85:
                    matched += 1
                    break

        if matched == 0:
            continue

        fraction = matched / len(folder_tokens)
        # Multi-token folders need at least 2 name tokens to match (avoids false positives
        # on common first names like "Ana" matching many queries)
        min_required = min(2, len(folder_tokens))
        if matched >= min_required and fraction > best_score:
            best_score = fraction
            best_id = c["id"]
            best_name = c["folder_name"]

    return best_id, best_name


def _fetch_category_docs(client_id: int, category: str, limit: int = 8) -> list:
    """Fetch documents directly from a specific category for a client."""
    with get_db() as db:
        rows = db.execute(
            """SELECT d.id, d.filename, d.abs_path, d.category, c.folder_name,
                      fts.body AS snippet
               FROM documents d
               JOIN clients c ON c.id = d.client_id
               LEFT JOIN documents_fts fts ON fts.doc_id = d.id
               WHERE d.client_id=? AND d.category=?
               ORDER BY d.file_mtime DESC LIMIT ?""",
            (client_id, category, limit)
        ).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        body = d.get("snippet") or ""
        d["snippet"] = body[:1500] if body else f"[Image file — no text extracted: {d['filename']}]"
        results.append(d)
    return results


def _build_context(query: str, client_id: int = None) -> tuple:
    """
    Returns (context_parts, sources, resolved_client_id, auto_detected_name).
    auto_detected_name is set only when the client was inferred from the query text.
    """
    auto_name = None
    resolved_id = client_id

    if not client_id:
        resolved_id, auto_name = _resolve_client_from_query(query)

    intent_category = _detect_intent_category(query)

    # FTS keyword search
    fts_q = _to_fts_query(query)
    results = []
    try:
        results = search(fts_q, client_id=resolved_id, limit=6)
    except Exception:
        pass
    if not results:
        try:
            results = search(f'"{query}"', client_id=resolved_id, limit=6)
        except Exception:
            pass

    # Supplement with direct category fetch when intent is clear
    if intent_category and resolved_id:
        existing_ids = {r["id"] for r in results}
        for d in _fetch_category_docs(resolved_id, intent_category, limit=8):
            if d["id"] not in existing_ids:
                results.append(d)
                existing_ids.add(d["id"])

    if not results:
        return [], [], resolved_id, auto_name

    context_parts = [
        f"[{r['folder_name']} / {r['category'] or 'root'} / {r['filename']}]\n{r.get('snippet', '')}"
        for r in results
    ]
    return context_parts, results, resolved_id, auto_name


def answer_stream(query: str, client_id: int = None, history: list = None):
    """
    Yields chunks of text (str) or a final JSON sources block.
    If no API key, yields FTS excerpts formatted as text.
    """
    api_key = setting("anthropic_api_key")
    context_parts, sources, resolved_id, auto_name = _build_context(query, client_id)

    if not api_key:
        if not context_parts:
            yield "No matching documents found. Try different keywords or run the indexer."
            return
        yield "**Relevant document excerpts** (configure API key in Settings for AI answers):\n\n"
        for part in context_parts:
            yield f"---\n{part}\n"
        return

    # Signal to the UI which client was auto-detected (before streaming begins)
    if auto_name:
        yield f"<!--CLIENT:{auto_name}-->"

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        yield f"Could not initialise AI client: {e}"
        return

    context_text = "\n\n".join(context_parts) if context_parts else "No relevant documents found."

    # Prefix to tell Claude which client was inferred from the question
    auto_note = f"[Identified client from question: {auto_name}]\n" if auto_name else ""

    messages = []
    if history:
        for msg in history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({
        "role": "user",
        "content": f"{auto_note}Document context:\n{context_text}\n\nQuestion: {query}"
    })

    try:
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield text
    except Exception as e:
        yield f"\n\n*Error calling AI: {e}*"
        return

    if sources:
        src_list = [
            {"file": s["filename"], "client": s["folder_name"], "category": s["category"]}
            for s in sources
        ]
        yield f"\n\n<!--SOURCES:{json.dumps(src_list)}-->"
