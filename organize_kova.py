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

# These subfolders are dissolved: their files are redistributed into standard folders
DISSOLVE_FOLDERS = {"documentos processados", "hpp"}

# Words that mark a subfolder as temporary/working (to be dissolved)
_DISSOLVE_WORDS = {"processados", "processadas", "verificar", "hpp"}


def _is_dissolve_folder(name: str) -> bool:
    """True for temp/working folders whose contents should be redistributed."""
    n = name.lower()
    if n in DISSOLVE_FOLDERS:
        return True
    if n.startswith("_"):
        return True
    return any(w in n for w in _DISSOLVE_WORDS)


def _subclient_has_content(folder: Path) -> bool:
    """
    True when a subfolder is worth treating as a sub-client:
    - has loose files at its root, OR
    - has files inside non-standard sub-subfolders (dissolve fodder), OR
    - already has standard subfolders (even empty) — signals prior sub-client setup.
    """
    standard_lower = {s.lower() for s in STANDARD_FOLDERS}
    for item in folder.iterdir():
        if item.is_file() and item.suffix.lower() not in SKIP_EXTENSIONS:
            return True  # loose file
        if item.is_dir():
            if item.name.lower() in standard_lower:
                return True  # previously set up as sub-client
            else:
                if any(f.is_file() for f in item.rglob("*") if f.suffix.lower() not in SKIP_EXTENSIONS):
                    return True  # non-standard subfolder with files
    return False


def _parse_person_names(folder_name: str) -> List[str]:
    """
    Extract individual person names from a two-person client folder name.
    'Angel e Ana Isabel' -> ['Angel', 'Ana Isabel']
    'Ipshita and Ribal'  -> ['Ipshita', 'Ribal']
    Returns [] for single-person clients.
    """
    for sep in [" e ", " and ", " & "]:
        lower = folder_name.lower()
        idx = lower.find(sep.lower())
        if idx != -1:
            a = folder_name[:idx].strip()
            b = folder_name[idx + len(sep):].strip()
            return [n for n in [a, b] if n]
    return []


def _name_patterns(sc_name: str) -> List:
    """
    Return regex patterns for matching a person name in a normalised filename stem.
    Handles:
    - Full name:      'paulo macario'  -> \bpaulo macario\b
    - Compact:        'paulo macario'  -> \bpaulomacario\b  (PauloMacario)
    - First name:     'paulo macario'  -> \bpaulo\b  (CC_Paulo.pdf)
    - Fuzzy vowel:    'elizbieta'      -> \belzbieta\b  (single missing vowel)
    """
    seen: set = set()

    def _add(word: str):
        w = word.strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            patterns.append(re.compile(r'\b' + re.escape(w) + r'\b', re.IGNORECASE))

    patterns: List = []
    _add(sc_name)

    if ' ' in sc_name:
        compact = sc_name.replace(' ', '')
        _add(compact)
        first = sc_name.split()[0]
        if len(first) >= 4:
            _add(first)

    # Fuzzy: for single-word names >=7 chars, try each vowel removed.
    # Catches e.g. "Elizbieta" -> "Elzbieta" (folder typo vs filenames).
    base = sc_name if ' ' not in sc_name else sc_name.split()[0]
    if len(base) >= 7:
        _VOWELS = set('aeiouáéíóúàèìòùãõâêîôûäëïöü')
        for i, ch in enumerate(base.lower()):
            if ch in _VOWELS:
                variant = base[:i] + base[i + 1:]
                if len(variant) >= 4:
                    _add(variant)

    return patterns


def _get_subclient_folders(client_folder: Path) -> List[Path]:
    """Non-standard, non-dissolve subfolders that represent co-applicants or guarantors."""
    standard_lower = {s.lower() for s in STANDARD_FOLDERS}
    return [
        item for item in client_folder.iterdir()
        if item.is_dir()
        and item.name.lower() not in standard_lower
        and not _is_dissolve_folder(item.name)
        and _subclient_has_content(item)
    ]

# Top-level names to skip
SKIP_NAMES = {".claude", ".git", "standby", "despesas valencia", "nova pasta", "webapp"}

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

