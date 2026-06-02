#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kova Finance Document Organizer
Categorizes loose documents into standard client subfolders,
merges multi-page image groups into PDFs, and detects true duplicates.

Usage:
    python organize_kova.py                              # Dry-run: show plan
    python organize_kova.py --apply                      # Execute the plan
    python organize_kova.py --apply --skip-vision        # Apply without AI calls
    python organize_kova.py --client "Angel e Ana Isabel"  # Single client only
    python organize_kova.py --standby                    # Also process Standby folder

    -- Claude Code workflow (no API key needed) --
    python organize_kova.py --claude-mode                # Render previews + write claude_review.json
    # Then in Claude Code terminal: "categorize the files in claude_review.json"
    # Claude writes claude_categorizations.json
    python organize_kova.py --apply-categorizations      # Apply Claude's answers

Requirements:
    pip install anthropic img2pdf Pillow pymupdf
    ANTHROPIC_API_KEY must be set in environment (or use --api-key) for --apply vision mode
"""

import os
import re
import sys
import json
import base64
import hashlib
import argparse
import shutil
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

try:
    import img2pdf
    HAS_IMG2PDF = True
except ImportError:
    HAS_IMG2PDF = False

try:
    from PIL import Image as PilImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import anthropic as _anthropic_module
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

# ─── Configuration ─────────────────────────────────────────────────────────

BASE_DIR = Path(r"C:\Users\anton\Desktop\Kova Finance")
VISION_MODEL = "claude-haiku-4-5-20251001"

STANDARD_FOLDERS = [
    "Documentos Pessoais",
    "Rendimentos",
    "Extratos Bancários",
    "IRS",
    "Imóvel",
    "Mapa CRC",
    "RGPD",
    "Proposta Crédito",
]

# These subfolders act as sub-clients and get their own STANDARD_FOLDERS inside
SUBCLIENT_FOLDERS = {"fiador"}

# These subfolders are dissolved: their files are redistributed into standard folders
DISSOLVE_FOLDERS = {"documentos processados", "hpp"}

# Top-level names to skip
SKIP_NAMES = {".claude", "standby", "despesas valencia", "nova pasta"}

# File extensions to skip entirely
SKIP_EXTENSIONS = {".action", ".json", ".xlsx", ".xls", ".docx", ".doc"}

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

# ─── Categorization rules ───────────────────────────────────────────────────
# Reference: Angel e Ana Isabel folder structure (confirmed 2026-06-01)
# Documentos Pessoais: IDs, residence permits, address proofs, IBAN, debt certs
# Rendimentos: payslips, employer declarations, work contracts
# Extratos Bancários: bank statements
# IRS: tax returns, liquidation notes, IES
# Imóvel: CPCV, property certificates, energy certs, plans, escritura
# Mapa CRC: Banco de Portugal credit responsibility maps
# RGPD: data protection consent
# Proposta Crédito: credit proposals, simulations, bank forms

CATEGORY_RULES: Dict[str, List[str]] = {
    "Rendimentos": [
        r"^RecVenc_", r"^DeclPatronal_", r"^ContratoTrabalho_",
        r"^DeclInternaPag", r"^DeclSocioGerente_", r"^DeclSubsidio",
        r"^FaturaAL_", r"^FaturaNegocios_", r"^RecibosVenc",
    ],
    "Extratos Bancários": [
        r"^Extrato_", r"^Extratos_",
    ],
    "IRS": [
        r"^IRS_", r"^NotaLiq_IRS_", r"^NotaLiq_", r"^IES_",
        r"^ComprovativoIRS_", r"^DeclIRS_", r"^Modelo3_", r"^Reembolso_",
    ],
    "Imóvel": [
        r"^CPCV[_.]", r"^CPCV$", r"^Adenda_",
        r"^CertidaoPredial[_.]", r"^CertidaoPredial$",
        r"^Caderneta_", r"^CertificadoEnergetico",
        r"^LicencaUtilizacao", r"^ReciboRenda_",
        r"^Escritura_", r"^Distrate_",
        r"^Plantas_", r"^Desenhos_", r"^Perfis_",
    ],
    "Mapa CRC": [
        r"^MapaCRC_", r"^Mapa_CRC", r"^MapaResponsab", r"^CRC_",
    ],
    "RGPD": [
        r"^RGPD",
    ],
    "Proposta Crédito": [
        r"^Proposta", r"^Simulacao_", r"^FormularioCredHab_",
        r"^Formulario", r"^DeclaracaoMutuarios", r"^CompCapitaisProrios",
        r"^DossierCredito_", r"^AvaliacaoSolv", r"^CertificadoConclusao",
    ],
    "Documentos Pessoais": [
        r"^CC_", r"^NIF_", r"^Passaporte_", r"^CartaoCidadao_",
        r"^TituloResidencia_", r"^AutorizacaoResidencia_", r"^AIMA_",
        r"^DomicilioFiscal_", r"^CompMorada_", r"^CompIBAN_",
        r"^CertNaoDivida", r"^CertPredialNegativa_",
        r"^DeclEmpresaEndereco", r"^DeclFinsBancarios",
        r"^DeclVinculo_", r"^CarreiraContrib", r"^CartaVerde_",
    ],
}

_COMPILED_RULES: Dict[str, List] = {
    cat: [re.compile(p, re.IGNORECASE) for p in patterns]
    for cat, patterns in CATEGORY_RULES.items()
}

# Normalized rules — matched against accent-stripped, lower-cased, space-normalized names.
# Catches natural-language filenames like "Mapa CRC - Tiago", "Declaração Patronal - Dalila".
NORMALIZED_RULES: Dict[str, List[str]] = {
    "Rendimentos": [
        r"^recibo[s]?(\s|$)", r"^recibo de vencimento", r"^recibos de vencimento",
        r"declaracao patronal", r"decl patronal", r"^geprecib",
        r"^contrato de trabalho", r"certificat de salaire", r"salary (certificate|slip|cert)",
        r"^fatura al ", r"informe.*rendimento", r"informederendimento",
        # Payslips named as "87913RecJaneiro", "87913RecDezembro" etc.
        r"rec(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)",
        # Investment/asset docs (common for Brazilian clients)
        r"^acoes ", r"^fundos ", r"^bitcoin", r"^xp informe",
        r"^consolidado.*pf", r"^consolidadopf",
        r"dif itau",  # Brazilian bank financial declaration
    ],
    "Extratos Bancários": [
        r"^extratos?( |$)", r"^extractos?( |$)", r"^ext (sant|bpi|bcp|ctt|cgd|novo|millen)",
        r"^extrato combinado", r"^extracto integrado", r"^extrato global",
        r"itau.*extrato", r"^wise (trimestral|mensal)",
        r"^banco itau", r"^dif itau",
    ],
    "IRS": [
        r"^irs( |\+|$)", r"nota de liquidac", r"nota liquidac",
        r"^dipf", r"declaracao.*irs", r"attestation.*impots",
        r"informederendimentosfinanceiros", r"declar.*ano.*ex",
        r"^comprovativo ir[_\s]",  # Brazilian IR (income tax) proof
    ],
    "Mapa CRC": [
        r"mapa.?crc", r"resp(onsabilidades)?.?(bp|banco)", r"responsabilidades.*banco",
        r"banco.?portugal",  # catches typos like "Respsabilidades BANCO Portugal"
        r"^mapa_crc",
    ],
    "Documentos Pessoais": [
        r"^cc ?-", r"^cc [a-z]", r"^nif( |$)", r"^niss( |$)",
        r"^passaporte( |$)", r"titulo.?residencia", r"titulo.?de.?residencia",
        r"^comprovativo de morada", r"^comprovativo morada", r"^comp.?morada",
        r"^identificacao", r"^numero de utente", r"^iban( |$)",
        r"certidao de casamento", r"^certidao.*casamento",
    ],
    "Imóvel": [
        r"^escritura( de| $)", r"^caderneta", r"^certidao predial",
        r"certidao.*predial", r"unid.*aloj",
        r"^contrato.*arrendamento", r"^recibo.*renda",
        r"^ce\d{10}",  # Certificado Energetico reference numbers
        r"^pct\d",     # Portuguese energy certificate PCT format
        r"certidao permanente",  # Certidão Permanente do Registo Predial
        r"^notarios",  # Notary documents = property deeds
    ],
    "RGPD": [
        r"^rgpd",
    ],
    "Proposta Crédito": [
        r"^proposta", r"^simulacao",
        r"^(1|2|3)(o|a|o|a)\s+proponente",
    ],
}

_COMPILED_NORMALIZED: Dict[str, List] = {
    cat: [re.compile(p, re.IGNORECASE) for p in patterns]
    for cat, patterns in NORMALIZED_RULES.items()
}


def _strip_accents(s: str) -> str:
    import unicodedata
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def normalize_stem(stem: str) -> str:
    """Lower-case, strip accents, collapse separators to single space."""
    s = _strip_accents(stem.lower())
    return re.sub(r"[\s_\-]+", " ", s).strip()

VISION_PROMPT = (
    "This is a scanned page from a Portuguese mortgage application dossier. "
    "Classify it into exactly ONE of these categories:\n"
    "- Documentos Pessoais (CC identity card, passport, residence permit TituloResidencia, "
    "IBAN proof CompIBAN, address proof CompMorada, fiscal domicile, "
    "debt-free certificates CertNaoDivida, career history CarreiraContributiva)\n"
    "- Rendimentos (payslip RecVenc, employer declaration DeclPatronal, "
    "work contract ContratoTrabalho, income declarations)\n"
    "- Extratos Bancários (bank account statement)\n"
    "- IRS (tax return declaration, IRS liquidation note NotaLiquidacao, IES annual report)\n"
    "- Imóvel (CPCV purchase promise, property certificate CertidaoPredial, "
    "land register Caderneta Predial, energy certificate, usage licence, "
    "rent receipt, property plans or drawings)\n"
    "- Mapa CRC (Banco de Portugal credit responsibility map)\n"
    "- RGPD (data protection / GDPR consent form with signature)\n"
    "- Proposta Crédito (credit proposal, bank simulation, mortgage application form, "
    "borrower declaration, solvency assessment)\n\n"
    "Reply with ONLY the category name, nothing else."
)

# ─── Helpers ───────────────────────────────────────────────────────────────

def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def categorize_by_name(stem: str) -> Optional[str]:
    # 1. Try strict prefix rules (original casing)
    for category, patterns in _COMPILED_RULES.items():
        for pat in patterns:
            if pat.match(stem):
                return category
    # 2. Try normalized rules (accent-stripped, space-normalized)
    norm = normalize_stem(stem)
    for category, patterns in _COMPILED_NORMALIZED.items():
        for pat in patterns:
            if pat.search(norm):
                return category
    return None


def get_image_group_key(stem: str) -> str:
    """Strip trailing page suffix (_p1, _p2, _frente, _verso, etc.)."""
    result = re.sub(r"[_-]p\d+$", "", stem, flags=re.IGNORECASE)
    if result != stem:
        return result
    result = re.sub(r"[_-](frente|verso|front|back|page\d+)$", "", stem, flags=re.IGNORECASE)
    return result


def images_to_pdf(image_paths: List[Path], output_path: Path) -> bool:
    """Merge one or more images into a single PDF. Returns True on success."""
    if HAS_IMG2PDF:
        try:
            with open(output_path, "wb") as f:
                f.write(img2pdf.convert([str(p) for p in image_paths]))
            return True
        except Exception as e:
            print(f"    img2pdf error: {e} — trying Pillow fallback")
    if HAS_PIL:
        try:
            imgs = [PilImage.open(p).convert("RGB") for p in image_paths]
            if imgs:
                imgs[0].save(
                    output_path, format="PDF",
                    save_all=True, append_images=imgs[1:]
                )
            return True
        except Exception as e:
            print(f"    Pillow PDF error: {e}")
            return False
    return False


def categorize_by_vision(path: Path, client) -> Optional[str]:
    """Ask Claude Haiku to classify a document image."""
    ext = path.suffix.lower()
    media_type = "image/png" if ext == ".png" else "image/jpeg"
    try:
        with open(path, "rb") as f:
            b64 = base64.standard_b64encode(f.read()).decode()
        resp = client.messages.create(
            model=VISION_MODEL,
            max_tokens=30,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    },
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }],
        )
        result = resp.content[0].text.strip()
        if result in STANDARD_FOLDERS:
            return result
        for f in STANDARD_FOLDERS:
            if f.lower() in result.lower():
                return f
    except Exception as e:
        print(f"    Vision API error for {path.name}: {e}")
    return None


# ─── File discovery ────────────────────────────────────────────────────────

def get_loose_files(folder: Path) -> List[Path]:
    """Files directly inside folder (not in any subfolder)."""
    return [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() not in SKIP_EXTENSIONS
    ]


def get_files_to_organize(client_folder: Path) -> List[Tuple[Path, Path]]:
    """
    Returns (file_path, target_base_folder) for all files that need categorizing:
    - Files at root of client_folder
    - Files inside DISSOLVE_FOLDERS (documentos processados, HPP)
    - Files at root of SUBCLIENT_FOLDERS (fiador)
    - Files inside DISSOLVE_FOLDERS nested within SUBCLIENT_FOLDERS
    """
    results: List[Tuple[Path, Path]] = []

    for f in get_loose_files(client_folder):
        results.append((f, client_folder))

    for item in client_folder.iterdir():
        if not item.is_dir():
            continue
        name_lower = item.name.lower()

        if name_lower in DISSOLVE_FOLDERS:
            for f in item.rglob("*"):
                if f.is_file() and f.suffix.lower() not in SKIP_EXTENSIONS:
                    results.append((f, client_folder))

        elif name_lower in SUBCLIENT_FOLDERS:
            for f in get_loose_files(item):
                results.append((f, item))
            for sub in item.iterdir():
                if sub.is_dir() and sub.name.lower() in DISSOLVE_FOLDERS:
                    for f in sub.rglob("*"):
                        if f.is_file() and f.suffix.lower() not in SKIP_EXTENSIONS:
                            results.append((f, item))

    return results


def find_image_merge_groups(
    files: List[Tuple[Path, Path]]
) -> Dict[Tuple[Path, str], List[Path]]:
    """
    Find groups of images that represent pages of the same document
    (detected by _p1/_p2/... or _frente/_verso suffixes).
    Returns {(base_folder, group_key): [sorted image paths]}.
    """
    groups: Dict[Tuple[Path, str], List[Path]] = defaultdict(list)
    for file_path, target_base in files:
        if file_path.suffix.lower() in IMAGE_EXTS:
            key = get_image_group_key(file_path.stem)
            if key != file_path.stem:
                groups[(target_base, key)].append(file_path)
    return {k: sorted(v, key=lambda p: p.stem) for k, v in groups.items() if len(v) >= 2}


def find_inplace_image_merges(client_folder: Path) -> List[Dict]:
    """
    Find multi-page image groups already inside standard subfolders.
    These are merged in-place (no move needed, just combine to PDF).
    """
    merges = []
    scopes: List[Path] = [client_folder]

    # Also check fiador subfolders
    for item in client_folder.iterdir():
        if item.is_dir() and item.name.lower() in SUBCLIENT_FOLDERS:
            scopes.append(item)

    for base in scopes:
        for std in STANDARD_FOLDERS:
            std_folder = base / std
            if not std_folder.exists():
                continue
            images_here = [
                f for f in std_folder.iterdir()
                if f.is_file() and f.suffix.lower() in IMAGE_EXTS
            ]
            groups: Dict[str, List[Path]] = defaultdict(list)
            for img in images_here:
                key = get_image_group_key(img.stem)
                if key != img.stem:
                    groups[key].append(img)
            for key, imgs in groups.items():
                if len(imgs) >= 2:
                    output = std_folder / (key + ".pdf")
                    if not output.exists():
                        merges.append({
                            "images": [str(p) for p in sorted(imgs, key=lambda p: p.stem)],
                            "output": str(output),
                            "category": std,
                        })
    return merges


def find_duplicates(client_folder: Path) -> List[Tuple[Path, Path]]:
    """
    Find files with identical MD5 within a client folder.
    Returns (keep, delete) pairs — prefers files already in standard subfolders.
    """
    hash_map: Dict[str, List[Path]] = defaultdict(list)
    for f in client_folder.rglob("*"):
        if f.is_file() and f.suffix.lower() not in SKIP_EXTENSIONS:
            try:
                hash_map[file_md5(f)].append(f)
            except (PermissionError, OSError):
                pass

    dupes: List[Tuple[Path, Path]] = []
    for paths in hash_map.values():
        if len(paths) < 2:
            continue
        paths_sorted = sorted(paths, key=lambda p: (
            0 if p.parent.name in STANDARD_FOLDERS else 1,
            str(p)
        ))
        for dup in paths_sorted[1:]:
            dupes.append((paths_sorted[0], dup))
    return dupes


# ─── Plan generation ───────────────────────────────────────────────────────

def scan_client(
    client_folder: Path,
    use_vision: bool,
    ai_client,
) -> Dict:
    plan: Dict = {
        "client": client_folder.name,
        "new_folders": [],
        "moves": [],
        "merges": [],
        "duplicates": [],
        "uncategorized": [],
    }

    # Collect standard folders to create
    for std in STANDARD_FOLDERS:
        target = client_folder / std
        if not target.exists():
            plan["new_folders"].append(str(target))

    for item in client_folder.iterdir():
        if item.is_dir() and item.name.lower() in SUBCLIENT_FOLDERS:
            for std in STANDARD_FOLDERS:
                target = item / std
                if not target.exists():
                    plan["new_folders"].append(str(target))

    # Get all files that need organizing
    to_organize = get_files_to_organize(client_folder)

    # Detect image merge groups first
    merge_groups = find_image_merge_groups(to_organize)
    merged_files: set = set()

    for (base_folder, group_key), images in merge_groups.items():
        category = categorize_by_name(group_key)
        if category is None and use_vision and ai_client:
            print(f"    [vision] {images[0].name}  (group: {group_key})")
            category = categorize_by_vision(images[0], ai_client)
        if category is None:
            category = "Documentos Pessoais"  # safe fallback for image groups

        output = base_folder / category / (group_key + ".pdf")
        plan["merges"].append({
            "images": [str(p) for p in images],
            "output": str(output),
            "category": category,
        })
        merged_files.update(images)

    # Process remaining files
    for file_path, base_folder in to_organize:
        if file_path in merged_files:
            continue

        category = categorize_by_name(file_path.stem)

        if category is None and file_path.suffix.lower() in IMAGE_EXTS:
            if use_vision and ai_client:
                print(f"    [vision] {file_path.name}")
                category = categorize_by_vision(file_path, ai_client)

        if category is None:
            plan["uncategorized"].append({
                "file": str(file_path),
                "target_base": str(base_folder),
            })
            continue

        target = base_folder / category / file_path.name
        if target.resolve() != file_path.resolve():
            plan["moves"].append({
                "from": str(file_path),
                "to": str(target),
            })

    # Also merge image groups that are already inside standard subfolders
    for mg in find_inplace_image_merges(client_folder):
        plan["merges"].append(mg)

    # Find exact duplicates
    for keep, delete in find_duplicates(client_folder):
        plan["duplicates"].append({
            "keep": str(keep),
            "delete": str(delete),
        })

    return plan


# ─── Report ────────────────────────────────────────────────────────────────

def print_plan(plans: List[Dict]) -> None:
    total_moves   = sum(len(p["moves"])         for p in plans)
    total_merges  = sum(len(p["merges"])        for p in plans)
    total_dupes   = sum(len(p["duplicates"])    for p in plans)
    total_uncat   = sum(len(p["uncategorized"]) for p in plans)
    total_folders = sum(len(p["new_folders"])   for p in plans)

    sep = "=" * 70
    print(f"\n{sep}")
    print("  KOVA FINANCE ORGANIZER — DRY RUN REPORT")
    print(sep)
    print(f"  Clients scanned  : {len(plans)}")
    print(f"  New folders      : {total_folders}")
    print(f"  Files to move    : {total_moves}")
    print(f"  Image -> PDF merge: {total_merges} group(s)")
    print(f"  True duplicates  : {total_dupes}")
    print(f"  Uncategorized    : {total_uncat}")
    print(sep)

    for p in plans:
        has_work = any([
            p["new_folders"], p["moves"], p["merges"],
            p["duplicates"], p["uncategorized"]
        ])
        if not has_work:
            continue

        print(f"\n>>  {p['client']}")

        if p["new_folders"]:
            names = [Path(f).name for f in p["new_folders"]]
            print(f"   + Create {len(names)} folder(s): {', '.join(names)}")

        if p["moves"]:
            for m in p["moves"]:
                src = Path(m["from"])
                dst = Path(m["to"])
                try:
                    rel_src = src.relative_to(BASE_DIR)
                    rel_dst = dst.relative_to(BASE_DIR)
                except ValueError:
                    rel_src, rel_dst = src, dst
                print(f"   MOVE  {rel_src}")
                print(f"     ->  {rel_dst}")

        if p["merges"]:
            for mg in p["merges"]:
                names = [Path(i).name for i in mg["images"]]
                try:
                    out = Path(mg["output"]).relative_to(BASE_DIR)
                except ValueError:
                    out = Path(mg["output"])
                print(f"   MERGE {' + '.join(names)}")
                print(f"     ->  {out}")

        if p["duplicates"]:
            for d in p["duplicates"]:
                try:
                    keep   = Path(d["keep"]).relative_to(BASE_DIR)
                    delete = Path(d["delete"]).relative_to(BASE_DIR)
                except ValueError:
                    keep, delete = Path(d["keep"]), Path(d["delete"])
                print(f"   DUPE  keep   {keep}")
                print(f"         delete {delete}")

        if p["uncategorized"]:
            print(f"   ? {len(p['uncategorized'])} file(s) need manual categorization:")
            for u in p["uncategorized"]:
                f = u["file"] if isinstance(u, dict) else u
                print(f"       {Path(f).name}")

    print(f"\n{sep}")
    print("  Run with --apply to execute.  Plan saved to organize_plan.json")
    print(f"{sep}\n")


# ─── Apply ─────────────────────────────────────────────────────────────────

def apply_plan(plans: List[Dict]) -> None:
    for p in plans:
        print(f"\n>> {p['client']}")
        actions = 0

        for folder_str in p["new_folders"]:
            fp = Path(folder_str)
            if not fp.exists():
                fp.mkdir(parents=True, exist_ok=True)
                print(f"  mkdir  {fp.relative_to(fp.parent.parent)}")
                actions += 1

        for m in p["moves"]:
            src = Path(m["from"])
            dst = Path(m["to"])
            if not src.exists():
                print(f"  SKIP (missing)  {src.name}")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                if file_md5(src) == file_md5(dst):
                    src.unlink()
                    print(f"  CLEAN (dupe already moved)  {src.name}")
                    actions += 1
                    continue
                base, ext = dst.stem, dst.suffix
                i = 1
                while dst.exists():
                    dst = dst.parent / f"{base}_conflict{i}{ext}"
                    i += 1
            shutil.move(str(src), str(dst))
            try:
                rel = dst.relative_to(BASE_DIR)
            except ValueError:
                rel = dst
            print(f"  move  {src.name}  ->  {rel}")
            actions += 1

        for mg in p["merges"]:
            imgs = [Path(i) for i in mg["images"]]
            out  = Path(mg["output"])
            existing = [i for i in imgs if i.exists()]
            if len(existing) < 2:
                print(f"  SKIP merge (files missing)  {out.name}")
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists():
                print(f"  SKIP merge (output exists)  {out.name}")
                continue
            if images_to_pdf(existing, out):
                for img in existing:
                    img.unlink()
                try:
                    rel = out.relative_to(BASE_DIR)
                except ValueError:
                    rel = out
                print(f"  merge {len(existing)} imgs  ->  {rel}")
                actions += 1
            else:
                print(f"  FAIL merge  {out.name}  (originals kept)")

        for d in p["duplicates"]:
            delete_path = Path(d["delete"])
            if delete_path.exists():
                delete_path.unlink()
                print(f"  del dupe  {delete_path.name}")
                actions += 1

        if actions == 0:
            print("  (nothing to do)")

    print("\nDone.\n")


# ─── Claude-mode ───────────────────────────────────────────────────────────

def generate_claude_review(plans: List[Dict]) -> Path:
    """
    Renders a first-page preview PNG for every uncategorized file and writes
    claude_review.json.  The user then asks Claude Code to read that file and
    produce claude_categorizations.json, which --apply-categorizations consumes.
    """
    try:
        import fitz
        has_fitz = True
    except ImportError:
        has_fitz = False

    review_dir = BASE_DIR / "_claude_review"
    review_dir.mkdir(exist_ok=True)
    for old in review_dir.glob("*"):
        old.unlink()

    entries = []
    for p in plans:
        for u in p["uncategorized"]:
            file_path  = Path(u["file"])
            target_base = u["target_base"]
            if not file_path.exists():
                continue

            # Build a safe preview filename
            safe = re.sub(r'[<>:"/\\|?*\[\]]', "_", file_path.name)
            preview_path = review_dir / (safe + ".png")

            ext = file_path.suffix.lower()
            rendered = False

            if ext in IMAGE_EXTS:
                if HAS_PIL:
                    try:
                        img = PilImage.open(file_path).convert("RGB")
                        img.thumbnail((1200, 1600))
                        img.save(preview_path, "PNG")
                        rendered = True
                    except Exception:
                        pass
                if not rendered:
                    shutil.copy2(file_path, preview_path.with_suffix(file_path.suffix))
                    preview_path = preview_path.with_suffix(file_path.suffix)
                    rendered = True

            elif ext == ".pdf" and has_fitz:
                try:
                    doc = fitz.open(str(file_path))
                    pix = doc[0].get_pixmap(dpi=120)
                    pix.save(str(preview_path))
                    doc.close()
                    rendered = True
                except Exception:
                    pass

            entries.append({
                "client":      p["client"],
                "file":        str(file_path),
                "target_base": target_base,
                "preview":     str(preview_path) if rendered else None,
                "category":    None,
            })

    review_json = BASE_DIR / "claude_review.json"
    with open(review_json, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    print(f"\n{len(entries)} file(s) need review.")
    print(f"Previews  -> {review_dir}")
    print(f"Review JSON -> {review_json}")
    print("\nNext steps:")
    print('  1. In this terminal ask Claude Code:')
    print('       "categorize the files in claude_review.json"')
    print("  2. Claude will read each preview and write claude_categorizations.json")
    print("  3. Run:  python organize_kova.py --apply-categorizations")
    return review_json


def apply_categorizations(path: Path = None) -> None:
    """
    Reads claude_categorizations.json produced by Claude Code and moves each
    file to its categorized subfolder.
    """
    if path is None:
        path = BASE_DIR / "claude_categorizations.json"

    if not path.exists():
        print(f"Not found: {path}")
        print("Run --claude-mode first, have Claude categorize, then retry.")
        return

    with open(path, encoding="utf-8") as f:
        entries = json.load(f)

    moved = skipped = 0
    for entry in entries:
        file_path   = Path(entry["file"])
        category    = entry.get("category", "").strip()
        target_base = Path(entry.get("target_base", file_path.parent))

        if not category:
            print(f"  SKIP (no category set)  {file_path.name}")
            skipped += 1
            continue
        if category not in STANDARD_FOLDERS:
            print(f"  SKIP (unknown category '{category}')  {file_path.name}")
            skipped += 1
            continue
        if not file_path.exists():
            print(f"  SKIP (already moved?)  {file_path.name}")
            skipped += 1
            continue

        dst = target_base / category / file_path.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if file_md5(file_path) == file_md5(dst):
                file_path.unlink()
                print(f"  CLEAN (identical exists)  {file_path.name}")
            else:
                stem, ext = dst.stem, dst.suffix
                i = 1
                while dst.exists():
                    dst = dst.parent / f"{stem}_conflict{i}{ext}"
                    i += 1
                shutil.move(str(file_path), str(dst))
                print(f"  move  {file_path.name}  ->  {dst.parent.name}/  [conflict rename]")
            moved += 1
            continue

        shutil.move(str(file_path), str(dst))
        try:
            rel = dst.relative_to(BASE_DIR)
        except ValueError:
            rel = dst
        print(f"  move  {file_path.name}  ->  {rel.parent.name}/")
        moved += 1

    print(f"\nDone: {moved} moved, {skipped} skipped.\n")


# ─── Main ──────────────────────────────────────────────────────────────────

def _api_key_from_db() -> Optional[str]:
    """Read the Anthropic API key stored via the webapp Settings page."""
    db_path = BASE_DIR / "webapp" / "kova.db"
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='anthropic_api_key'"
            ).fetchone()
            return (row[0] or "").strip() or None
    except Exception:
        return None


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Kova Finance Document Organizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--apply",                   action="store_true", help="Execute changes (default: dry-run)")
    parser.add_argument("--client",                  metavar="NAME",      help="Process only this client folder")
    parser.add_argument("--skip-vision",             action="store_true", help="Skip AI vision for unrecognised images")
    parser.add_argument("--standby",                 action="store_true", help="Also process the Standby folder")
    parser.add_argument("--api-key",                 metavar="KEY",       help="Anthropic API key (overrides ANTHROPIC_API_KEY env var)")
    parser.add_argument("--claude-mode",             action="store_true", help="Render previews of uncategorized files + write claude_review.json")
    parser.add_argument("--apply-categorizations",   metavar="JSON",      nargs="?", const=str(BASE_DIR / "claude_categorizations.json"),
                        help="Apply categorizations from claude_categorizations.json (or a custom path)")
    args = parser.parse_args()

    # --apply-categorizations is a standalone mode — no scanning needed
    if args.apply_categorizations:
        apply_categorizations(Path(args.apply_categorizations))
        return

    # Set up AI client
    ai_client = None
    use_vision = not args.skip_vision
    if use_vision:
        if not HAS_ANTHROPIC:
            print("Warning: 'anthropic' not installed — vision disabled. Run: pip install anthropic")
            use_vision = False
        else:
            api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY") or _api_key_from_db()
            if not api_key:
                print("Warning: ANTHROPIC_API_KEY not set and not found in webapp DB — vision disabled. Use --skip-vision to suppress.")
                use_vision = False
            else:
                ai_client = _anthropic_module.Anthropic(api_key=api_key)

    # Collect client folders
    skip_lower = SKIP_NAMES.copy()
    if not args.standby:
        skip_lower.add("standby")

    client_folders: List[Path] = []
    for item in sorted(BASE_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_dir():
            continue
        if item.name.lower() in skip_lower:
            continue
        if args.client and item.name.lower() != args.client.lower():
            continue
        client_folders.append(item)

    if not client_folders:
        sys.exit(f"No matching client folders found under {BASE_DIR}")

    mode = "APPLYING" if args.apply else "DRY-RUN"
    print(f"\n[{mode}] Scanning {len(client_folders)} client folder(s)...")
    if use_vision:
        print(f"Vision: enabled (model: {VISION_MODEL})")
    else:
        print("Vision: disabled")

    plans: List[Dict] = []
    for folder in client_folders:
        print(f"  {folder.name}")
        plans.append(scan_client(folder, use_vision=use_vision, ai_client=ai_client))

    # Save JSON plan
    plan_path = BASE_DIR / "organize_plan.json"
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plans, f, ensure_ascii=False, indent=2)
    print(f"\nPlan saved -> {plan_path}")

    if args.claude_mode:
        generate_claude_review(plans)
    elif args.apply:
        apply_plan(plans)
    else:
        print_plan(plans)


if __name__ == "__main__":
    main()
