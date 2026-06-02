"""AI Q&A — uses Claude API when key available, FTS excerpts as fallback."""
import json, re
from database import setting
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

def _to_fts_query(text: str) -> str:
    """
    Convert a natural-language question to an FTS5-safe keyword query.
    Strips stop words, quotes each remaining token, joins with OR so partial
    matches still surface relevant results.
    """
    tokens = re.findall(r'\w+', text.lower())
    keywords = [t for t in tokens if t not in _STOP and len(t) > 2]
    if not keywords:
        keywords = [t for t in tokens if len(t) > 2]  # fallback: skip only very short words
    # Quote tokens so special FTS chars are safe; OR so any hit surfaces
    return " OR ".join(f'"{k}"' for k in keywords[:10])

def _build_context(query: str, client_id: int = None) -> tuple[list, list]:
    fts_q = _to_fts_query(query)
    try:
        results = search(fts_q, client_id=client_id, limit=8)
    except Exception:
        results = []
    # If keyword search missed, fall back to raw query as a phrase
    if not results:
        try:
            results = search(f'"{query}"', client_id=client_id, limit=8)
        except Exception:
            results = []
    if not results:
        return [], []
    context_parts = []
    for r in results:
        snippet = r.get("snippet") or ""
        context_parts.append(
            f"[{r['folder_name']} / {r['category'] or 'root'} / {r['filename']}]\n{snippet}"
        )
    return context_parts, results

def answer_stream(query: str, client_id: int = None, history: list = None):
    """
    Yields chunks of text (str) or a final JSON sources block.
    If no API key, yields FTS excerpts formatted as text.
    """
    api_key = setting("anthropic_api_key")
    context_parts, sources = _build_context(query, client_id)

    if not api_key:
        # Fallback: return excerpts directly
        if not context_parts:
            yield "No matching documents found. Try different keywords or run the indexer."
            return
        yield "**Relevant document excerpts** (configure API key in Settings for AI answers):\n\n"
        for part in context_parts:
            yield f"---\n{part}\n"
        return

    # Build messages for Claude
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        yield f"Could not initialise AI client: {e}"
        return

    context_text = "\n\n".join(context_parts) if context_parts else "No relevant documents found."
    messages = []
    if history:
        for msg in history[-6:]:  # last 3 exchanges
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({
        "role": "user",
        "content": f"Document context:\n{context_text}\n\nQuestion: {query}"
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

    # Append sources as a special sentinel
    if sources:
        src_list = [{"file": s["filename"], "client": s["folder_name"], "category": s["category"]} for s in sources]
        yield f"\n\n<!--SOURCES:{json.dumps(src_list)}-->"
