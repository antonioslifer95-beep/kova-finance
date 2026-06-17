"""Mortgage simulation: amortization calculations + PDF generation."""
from __future__ import annotations
from datetime import date
from pathlib import Path


# ── Calculations ──────────────────────────────────────────────────────────────

def _pmt(principal: float, annual_rate_pct: float, term_months: int) -> float:
    if term_months <= 0:
        return 0.0
    if annual_rate_pct <= 0:
        return principal / term_months
    r = annual_rate_pct / 100 / 12
    return principal * r / (1 - (1 + r) ** (-term_months))


def build_schedule(principal: float, annual_rate_pct: float, term_months: int) -> list[dict]:
    """Standard French-amortization schedule (fixed or variable rate)."""
    payment = _pmt(principal, annual_rate_pct, term_months)
    balance = principal
    rows = []
    for m in range(1, term_months + 1):
        r = annual_rate_pct / 100 / 12
        interest = balance * r
        capital = min(payment - interest, balance)
        balance = max(0.0, balance - capital)
        rows.append({
            "month":    m,
            "payment":  round(payment, 2),
            "capital":  round(capital, 2),
            "interest": round(interest, 2),
            "balance":  round(balance, 2),
            "phase":    1,
        })
    return rows


def build_mixed_schedule(
    principal: float,
    fixed_tan: float,
    fixed_months: int,
    variable_tan: float,
    total_months: int,
) -> list[dict]:
    """
    Phase 1: fixed_tan for fixed_months, monthly payment calculated over total_months.
    Phase 2: variable_tan on remaining balance for (total_months - fixed_months).
    """
    pmt1 = _pmt(principal, fixed_tan, total_months)
    balance = principal
    rows = []

    for m in range(1, fixed_months + 1):
        r = fixed_tan / 100 / 12
        interest = balance * r
        capital = min(pmt1 - interest, balance)
        balance = max(0.0, balance - capital)
        rows.append({"month": m, "payment": round(pmt1, 2), "capital": round(capital, 2),
                     "interest": round(interest, 2), "balance": round(balance, 2), "phase": 1})

    remaining = total_months - fixed_months
    if remaining > 0 and balance > 0.01:
        pmt2 = _pmt(balance, variable_tan, remaining)
        for m in range(fixed_months + 1, total_months + 1):
            r = variable_tan / 100 / 12
            interest = balance * r
            capital = min(pmt2 - interest, balance)
            balance = max(0.0, balance - capital)
            rows.append({"month": m, "payment": round(pmt2, 2), "capital": round(capital, 2),
                         "interest": round(interest, 2), "balance": round(balance, 2), "phase": 2})

    return rows


def schedule_summary(rows: list[dict], principal: float = None) -> dict:
    total_paid     = sum(r["payment"] for r in rows)
    total_interest = sum(r["interest"] for r in rows)
    total_capital  = principal if principal is not None else round(total_paid - total_interest, 2)
    return {
        "total_paid":     round(total_paid, 2),
        "total_interest": round(total_interest, 2),
        "total_capital":  total_capital,
    }


def build_display_schedule(rows: list[dict], rate_type: str) -> list[dict]:
    """Condense monthly schedule: first 12 of each phase monthly, rest annual.
    For mixed: first 12 fixed monthly → fixed annual totals → first 12 variable monthly → variable annual totals.
    """
    def _monthly(phase_rows):
        return [{
            "label":     str(r["month"]),
            "payment":   r["payment"],
            "capital":   r["capital"],
            "interest":  r["interest"],
            "balance":   r["balance"],
            "phase":     r.get("phase", 1),
            "is_annual": False,
        } for r in phase_rows[:12]]

    def _annual(phase_rows, start_year, start_idx=12):
        result, i, yr = [], start_idx, start_year
        while i < len(phase_rows):
            chunk = phase_rows[i:i+12]
            result.append({
                "label":     f"Ano {yr}",
                "payment":   round(sum(r["payment"]  for r in chunk), 2),
                "capital":   round(sum(r["capital"]  for r in chunk), 2),
                "interest":  round(sum(r["interest"] for r in chunk), 2),
                "balance":   chunk[-1]["balance"],
                "phase":     chunk[0].get("phase", 1),
                "is_annual": True,
            })
            i += 12
            yr += 1
        return result

    if rate_type != "mixed":
        return _monthly(rows) + _annual(rows, start_year=2)

    p1 = [r for r in rows if r.get("phase") == 1]
    p2 = [r for r in rows if r.get("phase") == 2]
    p1_years = (len(p1) + 11) // 12
    disp = _monthly(p1) + _annual(p1, start_year=2)
    if p2:
        disp += _monthly(p2) + _annual(p2, start_year=p1_years + 2)
    return disp


