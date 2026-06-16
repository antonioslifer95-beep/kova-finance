"""Package generation: merged dossier PDFs + summary sheet."""
import re
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from auth import current_user
from database import get_db
from config import TEMPLATE_DIR

router = APIRouter()
tmpl   = Jinja2Templates(directory=str(TEMPLATE_DIR))


@router.get("/clients/{client_id}/package", response_class=HTMLResponse)
def package_form(request: Request, client_id: int):
    user = current_user(request)
    with get_db() as db:
        client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")

    from services.pdf_package import get_persons, extract_person_data
    persons = get_persons(client_id)

    # Pre-fill AI-extracted fields for each person
    for p in persons:
        extracted = extract_person_data(client_id, p["key"])
        p["extracted"] = extracted  # keys: name, age, nif, monthly_income, crc_total

    return tmpl.TemplateResponse("package.html", {
        "request": request, "user": user,
        "client": dict(client),
        "persons": persons,
    })


@router.post("/clients/{client_id}/package/generate", response_class=HTMLResponse)
async def generate_package(request: Request, client_id: int):
    user = current_user(request)
    with get_db() as db:
        client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")

    form = await request.form()
    n = int(form.get("person_count", 0))

    persons_data = []
    for i in range(n):
        persons_data.append({
            "key":           form.get(f"p{i}_key", ""),
            "name":          form.get(f"p{i}_name", ""),
            "age":           form.get(f"p{i}_age") or None,
            "nif":           form.get(f"p{i}_nif", ""),
            "phone":         form.get(f"p{i}_phone", ""),
            "email":         form.get(f"p{i}_email", ""),
            "monthly_income": form.get(f"p{i}_income") or None,
            "crc_total":     form.get(f"p{i}_crc") or None,
            "is_fiador":     form.get(f"p{i}_fiador") == "1",
        })

    operation = {
        "mortgage_amount":  form.get("mortgage_amount") or None,
        "property_value":   form.get("property_value") or None,
    }

    from services.pdf_package import get_persons, merge_person_pdf, generate_summary_pdf
    persons_with_docs = get_persons(client_id)

    folder_path = Path(client["folder_path"])
    gerado_dir  = folder_path / "Dossier e Folha de Rosto"
    gerado_dir.mkdir(exist_ok=True)

    generated = []

    # One merged PDF per person
    for p in persons_data:
        docs = next((pw["docs"] for pw in persons_with_docs if pw["key"] == p["key"]), [])
        if not docs:
            continue
        label = p["name"] or p["key"] or "Requerente"
        safe  = re.sub(r'[^\w\-]', '_', label)
        role  = "Fiador" if p["is_fiador"] else "Dossier"
        out   = str(gerado_dir / f"{role}_{safe}.pdf")
        pages = merge_person_pdf(docs, out)
        generated.append({
            "label": f"{'Fiador' if p['is_fiador'] else 'Dossier'}  —  {label}",
            "filename": Path(out).name,
            "pages": pages,
            "client_id": client_id,
        })

    # Summary PDF
    safe_client  = re.sub(r'[^\w\-]', '_', client["folder_name"])
    summary_path = str(gerado_dir / f"Resumo_{safe_client}.pdf")
    generate_summary_pdf(persons_data, operation, summary_path, client["folder_name"])
    generated.append({
        "label":    "Resumo da Proposta",
        "filename": Path(summary_path).name,
        "pages":    None,
        "client_id": client_id,
    })

    return tmpl.TemplateResponse("package_result.html", {
        "request":   request,
        "user":      user,
        "client":    dict(client),
        "generated": generated,
    })


@router.get("/clients/{client_id}/package/download")
def download_generated(request: Request, client_id: int, filename: str):
    current_user(request)
    # Validate: no path traversal, must be inside _gerado/
    if "/" in filename or "\\" in filename or ".." in filename:
        from fastapi import HTTPException
        raise HTTPException(400, "Invalid filename")
    with get_db() as db:
        client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        from fastapi import HTTPException
        raise HTTPException(404)
    path = Path(client["folder_path"]) / "_gerado" / filename
    if not path.exists():
        from fastapi import HTTPException
        raise HTTPException(404, "File not found")
    return FileResponse(str(path), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
