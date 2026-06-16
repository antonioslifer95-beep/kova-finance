"""PDF package generation: per-person merged dossiers + one-page summary sheet."""
import json, re
from pathlib import Path
from datetime import date
from typing import Optional
from database import get_db, setting
from config import STANDARD_FOLDERS

_CAT_ORDER = {cat: i for i, cat in enumerate(STANDARD_FOLDERS)}


def _safe(s: str) -> str:
    return re.sub(r'[^\w\-]', '_', (s or "").strip())


# ── Person / document helpers ─────────────────────────────────────────────────

def get_persons(client_id: int) -> list:
    """
    Return [{key, name, is_fiador, docs[]}] sorted: applicants first, fiador last.
    key is the subclient column value ("" for single-person clients).
    """
    with get_db() as db:
        rows = db.execute(
            """SELECT abs_path, category, filename, subclient, mime_type
               FROM documents
               WHERE client_id=? AND mime_type IN ('application/pdf','image/jpeg','image/png')
               ORDER BY subclient, category, filename""",
            (client_id,)
        ).fetchall()

    buckets = {}
    for r in rows:
        sc = r["subclient"] or ""
        if sc not in buckets:
            buckets[sc] = []
        buckets[sc].append(dict(r))

    persons = []
    for sc, docs in sorted(buckets.items(), key=lambda kv: (kv[0].lower() == "fiador", kv[0])):
        persons.append({
            "key":       sc,
            "name":      sc or "Requerente",
            "is_fiador": sc.lower() == "fiador",
            "docs":      sorted(docs, key=lambda d: (_CAT_ORDER.get(d["category"] or "", 99), d["filename"])),
        })
    return persons