RENAME_PROMPT = """\
This is a page from a Portuguese mortgage dossier.
Category folder: {category}
{person_line}
Current filename: {filename}

Generate a standardized filename stem (NO extension) following these exact rules:

Rendimentos:
  payslip/recibo de vencimento  →  RecVenc_YYYY-MM_Person
  employer declaration          →  DeclPatronal_Person
  work contract                 →  ContratoTrabalho_Person
  income declaration            →  DeclRendimentos_YYYY_Person
  social-manager declaration    →  DeclSocioGerente_Person
  salary declaration            →  DeclInternaPagSalarial_Person

Extratos Bancários:
  bank statement                →  Extrato_YYYY-MM_BankName_Person
    (joint/no clear person      →  Extrato_YYYY-MM_BankName)
  short bank names: BCP BPI CGD Santander NovoBanco Revolut Wise ActivoBank Montepio Itau

IRS:
  tax declaration               →  IRS_YYYY_Person
  liquidation note              →  NotaLiq_IRS_YYYY_Person
  IES report                    →  IES_YYYY_Person

Documentos Pessoais:
  ID card (cartão cidadão/CC)   →  CC_Person
  passport                      →  Passaporte_Person
  residence permit              →  TituloResidencia_Person
  address proof                 →  CompMorada_Person
  IBAN proof                    →  CompIBAN_BankName_Person
  tax domicile                  →  DomicilioFiscal_Person
  debt-free certificate         →  CertNaoDivida_Person
  career history                →  CarreiraContributiva_Person
  fiscal certificate            →  CertNaoDividaFinancas_Person
  SS certificate                →  CertNaoDividaSS_Person

Mapa CRC:
  CRC map                       →  MapaCRC_YYYY-MM_Person

RGPD:
  consent form                  →  RGPD_Person

Imóvel:
  property certificate          →  CertidaoPredial
  land register (caderneta)     →  CadernaPredial
  energy certificate            →  CertificadoEnergetico
  usage licence                 →  LicencaUtilizacao
  property plans                →  Plantas
  CPCV                          →  CPCV
  purchase deed (escritura)     →  Escritura
  unit sheet (unidade aloj.)    →  UnidAloj
  addendum                      →  Adenda

Proposta Crédito:
  credit proposal               →  Proposta_BankName_YYYY-MM
  simulation                    →  Simulacao_BankName_YYYY-MM
  bank form                     →  FormularioBanco_BankName
  borrower declaration          →  DeclaracaoMutuarios

Rules:
- Use _ to separate parts. No spaces. No special chars except - for dates.
- YYYY-MM = year and month shown in the document (e.g. 2025-11)
- Person = the person's first name as shown in the dossier (e.g. Tiago, Dalila)
- BankName = short name of the bank/institution
- If a detail is not visible, omit that part
- Reply with ONLY the filename stem, nothing else
"""


def _is_standard_name(stem: str) -> bool:
    """True if the filename already matches our strict naming convention."""
    for patterns in _COMPILED_RULES.values():
        for pat in patterns:
            if pat.match(stem):
                return True
    return False


def _sanitize_stem(raw: str) -> Optional[str]:
    """Clean an AI-returned filename stem: remove path chars, strip quotes."""
    s = raw.strip().strip('"\'').strip()
    s = re.sub(r'[/\\<>:"|?*]', '', s)
    # Remove extension if AI accidentally included it
    if '.' in s:
        s = s.rsplit('.', 1)[0]
    return s.strip() or None