# ── PDF ───────────────────────────────────────────────────────────────────────

def generate_simulation_pdf(data: dict, schedule: list[dict], summary: dict, output_path: str) -> None:
    """
    data keys: client_name, operation_type, persons[], finance_amount, property_value,
               term_months, rate_type, euribor_period, euribor, spread, fixed_tan, fixed_months.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as rl_canvas

    W, H = A4
    LM, RM = 15 * mm, W - 15 * mm
    CW = RM - LM

    NAVY    = colors.HexColor("#1a3560")
    ACCENT  = colors.HexColor("#2563eb")
    BORDER  = colors.HexColor("#cbd5e1")
    LABEL_C = colors.HexColor("#64748b")
    VALUE_C = colors.HexColor("#0f172a")
    LIGHT   = colors.HexColor("#f8fafc")
    BLUE_BG = colors.HexColor("#eff6ff")
    PHASE2  = colors.HexColor("#dbeafe")
    WHITE   = colors.white

    HDR_H  = 48
    FOOT_Y = 13 * mm

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    c = rl_canvas.Canvas(output_path, pagesize=A4)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _header(page_num):
        c.setFillColor(NAVY)
        c.rect(0, H - HDR_H, W, HDR_H, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont("Helvetica-Bold", 15)
        c.drawString(LM, H - 16 * mm, "KOVA FINANCE")
        c.setFont("Helvetica", 8)
        c.drawString(LM, H - 23 * mm, "Simulação de Crédito à Habitação")
        c.drawRightString(RM, H - 16 * mm, date.today().strftime("%d / %m / %Y"))
        c.drawRightString(RM, H - 23 * mm, data.get("client_name", ""))
        if page_num > 1:
            c.setFillColor(colors.HexColor("#94a3b8"))
            c.setFont("Helvetica", 7)
            c.drawRightString(RM, H - HDR_H + 3, f"pág. {page_num}")

    def _footer(page_num):
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.4)
        c.line(LM, FOOT_Y + 5, RM, FOOT_Y + 5)
        c.setFillColor(LABEL_C)
        c.setFont("Helvetica", 6.5)
        c.drawString(LM, FOOT_Y, "Kova Finance  ·  Intermediário de Crédito  ·  Simulação indicativa — não constitui oferta vinculativa")
        c.drawRightString(RM, FOOT_Y, f"pág. {page_num}")

    def _section(title, y, span_mm=50):
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(LM, y, title)
        y -= 3 * mm
        c.setStrokeColor(ACCENT)
        c.setLineWidth(1.5)
        c.line(LM, y, LM + span_mm * mm, y)
        c.setLineWidth(0.4)
        return y - 4 * mm

    def _eur(v):
        if v is None:
            return "—"
        try:
            s = "{:,.2f}".format(float(v))
            int_part, dec_part = s.split(".")
            return "€ " + int_part.replace(",", ".") + "," + dec_part
        except Exception:
            return str(v)

    def _pct(v):
        if v is None:
            return "—"
        try:
            return f"{float(v):.3f}%"
        except Exception:
            return str(v)

    def _cell(cx, cy, label, value, lsz=6.5, vsz=8.5):
        c.setFillColor(LABEL_C)
        c.setFont("Helvetica", lsz)
        c.drawString(cx, cy + 10, label.upper())
        c.setFillColor(VALUE_C)
        c.setFont("Helvetica-Bold", vsz)
        c.drawString(cx, cy, str(value) if value is not None else "—")

    # ── Page 1 ────────────────────────────────────────────────────────────────
    page = 1
    _header(page)
    _footer(page)
    y = H - HDR_H - 8 * mm

    # Persons
    persons = data.get("persons", [])
    if persons:
        y = _section("REQUERENTES", y, 40)
        n = len(persons)
        card_w = (CW - (n - 1) * 4 * mm) / n
        card_h = 54
        card_y = y - card_h
        for i, p in enumerate(persons):
            cx = LM + i * (card_w + 4 * mm)
            c.setFillColor(LIGHT)
            c.setStrokeColor(BORDER)
            c.roundRect(cx, card_y, card_w, card_h, 3, fill=1, stroke=1)
            # Tab
            c.setFillColor(ACCENT)
            c.roundRect(cx, card_y + card_h - 17, card_w, 17, 3, fill=1, stroke=0)
            c.rect(cx, card_y + card_h - 17, card_w, 8, fill=1, stroke=0)
            c.setFillColor(WHITE)
            label = p.get("name") or ("Fiador" if p.get("is_fiador") else f"Requerente {i+1}")
            name_sz = 8.5 if len(label) <= 30 else 7.5
            c.setFont("Helvetica-Bold", name_sz)
            c.drawString(cx + 4 * mm, card_y + card_h - 12, label[:45])
            # Fields
            fx, fy = cx + 4 * mm, card_y + card_h - 32
            _cell(fx, fy, "Rendimento Mensal Médio", _eur(p.get("income")), 6.5, 8.5)
            _cell(fx, fy - 18, "Responsab. Crédito (€/mês)", _eur(p.get("crc")), 6.5, 8.5)
        y = card_y - 6 * mm

    # Operation box
    y = _section("OPERAÇÃO DE CRÉDITO", y, 55)
    box_h = 44
    c.setFillColor(LIGHT)
    c.setStrokeColor(BORDER)
    c.roundRect(LM, y - box_h, CW, box_h, 3, fill=1, stroke=1)

    finance = data.get("finance_amount")
    prop    = data.get("property_value")
    term    = data.get("term_months", 0)
    ltv_str = None
    if finance and prop:
        try:
            ltv_str = f"{float(finance)/float(prop)*100:.1f}%"
        except Exception:
            pass

    col4 = CW / 4
    r1y = y - box_h + 26
    r2y = y - box_h + 8

    _cell(LM + 5*mm, r1y, "Tipo de Operação",    data.get("operation_type", "—"), 6.5, 8.5)
    _cell(LM + col4 + 5*mm, r1y, "Montante Financiado", _eur(finance), 6.5, 8.5)
    _cell(LM + 2*col4 + 5*mm, r1y, "Valor do Imóvel",  _eur(prop), 6.5, 8.5)
    _cell(LM + 3*col4 + 5*mm, r1y, "Rácio LTV",        ltv_str or "—", 6.5, 8.5)

    total_income = sum(float(p.get("income") or 0) for p in persons if p.get("income"))
    total_crc    = sum(float(p.get("crc") or 0) for p in persons if p.get("crc"))

    _cell(LM + 5*mm, r2y, "Prazo", f"{term} meses", 6.5, 8.5)
    _cell(LM + col4 + 5*mm, r2y, "Rendimento Total", _eur(total_income) if total_income else "—", 6.5, 8.5)
    _cell(LM + 2*col4 + 5*mm, r2y, "Responsab. Actuais", _eur(total_crc) if total_crc else "—", 6.5, 8.5)
    if total_income and total_crc:
        try:
            _cell(LM + 3*col4 + 5*mm, r2y, "Taxa Esforço Actual",
                  f"{total_crc/total_income*100:.1f}%", 6.5, 8.5)
        except Exception:
            pass

    y = y - box_h - 5 * mm

    # Rate box
    y = _section("CONDIÇÕES DA TAXA", y, 52)
    c.setFillColor(LIGHT)
    c.setStrokeColor(BORDER)
    c.roundRect(LM, y - box_h, CW, box_h, 3, fill=1, stroke=1)

    rate_type = data.get("rate_type", "variable")
    col3 = CW / 3
    r1y = y - box_h + 26
    r2y = y - box_h + 8

    pmt_phase1 = schedule[0]["payment"] if schedule else None
    pmt_phase2 = next((r["payment"] for r in schedule if r.get("phase") == 2), None)

    is_transfer = (data.get("operation_type") == "Transferência de Crédito")

    if rate_type == "variable":
        idx = f"EURIBOR {data.get('euribor_period','6M')}"
        tan = (float(data.get("euribor") or 0) + float(data.get("spread") or 0))
        _cell(LM + 5*mm, r1y, "Tipo de Taxa", "Variável", 6.5, 8.5)
        _cell(LM + col3 + 5*mm, r1y, idx, _pct(data.get("euribor")), 6.5, 8.5)
        _cell(LM + 2*col3 + 5*mm, r1y, "Spread", _pct(data.get("spread")), 6.5, 8.5)
        _cell(LM + 5*mm, r2y, "TAN", _pct(tan), 6.5, 8.5)
        _cell(LM + col3 + 5*mm, r2y, "Prestação Mensal", _eur(pmt_phase1), 6.5, 8.5)
        if total_income and pmt_phase1:
            try:
                base = 0 if is_transfer else total_crc
                _cell(LM + 2*col3 + 5*mm, r2y, "Taxa Esforço c/ Prestação",
                      f"{(base + pmt_phase1)/total_income*100:.1f}%", 6.5, 8.5)
            except Exception:
                pass

    elif rate_type == "fixed":
        _cell(LM + 5*mm, r1y, "Tipo de Taxa", "Fixa", 6.5, 8.5)
        _cell(LM + col3 + 5*mm, r1y, "TAN", _pct(data.get("fixed_tan")), 6.5, 8.5)
        _cell(LM + 5*mm, r2y, "Prestação Mensal", _eur(pmt_phase1), 6.5, 8.5)
        if total_income and pmt_phase1:
            try:
                base = 0 if is_transfer else total_crc
                _cell(LM + col3 + 5*mm, r2y, "Taxa Esforço c/ Prestação",
                      f"{(base + pmt_phase1)/total_income*100:.1f}%", 6.5, 8.5)
            except Exception:
                pass

    else:  # mixed
        fm = data.get("fixed_months", 0)
        vm = int(term) - int(fm)
        var_tan = (float(data.get("euribor") or 0) + float(data.get("spread") or 0))
        _cell(LM + 5*mm, r1y, "Tipo de Taxa", "Mista", 6.5, 8.5)
        _cell(LM + col3 + 5*mm, r1y, f"TAN Fixa ({fm}m)", _pct(data.get("fixed_tan")), 6.5, 8.5)
        _cell(LM + 2*col3 + 5*mm, r1y, f"TAN Variável ({vm}m)", _pct(var_tan), 6.5, 8.5)
        _cell(LM + 5*mm, r2y, "Prestação Fase 1", _eur(pmt_phase1), 6.5, 8.5)
        _cell(LM + col3 + 5*mm, r2y, "Prestação Fase 2", _eur(pmt_phase2), 6.5, 8.5)
        if total_income and pmt_phase1:
            try:
                base = 0 if is_transfer else total_crc
                _cell(LM + 2*col3 + 5*mm, r2y, "Taxa Esforço c/ Fase 1",
                      f"{(base + pmt_phase1)/total_income*100:.1f}%", 6.5, 8.5)
            except Exception:
                pass

    y = y - box_h - 5 * mm

    # Key figures
    y = _section("RESUMO FINANCEIRO", y, 48)
    kf_h = 26
    c.setFillColor(BLUE_BG)
    c.setStrokeColor(BORDER)
    c.roundRect(LM, y - kf_h, CW, kf_h, 3, fill=1, stroke=1)
    ky = y - kf_h + 7
    _cell(LM + 5*mm, ky, "Capital Financiado",   _eur(summary.get("total_capital")), 6.5, 8.5)
    _cell(LM + col3 + 5*mm, ky, "Total de Juros",  _eur(summary.get("total_interest")), 6.5, 8.5)
    _cell(LM + 2*col3 + 5*mm, ky, "Custo Total do Crédito", _eur(summary.get("total_paid")), 6.5, 8.5)

    y = y - kf_h - 7 * mm

    # ── Amortization table ────────────────────────────────────────────────────
    y = _section("TABELA DE AMORTIZAÇÃO", y, 65)

    COLS      = [32, 90, 84, 80, 92]
    TW        = sum(COLS)
    TX        = LM + (CW - TW) / 2
    ROW_H     = 10    # monthly row height
    ANN_H     = 14    # annual row height
    TH_H      = 15
    SAFE_Y    = FOOT_Y + 8
    PHASE2ANN = colors.HexColor("#c3d9f8")
    ANN_BG    = colors.HexColor("#dde3ea")

    def _tbl_header(top_y):
        c.setFillColor(NAVY)
        c.rect(TX, top_y - TH_H, TW, TH_H, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont("Helvetica-Bold", 6.5)
        cx = TX
        for lbl, w in zip(["PERIODO", "PRESTACAO", "CAPITAL AMORTIZADO", "JUROS", "CAPITAL EM DIVIDA"], COLS):  # ASCII-only to avoid encoding issues
            c.drawCentredString(cx + w / 2, top_y - TH_H + 4, lbl)
            cx += w
        return top_y - TH_H

    def _tbl_row(disp, row_top, shade):
        is_ann = disp.get("is_annual", False)
        ph     = disp.get("phase", 1)
        rh     = ANN_H if is_ann else ROW_H
        if is_ann:
            bg = PHASE2ANN if ph == 2 else ANN_BG
        elif ph == 2:
            bg = PHASE2
        else:
            bg = LIGHT if shade else WHITE
        c.setFillColor(bg)
        c.rect(TX, row_top - rh, TW, rh, fill=1, stroke=0)
        c.setStrokeColor(colors.HexColor("#dde3ea"))
        c.setLineWidth(0.15)
        c.line(TX, row_top, TX + TW, row_top)
        c.setFillColor(VALUE_C)
        c.setFont("Helvetica-Bold" if is_ann else "Helvetica", 6.5)
        ty = row_top - rh + (rh - 6.5) / 2
        cx = TX
        for val, w in zip([
            disp["label"],
            _eur(disp["payment"]),
            _eur(disp["capital"]),
            _eur(disp["interest"]),
            _eur(disp["balance"]),
        ], COLS):
            c.drawCentredString(cx + w / 2, ty, val)
            cx += w
        return rh

    display_rows = build_display_schedule(schedule, rate_type)
    cur_y     = _tbl_header(y)
    block_top = y

    for i, disp in enumerate(display_rows):
        rh = ANN_H if disp.get("is_annual") else ROW_H
        if cur_y - rh < SAFE_Y:
            c.setStrokeColor(BORDER)
            c.setLineWidth(0.5)
            c.rect(TX, cur_y, TW, block_top - cur_y, stroke=1, fill=0)
            c.showPage()
            page += 1
            _header(page)
            _footer(page)
            new_top   = H - HDR_H - 10 * mm
            cur_y     = _tbl_header(new_top)
            block_top = new_top
        cur_y -= _tbl_row(disp, cur_y, i % 2 == 0)

    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.rect(TX, cur_y, TW, block_top - cur_y, stroke=1, fill=0)

    c.save()