def extract_person_data(client_id: int, subclient_key: str) -> dict:
    """
    Use Claude Haiku to pull name, age, NIF, monthly_income, crc_total
    from that person's indexed documents. Returns {} if no API key or no docs.
    """
    api_key = setting("anthropic_api_key")
    if not api_key:
        return {}

    with get_db() as db:
        if subclient_key:
            rows = db.execute(
                """SELECT fts.body, d.category, d.filename
                   FROM documents_fts fts
                   JOIN documents d ON d.id = fts.doc_id
                   WHERE d.client_id=? AND d.subclient=?
                     AND d.category IN ('Documentos Pessoais','Rendimentos','Mapa CRC')
                     AND fts.body != ''
                   ORDER BY d.category""",
                (client_id, subclient_key)
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT fts.body, d.category, d.filename
                   FROM documents_fts fts
                   JOIN documents d ON d.id = fts.doc_id
                   WHERE d.client_id=? AND d.subclient IS NULL
                     AND d.category IN ('Documentos Pessoais','Rendimentos','Mapa CRC')
                     AND fts.body != ''
                   ORDER BY d.category""",
                (client_id,)
            ).fetchall()

    if not rows:
        return {}

    context = "\n\n".join(
        f"[{r['category']} / {r['filename']}]\n{r['body'][:800]}"
        for r in rows[:12]
    )
    today = date.today().isoformat()
    prompt = (
        f"From these Portuguese mortgage application documents extract the following. "
        f"Return ONLY a valid JSON object with these keys (null if not found):\n"
        f'- "name": full legal name\n'
        f'- "age": integer age (calculate from birth date if needed; today is {today})\n'
        f'- "nif": 9-digit NIF\n'
        f'- "monthly_income": average monthly gross income in euros as a number\n'
        f'- "crc_total": total monthly credit responsibilities in euros as a number\n\n'
        f"Documents:\n{context}"
    )
    try:
        import anthropic
        ai = anthropic.Anthropic(api_key=api_key)
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        text = re.sub(r'^```(?:json)?', '', text).rstrip('`').strip()
        return json.loads(text)
    except Exception:
        return {}


# ── Merged dossier PDF ────────────────────────────────────────────────────────

def merge_person_pdf(docs: list, output_path: str) -> int:
    """Merge a person's documents (in category order) into one PDF. Returns page count."""
    import fitz
    merged = fitz.open()
    for d in docs:
        path = d["abs_path"]
        ext = Path(path).suffix.lower()
        try:
            if ext == ".pdf":
                with fitz.open(path) as src:
                    merged.insert_pdf(src)
            elif ext in {".jpg", ".jpeg", ".png"}:
                page = merged.new_page(width=595, height=842)
                page.insert_image(page.rect, filename=path)
        except Exception:
            pass
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    merged.save(output_path)
    count = len(merged)
    merged.close()
    return count


# ── Summary PDF ───────────────────────────────────────────────────────────────

def generate_summary_pdf(persons_data: list, operation: dict, output_path: str, client_name: str):
    """
    One-page summary sheet.
    persons_data: list of dicts with keys name, age, nif, phone, email,
                  monthly_income, crc_total, is_fiador.
    operation: {mortgage_amount, property_value}.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as rl_canvas

    W, H = A4
    LM, RM = 15 * mm, W - 15 * mm
    CW = RM - LM

    # Palette
    NAVY      = colors.HexColor("#1a3560")
    ACCENT    = colors.HexColor("#2563eb")
    CARD_BG   = colors.HexColor("#f8fafc")
    OP_BG     = colors.HexColor("#eff6ff")
    BORDER    = colors.HexColor("#cbd5e1")
    LABEL_C   = colors.HexColor("#64748b")
    VALUE_C   = colors.HexColor("#0f172a")
    FIADOR_H  = colors.HexColor("#475569")
    WHITE     = colors.white

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    c = rl_canvas.Canvas(output_path, pagesize=A4)

    # ── Header band ──────────────────────────────────────────────────────────
    HDR = 52
    c.setFillColor(NAVY)
    c.rect(0, H - HDR, W, HDR, fill=1, stroke=0)

    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(LM, H - 19 * mm, "KOVA FINANCE")
    c.setFont("Helvetica", 9)
    c.drawString(LM, H - 26 * mm, "Ficha de Cliente  ·  Crédito à Habitação Própria Permanente")
    c.setFont("Helvetica", 8.5)
    c.drawRightString(RM, H - 19 * mm, date.today().strftime("%d / %m / %Y"))
    c.setFont("Helvetica", 8)
    c.drawRightString(RM, H - 26 * mm, client_name)

    y = H - HDR - 8 * mm

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _section_heading(label, width_mm=40):
        nonlocal y
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(LM, y, label)
        y -= 3 * mm
        c.setStrokeColor(ACCENT)
        c.setLineWidth(1.5)
        c.line(LM, y, LM + width_mm * mm, y)
        c.setLineWidth(0.5)
        y -= 5 * mm

    def _field(cx, fy, label, value):
        c.setFillColor(LABEL_C)
        c.setFont("Helvetica", 7)
        c.drawString(cx, fy + 9, label.upper())
        c.setFillColor(VALUE_C)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(cx, fy, str(value) if value else "—")

    def _fmt_eur(v):
        if v in (None, "", "None"):
            return "—"
        try:
            return "€ {:,.0f}".format(float(v)).replace(",", " ")
        except Exception:
            return str(v)

    def _fmt_nif(v):
        s = re.sub(r'\D', '', str(v or ""))
        return f"{s[:3]} {s[3:6]} {s[6:]}" if len(s) == 9 else (s or "—")

    def _fmt_age(v):
        return f"{v} anos" if v and str(v) != "None" else "—"

    # ── Person cards ─────────────────────────────────────────────────────────
    applicants = [p for p in persons_data if not p.get("is_fiador")]
    fiadores   = [p for p in persons_data if p.get("is_fiador")]

    FIELD_H   = 22      # pts per field row
    APP_FIELDS = 7      # name, age, nif, phone, email, income, crc
    CARD_HDR  = 20      # coloured tab
    PADDING   = 14      # top+bottom inner padding
    GAP       = 4 * mm  # gap between cards

    def _draw_person_cards(persons, fields_fn, n_fields, tab_color, role_prefix):
        nonlocal y
        n = len(persons)
        card_w  = (CW - GAP * (n - 1)) / max(n, 1)
        card_h  = CARD_HDR + PADDING + n_fields * FIELD_H
        card_y  = y - card_h

        for i, p in enumerate(persons):
            cx = LM + i * (card_w + GAP)

            # Card box
            c.setFillColor(CARD_BG)
            c.setStrokeColor(BORDER)
            c.roundRect(cx, card_y, card_w, card_h, 4, fill=1, stroke=1)

            # Coloured header tab
            c.setFillColor(tab_color)
            c.roundRect(cx, card_y + card_h - CARD_HDR, card_w, CARD_HDR, 4, fill=1, stroke=0)
            # cover bottom-rounded corners of tab
            c.rect(cx, card_y + card_h - CARD_HDR, card_w, CARD_HDR / 2, fill=1, stroke=0)
            c.setFillColor(WHITE)
            c.setFont("Helvetica-Bold", 9)
            tab_label = f"{role_prefix} {i+1}" if n > 1 else role_prefix
            c.drawString(cx + 4 * mm, card_y + card_h - CARD_HDR + 6, tab_label)

            # Fields
            fx  = cx + 4 * mm
            fy  = card_y + card_h - CARD_HDR - PADDING / 2 - FIELD_H
            for label, value in fields_fn(p):
                _field(fx, fy, label, value)
                fy -= FIELD_H

        y = card_y - 6 * mm

    def _applicant_fields(p):
        return [
            ("Nome",                      p.get("name")),
            ("Idade",                     _fmt_age(p.get("age"))),
            ("NIF",                       _fmt_nif(p.get("nif"))),
            ("Telefone",                  p.get("phone") or "—"),
            ("Email",                     p.get("email") or "—"),
            ("Rendimento Mensal Médio",   _fmt_eur(p.get("monthly_income"))),
            ("Responsabilidades Crédito", _fmt_eur(p.get("crc_total"))),
        ]

    def _fiador_fields(p):
        return [
            ("Nome",                      p.get("name")),
            ("NIF",                       _fmt_nif(p.get("nif"))),
            ("Telefone",                  p.get("phone") or "—"),
            ("Email",                     p.get("email") or "—"),
            ("Rendimento Mensal Médio",   _fmt_eur(p.get("monthly_income"))),
            ("Responsabilidades Crédito", _fmt_eur(p.get("crc_total"))),
        ]

    if applicants:
        _section_heading("REQUERENTES", 42)
        _draw_person_cards(applicants, _applicant_fields, APP_FIELDS, ACCENT, "REQUERENTE")

    if fiadores:
        _section_heading("FIADOR", 20)
        _draw_person_cards(fiadores, _fiador_fields, 6, FIADOR_H, "FIADOR")

    # ── Operation box ─────────────────────────────────────────────────────────
    _section_heading("OPERAÇÃO DE CRÉDITO", 55)

    mortgage  = operation.get("mortgage_amount")
    prop_val  = operation.get("property_value")
    col3      = CW / 3

    # Compute aggregates
    try:
        total_income = sum(float(p.get("monthly_income") or 0) for p in persons_data)
    except Exception:
        total_income = 0
    try:
        total_crc = sum(float(p.get("crc_total") or 0) for p in persons_data)
    except Exception:
        total_crc = 0

    op_h = 52
    op_y = y - op_h
    c.setFillColor(OP_BG)
    c.setStrokeColor(BORDER)
    c.roundRect(LM, op_y, CW, op_h, 4, fill=1, stroke=1)

    row1_y = op_y + op_h - 14
    row2_y = op_y + 8

    _field(LM + 5 * mm, row1_y, "Montante Solicitado",  _fmt_eur(mortgage))
    _field(LM + col3 + 5 * mm, row1_y, "Valor do Imóvel", _fmt_eur(prop_val))
    if mortgage and prop_val:
        try:
            ltv = float(mortgage) / float(prop_val) * 100
            _field(LM + 2 * col3 + 5 * mm, row1_y, "Rácio LTV", f"{ltv:.1f}%")
        except Exception:
            pass

    _field(LM + 5 * mm, row2_y, "Rendimento Total Agregado", _fmt_eur(total_income) if total_income else "—")
    _field(LM + col3 + 5 * mm, row2_y, "Total Responsab. Crédito", _fmt_eur(total_crc) if total_crc else "—")
    if total_income:
        try:
            effort = total_crc / total_income * 100
            _field(LM + 2 * col3 + 5 * mm, row2_y, "Taxa de Esforço Actual", f"{effort:.1f}%")
        except Exception:
            pass

    y = op_y - 6 * mm

    # ── Footer ────────────────────────────────────────────────────────────────
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.line(LM, 18 * mm, RM, 18 * mm)
    c.setFillColor(LABEL_C)
    c.setFont("Helvetica", 7.5)
    c.drawString(LM, 13 * mm, "Kova Finance  ·  Intermediário de Crédito  ·  Documento gerado automaticamente")
    c.drawRightString(RM, 13 * mm, f"Gerado em {date.today().strftime('%d/%m/%Y')}")

    c.save()