def generate_standard_name(path: Path, category: str, person: Optional[str], ai_client) -> Optional[str]:
    """
    Ask Claude to generate a standardized filename for an already-organized file.
    Returns the new full filename (stem + original extension), or None if unchanged/failed.
    """
    ext = path.suffix          # preserve original extension (including case)
    ext_lower = ext.lower()

    # Render first page to JPEG
    b64 = media_type = None
    if ext_lower == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(path))
            pix = doc[0].get_pixmap(dpi=100)
            b64 = base64.standard_b64encode(pix.tobytes("jpeg")).decode()
            media_type = "image/jpeg"
            doc.close()
        except Exception as e:
            print(f"    [rename] PDF render error {path.name}: {e}")
            return None
    elif ext_lower in IMAGE_EXTS:
        media_type = "image/png" if ext_lower == ".png" else "image/jpeg"
        try:
            with open(path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
        except Exception:
            return None
    else:
        return None

    person_line = f"Person: {person}" if person else "Person: (shared / not specified)"
    prompt = RENAME_PROMPT.format(
        category=category,
        person_line=person_line,
        filename=path.name,
    )
    try:
        resp = ai_client.messages.create(
            model=VISION_MODEL,
            max_tokens=60,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw_stem = resp.content[0].text.strip()
        new_stem = _sanitize_stem(raw_stem)
        if not new_stem:
            return None
        new_name = new_stem + ext   # keep original extension
        if new_name == path.name:
            return None             # already has the right name
        return new_name
    except Exception as e:
        print(f"    [rename] API error {path.name}: {e}")
        return None

# Normalized rules — matched against accent-stripped, lower-cased, space-normalized names.
# Catches natural-language filenames like "Mapa CRC - Tiago", "Declaração Patronal - Dalila".
NORMALIZED_RULES: Dict[str, List[str]] = {
    "Rendimentos": [
        r"^recibo[s]?(\s|$)", r"^recibo de vencimento", r"^recibos de vencimento",
        r"declaracao patronal", r"decl patronal", r"^geprecib",
        r"^contrato de trabalho", r"\bcontract( addendum)?\b", r"certificat de salaire", r"salary (certificate|slip|cert)",
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
    """Ask Claude Haiku to classify a document by its first page (image or PDF)."""
    ext = path.suffix.lower()

    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(path))
            pix = doc[0].get_pixmap(dpi=120)
            img_bytes = pix.tobytes("jpeg")
            doc.close()
            b64 = base64.standard_b64encode(img_bytes).decode()
            media_type = "image/jpeg"
        except Exception as e:
            print(f"    PDF render error for {path.name}: {e}")
            return None
    else:
        media_type = "image/png" if ext == ".png" else "image/jpeg"
        try:
            with open(path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
        except Exception as e:
            print(f"    Read error for {path.name}: {e}")
            return None

    try:
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
    Returns (file_path, target_base_folder) for all files that need categorizing.

    Target base folder determines where standard subfolders are created:
    - client root  → for loose files, dissolve-folder contents, and shared docs
    - sub-client/  → for files belonging to a specific co-applicant or guarantor

    Sub-client folders (e.g. Ipshita/, Ribal/, fiador/) get their own standard
    subfolders.  Dissolve folders (processados, _Para_Verificar, HPP, etc.) have
    their contents redistributed into the nearest base folder.
    """
    results: List[Tuple[Path, Path]] = []
    standard_lower = {s.lower() for s in STANDARD_FOLDERS}

    # Loose files at root
    for f in get_loose_files(client_folder):
        results.append((f, client_folder))

    for item in client_folder.iterdir():
        if not item.is_dir():
            continue
        if item.name.lower() in standard_lower:
            continue  # already organised

        if _is_dissolve_folder(item.name):
            # Redistribute into client root
            for f in item.rglob("*"):
                if f.is_file() and f.suffix.lower() not in SKIP_EXTENSIONS:
                    results.append((f, client_folder))
        else:
            # Sub-client folder (Ipshita, Ribal, fiador, …)
            # Loose files → this sub-client's standard subfolders
            for f in get_loose_files(item):
                results.append((f, item))
            for sub in item.iterdir():
                if not sub.is_dir():
                    continue
                if sub.name.lower() in standard_lower:
                    continue  # already organised inside sub-client
                # Any nested subfolder inside sub-client → dissolve into sub-client
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


def find_inplace_image_merges(client_folder: Path, subclients: List[Path] = None) -> List[Dict]:
    """
    Find multi-page image groups already inside standard subfolders.
    These are merged in-place (no move needed, just combine to PDF).
    """
    merges = []
    scopes: List[Path] = [client_folder] + (subclients if subclients is not None else _get_subclient_folders(client_folder))

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


# Categories that belong to one person — AI can decide which.
# Imóvel and Proposta Crédito are typically shared, so left at root.
_PERSONAL_CATEGORIES = {
    "Documentos Pessoais", "Rendimentos", "Extratos Bancários",
    "IRS", "Mapa CRC", "RGPD",
}


def identify_person_by_vision(
    path: Path, person_names: List[str], ai_client
) -> Optional[str]:
    """
    Ask Claude which person a document belongs to when the filename gives no clue.
    Returns one of the person_names, or None if shared / uncertain.
    """
    ext = path.suffix.lower()
    b64 = media_type = None

    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(path))
            pix = doc[0].get_pixmap(dpi=120)
            b64 = base64.standard_b64encode(pix.tobytes("jpeg")).decode()
            media_type = "image/jpeg"
            doc.close()
        except Exception as e:
            print(f"    PDF render error ({path.name}): {e}")
            return None
    elif ext in IMAGE_EXTS:
        media_type = "image/png" if ext == ".png" else "image/jpeg"
        try:
            with open(path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
        except Exception:
            return None
    else:
        return None

    names_list = ", ".join(person_names)
    prompt = (
        f"Mortgage dossier with two applicants: {names_list}.\n"
        f"Look at whose name appears on this document as account holder, taxpayer, or subject.\n"
        f"Write one word only — exactly one of: {names_list}, shared.\n"
        f"No other words."
    )
    try:
        resp = ai_client.messages.create(
            model=VISION_MODEL,
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        result = resp.content[0].text.strip()
        # Verbose response: scan for a person name anywhere in the text
        if "shared" in result.lower():
            return None
        for name in person_names:
            if name.lower() in result.lower():
                return name
    except Exception as e:
        print(f"    Person-vision error ({path.name}): {e}")
    return None


def find_misrouted_files(
    client_folder: Path,
    subclients: List[Path],
    use_vision: bool = False,
    ai_client=None,
) -> List[Dict]:
    """
    Find files sitting in root standard folders that belong to a specific sub-client.
    1. Try filename pattern matching (fast, free).
    2. If no match and vision is enabled, ask Claude which person it belongs to.
    Files in shared categories (Imóvel, Proposta Crédito) are never reassigned.
    """
    moves = []
    person_names = [sc.name for sc in subclients]

    for std in STANDARD_FOLDERS:
        std_folder = client_folder / std
        if not std_folder.exists():
            continue
        for f in std_folder.iterdir():
            if not f.is_file() or f.suffix.lower() in SKIP_EXTENSIONS:
                continue
            stem_norm = normalize_stem(f.stem)

            # 1. Filename match
            matched = None
            for subclient in subclients:
                sc_name = normalize_stem(subclient.name)
                if any(pat.search(stem_norm) for pat in _name_patterns(sc_name)):
                    matched = subclient
                    break

            # 2. AI vision fallback — only for personal categories
            if matched is None and use_vision and ai_client and len(subclients) >= 2:
                if std in _PERSONAL_CATEGORIES and f.suffix.lower() in IMAGE_EXTS | {".pdf"}:
                    print(f"    [vision-person] {f.name}")
                    person_name = identify_person_by_vision(f, person_names, ai_client)
                    if person_name:
                        matched = next((sc for sc in subclients if sc.name == person_name), None)

            if matched is not None:
                target = matched / std / f.name
                if target.resolve() != f.resolve():
                    moves.append({"from": str(f), "to": str(target)})
    return moves


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
        "client_folder": str(client_folder),
        "new_folders": [],
        "moves": [],
        "merges": [],
        "duplicates": [],
        "uncategorized": [],
        "renames": [],
        "empty_root_folders": [],
    }

    # Collect standard folders to create — root level
    for std in STANDARD_FOLDERS:
        target = client_folder / std
        if not target.exists():
            plan["new_folders"].append(str(target))

    # Build full sub-client list: existing folders + auto-detected person names
    existing_subclients = _get_subclient_folders(client_folder)
    existing_lower = {sc.name.lower() for sc in existing_subclients}
    parsed_names = _parse_person_names(client_folder.name)
    extra_subclients = [
        client_folder / name
        for name in parsed_names
        if name.lower() not in existing_lower
    ]
    all_subclients = existing_subclients + extra_subclients

    for subclient in all_subclients:
        for std in STANDARD_FOLDERS:
            target = subclient / std
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

        if category is None and use_vision and ai_client:
            if file_path.suffix.lower() in IMAGE_EXTS | {".pdf"}:
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

    # Migrate files from root standard folders to per-sub-client folders
    for m in find_misrouted_files(client_folder, all_subclients,
                                   use_vision=use_vision, ai_client=ai_client):
        plan["moves"].append(m)

    # Also merge image groups that are already inside standard subfolders
    for mg in find_inplace_image_merges(client_folder, all_subclients):
        plan["merges"].append(mg)

    # Find exact duplicates
    for keep, delete in find_duplicates(client_folder):
        plan["duplicates"].append({
            "keep": str(keep),
            "delete": str(delete),
        })

    # ── Rename planning ─────────────────────────────────────────────────────
    if use_vision and ai_client:
        _plan_renames(plan, client_folder, all_subclients, ai_client)

    # ── Empty root personal folders (two-person clients only) ────────────────
    if all_subclients:
        for std in STANDARD_FOLDERS:
            folder = client_folder / std
            if folder.exists() and not any(folder.rglob("*")):
                plan["empty_root_folders"].append(str(folder))

    return plan


def _plan_renames(plan: Dict, client_folder: Path, all_subclients: List[Path], ai_client) -> None:
    """
    Populate plan["renames"] with standardized-name proposals.

    Two passes:
    1. Files already in their final organized location.
    2. Files scheduled to be moved — rename at the target path (AI reads source).
    """
    VISION_EXTS = IMAGE_EXTS | {".pdf", ".PDF"}
    already_planned: set = set()   # avoid double-planning same target path

    def _add_rename(source_path: Path, target_path: Path, category: str, person: Optional[str]):
        key = str(target_path)
        if key in already_planned:
            return
        if source_path.suffix.lower() not in {e.lower() for e in VISION_EXTS}:
            return
        if _is_standard_name(source_path.stem):
            return
        print(f"    [rename] {source_path.name}")
        new_name = generate_standard_name(source_path, category, person, ai_client)
        if new_name:
            plan["renames"].append({
                "path": str(target_path),
                "new_name": new_name,
            })
            already_planned.add(key)

    # Pass 1: files already organized at root standard folders
    for std in STANDARD_FOLDERS:
        std_folder = client_folder / std
        if not std_folder.exists():
            continue
        for f in std_folder.iterdir():
            if f.is_file() and f.suffix.lower() not in SKIP_EXTENSIONS:
                _add_rename(f, f, std, None)

    # Pass 1b: files already organized inside sub-client standard folders
    for sc in all_subclients:
        for std in STANDARD_FOLDERS:
            std_folder = sc / std
            if not std_folder.exists():
                continue
            for f in std_folder.iterdir():
                if f.is_file() and f.suffix.lower() not in SKIP_EXTENSIONS:
                    _add_rename(f, f, std, sc.name)

    # Pass 2: files being moved in this run — rename at target after move
    for move in plan["moves"]:
        src = Path(move["from"])
        target = Path(move["to"])
        rel = target.relative_to(client_folder)
        parts = rel.parts
        if len(parts) >= 2 and parts[0] in STANDARD_FOLDERS:
            category, person = parts[0], None
        elif len(parts) >= 3 and parts[1] in STANDARD_FOLDERS:
            person, category = parts[0], parts[1]
        else:
            continue
        _add_rename(src, target, category, person)


# ─── Report ────────────────────────────────────────────────────────────────

def print_plan(plans: List[Dict]) -> None:
    total_moves   = sum(len(p["moves"])                   for p in plans)
    total_merges  = sum(len(p["merges"])                  for p in plans)
    total_dupes   = sum(len(p["duplicates"])              for p in plans)
    total_uncat   = sum(len(p["uncategorized"])           for p in plans)
    total_folders = sum(len(p["new_folders"])             for p in plans)
    total_renames = sum(len(p.get("renames", []))         for p in plans)
    total_empty   = sum(len(p.get("empty_root_folders", [])) for p in plans)

    sep = "=" * 70
    print(f"\n{sep}")
    print("  KOVA FINANCE ORGANIZER — DRY RUN REPORT")
    print(sep)
    print(f"  Clients scanned  : {len(plans)}")
    print(f"  New folders      : {total_folders}")
    print(f"  Files to move    : {total_moves}")
    print(f"  Files to rename  : {total_renames}")
    print(f"  Image -> PDF merge: {total_merges} group(s)")
    print(f"  True duplicates  : {total_dupes}")
    print(f"  Uncategorized    : {total_uncat}")
    print(f"  Empty root fldrs : {total_empty}")
    print(sep)

    for p in plans:
        has_work = any([
            p["new_folders"], p["moves"], p["merges"],
            p["duplicates"], p["uncategorized"],
            p.get("renames", []), p.get("empty_root_folders", [])
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

        if p.get("renames"):
            for r in p["renames"]:
                old = Path(r["path"]).name
                print(f"   REN   {old}")
                print(f"     ->  {r['new_name']}")

        if p.get("empty_root_folders"):
            names = [Path(f).name for f in p["empty_root_folders"]]
            print(f"   RMDIR (empty shared-root): {', '.join(names)}")

    print(f"\n{sep}")
    print("  Run with --apply to execute.  Plan saved to organize_plan.json")
    print(f"{sep}\n")


# ─── Cleanup ───────────────────────────────────────────────────────────────

def cleanup_empty_root_folders(client_folder: Path, subclients: List[Path]) -> List[str]:
    """
    For two-person clients: remove empty root-level standard folders.
    Only runs when sub-client folders exist. Folders with any files are left untouched —
    shared docs (Imóvel, joint IRS, joint RGPD, etc.) survive naturally.
    """
    if not subclients:
        return []
    removed = []
    for std in STANDARD_FOLDERS:
        folder = client_folder / std
        if not folder.exists():
            continue
        if not any(folder.rglob("*")):   # empty — no files anywhere inside
            try:
                folder.rmdir()
                removed.append(str(folder))
            except OSError:
                pass
    return removed


def cleanup_empty_folders(client_folder: Path) -> List[str]:
    """
    Remove empty directories left over after organising (dissolved folders,
    old processados dirs, etc.).  Standard folders and sub-client folders that
    still have files are preserved.  Walks bottom-up so nested empties are
    caught in one pass.
    """
    standard_lower = {s.lower() for s in STANDARD_FOLDERS}
    removed = []

    dirs_deepest_first = sorted(
        (d for d in client_folder.rglob("*") if d.is_dir() and d != client_folder),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    skip_lower = {e.lower() for e in SKIP_EXTENSIONS}
    for d in dirs_deepest_first:
        if d.name.lower() in standard_lower:
            continue  # never remove standard folders
        files = [f for f in d.rglob("*") if f.is_file()]
        # Treat as empty if no files, or only skip-extension stubs (.action, .json, etc.)
        if not files or all(f.suffix.lower() in skip_lower for f in files):
            try:
                shutil.rmtree(str(d))
                removed.append(str(d))
            except OSError:
                pass
    return removed


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

        # Rename files to standardized names
        for r in p.get("renames", []):
            old_path = Path(r["path"])
            if not old_path.exists():
                continue
            new_stem = Path(r["new_name"]).stem
            new_ext  = Path(r["new_name"]).suffix or old_path.suffix
            new_path = old_path.parent / (new_stem + new_ext)
            if new_path == old_path:
                continue
            # Resolve conflicts: append _v2, _v3, …
            if new_path.exists():
                v = 2
                while new_path.exists() and v <= 20:
                    new_path = old_path.parent / f"{new_stem}_v{v}{new_ext}"
                    v += 1
            if new_path.exists():
                print(f"  SKIP rename (conflict)  {old_path.name}")
                continue
            old_path.rename(new_path)
            print(f"  rename  {old_path.name}  ->  {new_path.name}")
            actions += 1

        # Post-rename misrouting pass: after renames, some root standard-folder
        # files may now contain a person's name and belong in a sub-client folder.
        subclient_paths = _get_subclient_folders(Path(p["client_folder"]))
        extra_parsed = _parse_person_names(p["client"])
        all_sc = subclient_paths + [
            Path(p["client_folder"]) / name
            for name in extra_parsed
            if name.lower() not in {sc.name.lower() for sc in subclient_paths}
        ]
        if all_sc:
            for extra_move in find_misrouted_files(Path(p["client_folder"]), all_sc):
                src = Path(extra_move["from"])
                dst = Path(extra_move["to"])
                if not src.exists() or dst.exists():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                print(f"  re-route  {src.name}  ->  {dst.parent.parent.name}/{dst.parent.name}/")
                actions += 1

        # Remove empty leftover folders (dissolved dirs, old processados, etc.)
        removed = cleanup_empty_folders(Path(p["client_folder"]))
        for r in removed:
            try:
                rel = Path(r).relative_to(BASE_DIR)
            except ValueError:
                rel = Path(r)
            print(f"  rmdir  {rel}")
            actions += 1

        # Remove empty root standard folders for two-person clients
        removed_root = cleanup_empty_root_folders(Path(p["client_folder"]), all_sc)
        for r in removed_root:
            print(f"  rmdir (shared-root cleanup)  {Path(r).name}")
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
