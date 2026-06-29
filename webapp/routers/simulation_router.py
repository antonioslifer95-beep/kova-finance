"""Simulation router: mortgage / refinance / transfer simulator."""
import re
from datetime import date
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from auth import current_user
from database import get_db
from config import TEMPLATE_DIR, BASE_DIR

router = APIRouter()
tmpl   = Jinja2Templates(directory=str(TEMPLATE_DIR))

TEST_SIM_DIR = BASE_DIR / "Simulacoes Teste"


@router.get("/simulation", response_class=HTMLResponse)
def simulation_page(request: Request):
    user = current_user(request)
    with get_db() as db:
        clients = [dict(r) for r in db.execute(
            "SELECT id, folder_name FROM clients ORDER BY folder_name"
        ).fetchall()]
    return tmpl.TemplateResponse("simulation.html", {
        "request": request, "user": user, "clients": clients,
    })


@router.get("/simulation/client-data", response_class=HTMLResponse)
def client_data_partial(request: Request, client_id: int):
    """HTMX: return editable person cards for the chosen client."""
    current_user(request)

    if client_id == 0:
        return tmpl.TemplateResponse("partials/sim_persons_manual.html", {
            "request": request,
        })

    from services.pdf_package import get_persons, extract_person_data, extract_transfer_data
    persons = get_persons(client_id)
    for p in persons:
        p["extracted"] = extract_person_data(client_id, p["key"])

    # Max term: oldest non-fiador holder must be ≤ 75 at mortgage end
    holder_ages = []
    primary_key = None
    for p in persons:
        if p.get("is_fiador"):
            continue
        if primary_key is None:
            primary_key = p["key"]
        age = (p.get("extracted") or {}).get("age")
        if age and isinstance(age, (int, float)):
            holder_ages.append(int(age))
    max_term = None
    if holder_ages:
        mt = (75 - max(holder_ages)) * 12
        if mt > 0:
            max_term = min(mt, 480)

    # Transfer data: housing mortgage balance + months left from Mapa CRC
    transfer_data = extract_transfer_data(client_id, primary_key or "")

    return tmpl.TemplateResponse("partials/sim_persons.html", {
        "request": request, "persons": persons,
        "max_term": max_term, "transfer_data": transfer_data,
    })


@router.post("/simulation/generate", response_class=HTMLResponse)
async def generate_simulation(request: Request):
    user = current_user(request)
    form = await request.form()

    client_id = int(form.get("client_id") or 0)

    if client_id == 0:
        client = {"id": 0, "folder_name": "Potencial Cliente"}
        is_test = True
    else:
        with get_db() as db:
            client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            return HTMLResponse("Cliente não encontrado.", status_code=404)
        client = dict(client)
        is_test = False

    # Persons
    n = int(form.get("person_count") or 0)
    persons = []
    for i in range(n):
        inc = form.get(f"p{i}_income") or None
        crc = form.get(f"p{i}_crc") or None
        persons.append({
            "name":   form.get(f"p{i}_name", ""),
            "income": float(inc) if inc else None,
            "crc":    float(crc) if crc else None,
        })

    # Operation
    operation_type = form.get("operation_type", "Aquisição")
    finance_amount = float(form.get("finance_amount") or 0)
    property_value = float(form.get("property_value") or 0) or None
    term_months    = int(form.get("term_months") or 0)
    rate_type      = form.get("rate_type", "variable")
    euribor_period = form.get("euribor_period", "6M")
    euribor        = float(form.get("euribor") or 0)
    spread         = float(form.get("spread") or 0)
    fixed_tan      = float(form.get("fixed_tan") or 0)
    fixed_months   = int(form.get("fixed_months") or 0)

    # Build schedule
    from services.simulation import (
        build_schedule, build_mixed_schedule, schedule_summary,
        generate_simulation_pdf,
    )
    if rate_type == "fixed":
        sched = build_schedule(finance_amount, fixed_tan, term_months)
    elif rate_type == "variable":
        sched = build_schedule(finance_amount, euribor + spread, term_months)
    else:  # mixed
        sched = build_mixed_schedule(finance_amount, fixed_tan, fixed_months,
                                     euribor + spread, term_months)

    summ = schedule_summary(sched, principal=finance_amount)

    sim_data = {
        "client_name":    client["folder_name"],
        "operation_type": operation_type,
        "persons":        persons,
        "finance_amount": finance_amount,
        "property_value": property_value,
        "term_months":    term_months,
        "rate_type":      rate_type,
        "euribor_period": euribor_period,
        "euribor":        euribor if rate_type != "fixed" else None,
        "spread":         spread if rate_type != "fixed" else None,
        "fixed_tan":      fixed_tan if rate_type != "variable" else None,
        "fixed_months":   fixed_months if rate_type == "mixed" else None,
    }

    # Determine output folder
    if is_test:
        folder = TEST_SIM_DIR
    else:
        folder = Path(client["folder_path"]) / "Proposta Crédito"
    folder.mkdir(exist_ok=True)

    today = date.today().strftime("%Y-%m")

    if is_test:
        rate_slug_map = {"variable": "Variavel", "fixed": "Fixa", "mixed": "Mista"}
        rate_slug = rate_slug_map.get(rate_type, rate_type)
        raw_name  = (persons[0]["name"].strip() if persons and persons[0]["name"] else "Potencial")
        # strip accents and keep only word chars
        import unicodedata
        name_slug = unicodedata.normalize("NFD", raw_name)
        name_slug = "".join(c for c in name_slug if unicodedata.category(c) != "Mn")
        name_slug = re.sub(r"[^\w]", "_", name_slug).strip("_")
        name_slug = re.sub(r"_+", "_", name_slug)[:30]
        base_name = f"Simulacao_{name_slug}_{rate_slug}"
    else:
        slug_map = {
            "Aquisição": "Aquisicao",
            "Refinanciamento": "Refinanciamento",
            "Transferência de Crédito": "Transferencia",
        }
        base_name = f"Simulacao_{slug_map.get(operation_type, 'Simulacao')}"

    filename = f"{base_name}_{today}.pdf"
    out_path = folder / filename
    v = 2
    while out_path.exists():
        out_path = folder / f"{base_name}_{today}_v{v}.pdf"
        v += 1

    generate_simulation_pdf(sim_data, sched, summ, str(out_path))

    return tmpl.TemplateResponse("partials/sim_result.html", {
        "request":  request,
        "user":     user,
        "client":   client,
        "filename": out_path.name,
        "summ":     summ,
        "sim_data": sim_data,
        "is_test":  is_test,
    })


@router.get("/simulation/download")
def download_simulation(request: Request, client_id: int, filename: str):
    current_user(request)
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400)

    if client_id == 0:
        path = TEST_SIM_DIR / filename
    else:
        with get_db() as db:
            client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            raise HTTPException(404)
        path = Path(client["folder_path"]) / "Proposta Crédito" / filename

    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
