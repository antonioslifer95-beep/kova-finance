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

    from services.pdf_package import get_persons, extract_person_data
    persons = get_persons(client_id)
    for p in persons:
        p["extracted"] = extract_person_data(client_id, p["key"])
    return tmpl.TemplateResponse("partials/sim_persons.html", {
        "request": request, "persons": persons,
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

    slug_map = {
        "Aquisição": "Aquisicao",
        "Refinanciamento": "Refinanciamento",
        "Transferência de Crédito": "Transferencia",
    }
    op_slug  = slug_map.get(operation_type, "Simulacao")
    today    = date.today().strftime("%Y-%m")
    filename = f"Simulacao_{op_slug}_{today}.pdf"
    out_path = folder / filename
    v = 2
    while out_path.exists():
        out_path = folder / f"Simulacao_{op_slug}_{today}_v{v}.pdf"
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
