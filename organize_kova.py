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

Per-client overrides (optional):
    Drop a .kova.json file in a client folder to state what the documents cannot say,
    e.g. which accounts the two applicants hold jointly:
        {"joint_accounts": ["PT50..."], "joint_files": ["Extrato_2026-04_CGD.pdf"]}
    See load_client_config().

Requirements:
    pip install anthropic img2pdf Pillow pymupdf
    ANTHROPIC_API_KEY must be set in environment (or use --api-key) for --apply vision mode
"""

import io
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

try:
    import pytesseract as _pytesseract
    HAS_PYTESSERACT = True
except ImportError:
    HAS_PYTESSERACT = False

# ─── Configuration ─────────────────────────────────────────────────────────

BASE_DIR = Path(r"C:\Users\anton\Desktop\Kova Finance")
VISION_MODEL = "claude-haiku-4-5-20251001"

STANDARD_FOLDERS = [
    "Documentos Pessoais",
    "Rendimentos",
    "Extratos Bancários",
    "Património",
    "IRS",
    "Imóvel",
    "Mapa CRC",
    "RGPD",
    "Proposta Crédito",
]

# Categories that belong to the mortgage application as a whole, never to one
# applicant — always live at the client root, even for two-person clients.
_SHARED_ONLY_CATEGORIES = {"RGPD", "Imóvel", "Proposta Crédito"}

# These subfolders are dissolved: their files are redistributed into standard folders
DISSOLVE_FOLDERS = {"documentos processados", "hpp"}

# Words that mark a subfolder as temporary/working (to be dissolved)
_DISSOLVE_WORDS = {"processados", "processadas", "verificar", "hpp"}

# Subfolders that are part of every mortgage application but must never be touched
SKIP_SUBFOLDER_NAMES = {"dossier e folha de rosto"}


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
        and item.name.lower() not in SKIP_SUBFOLDER_NAMES
        and not _is_dissolve_folder(item.name)
        and _subclient_has_content(item)
    ]

# Top-level names to skip
SKIP_NAMES = {".claude", ".git", "standby", "despesas valencia", "nova pasta", "_claude_review", "webapp", "__pycache__", "kova-app", "simulacoes teste"}

# File extensions to skip entirely
SKIP_EXTENSIONS = {".action", ".json", ".xlsx", ".xls", ".docx", ".doc", ".sqlite"}

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
        r"^RecVenc_", r"^DeclPatronal_", r"^ContratoTrabalho_", r"^RegistoArrendamento_",
        r"^DeclInternaPag", r"^DeclSocioGerente_", r"^DeclSubsidio",
        r"^FaturaAL_", r"^FaturaNegocios_", r"^RecibosVenc",
        r"^FaturaRecibo_", r"^ReciboVerde_", r"^ReciboRenda_", r"^ReciboAvenca_",
    ],
    "Extratos Bancários": [
        r"^Extrato_", r"^Extratos_",
    ],
    "Património": [
        r"^ContaPoupanca_", r"^CertificadoTesouro_", r"^CarteiraInvestimento_",
    ],
    "IRS": [
        r"^IRS_", r"^NotaLiq_IRS_", r"^NotaLiq_", r"^IES_", r"^DeclImpot_",
        r"^ComprovativoIRS_", r"^DeclIRS_", r"^Modelo3_", r"^Reembolso_",
        r"^P60_", r"^P45_", r"^P11D_",
    ],
    "Imóvel": [
        r"^CPCV[_.]", r"^CPCV$", r"^Adenda_",
        r"^CertidaoPredial[_.]", r"^CertidaoPredial$",
        r"^Caderneta_", r"^CadernaPredial", r"^CertificadoEnergetico",
        r"^LicencaUtilizacao",
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
        r"^Simulacao_SeguroVida", r"^Simulacao_Multirriscos",
    ],
    "Documentos Pessoais": [
        r"^CC_", r"^NIF_", r"^Passaporte_", r"^CartaoCidadao_",
        r"^TituloResidencia_", r"^AutorizacaoResidencia_", r"^AIMA_",
        r"^DomicilioFiscal_", r"^CompMorada_", r"^CompIBAN_",
        r"^CertNaoDivida", r"^CertPredialNegativa_",
        r"^DeclEmpresaEndereco", r"^DeclFinsBancarios",
        r"^DeclVinculo_", r"^CarreiraContrib", r"^CartaVerde_",
        r"^AMIM_",  # Atestado Médico de Incapacidade Multiúso
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
{property_line}

Document text (all pages, truncated):
{text_context}

Generate a standardized filename stem (NO extension) following these exact rules:

Rendimentos:
  payslip/recibo de vencimento  →  RecVenc_YYYY-MM_Person
  employer declaration          →  DeclPatronal_Person
  work contract                 →  ContratoTrabalho_Person
  income declaration            →  DeclRendimentos_YYYY_Person
  social-manager declaration    →  DeclSocioGerente_Person
  salary declaration            →  DeclInternaPagSalarial_Person
  freelance invoice-receipt (recibo verde / fatura-recibo)  →  FaturaRecibo_YYYY-MM_Person
  AT portal listing of issued fatura-recibo over a period    →  ReciboVerde_YYYY-MM_YYYY-MM_Person
  rent receipt (recibo de renda) →  ReciboRenda_YYYY-MM_Property_Person
  AT lease registration (Comunicacao de Contratos de Arrendamento, Modelo 2)
                                 →  RegistoArrendamento_Property
  avença / nota discriminativa dos atos clínicos (contrato de avença)  →  ReciboAvenca_YYYY-MM_Person
  payslip from a "Boletim de Vencimentos" template  →  RecVenc_YYYY-MM_Person  (same as any other payslip)

Extratos Bancários:
  CURRENT/CHECKING account statement ONLY (depósito à ordem) →  Extrato_YYYY-MM_BankName_Person
    (joint/no clear person      →  Extrato_YYYY-MM_BankName)
  short bank names: BCP BPI CGD Santander NovoBanco Revolut Wise ActivoBank Montepio Itau
  Savings accounts, term deposits, treasury bonds/certificates, and investment/brokerage
  statements are NOT bank statements — they go under Património below, even if their
  old filename starts with "Extrato".

Património (savings, investments, bonds — NOT everyday bank statements):
  savings/term-deposit account (conta poupança, aplicação a prazo)  →  ContaPoupanca_YYYY-MM_BankName_Person
  treasury bonds/certificates (IGCP, certificados de aforro/tesouro) →  CertificadoTesouro_YYYY-MM_Person
  investment/brokerage statement (carteira de investimento, stocks, funds) →  CarteiraInvestimento_YYYY-MM_BankName_Person

IRS:
  Modelo 3 IRS declaration (comprovativo de entrega, from AT portal)
                                →  Modelo3_YYYY  (joint couple — no person suffix)
                                →  Modelo3_YYYY_Person  (single filer)
  tax declaration (other)       →  IRS_YYYY_Person
  liquidation note              →  NotaLiq_IRS_YYYY_Person
  A married couple filing jointly has ONE declaration and ONE liquidation note
  for both of them: when the document names two sujeitos passivos (A and B),
  leave the person suffix off entirely - Modelo3_YYYY, IRS_YYYY, NotaLiq_IRS_YYYY.
  IES report                    →  IES_YYYY_Person
  UK P60 end-of-year certificate →  P60_YYYY-YY_Person  (YYYY-YY = UK tax year, e.g. 2025-26)
  UK P45 leaving employment      →  P45_YYYY-MM_Person
  UK P11D benefits in kind       →  P11D_YYYY-YY_Person
  Swiss cantonal tax return / quittance de declaration d'impot
                                 →  DeclImpot_YYYY_Person  (YYYY = periode fiscale)

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
  disability certificate (AMIM) →  AMIM_Person

Mapa CRC:
  CRC map (Banco de Portugal)    →  MapaCRC_YYYY-MM_Person
  Foreign credit bureau report   →  MapaCRC_BureauName_YYYY-MM_Person
    (BureauName: Experian, Equifax, TransUnion, etc.)

RGPD:
  consent form                  →  RGPD_Person

Imóvel:
  property certificate          →  CertidaoPredial_ArtigoN  (N = Artigo Matricial number shown; omit suffix if not visible)
  land register (caderneta)     →  CadernaPredial_ArtigoN  (N = Artigo Matricial number shown; omit suffix if not visible)
    A dossier can have several cadernetas/certidões for different property units (house, garage,
    storage) — each has its own Artigo Matricial number, so always include it to keep them distinct.
  energy certificate            →  CertificadoEnergetico
  usage licence                 →  LicencaUtilizacao
  property plans                →  Plantas
  CPCV                          →  CPCV
  purchase deed (escritura)     →  Escritura
  unit sheet (unidade aloj.)    →  UnidAloj
  addendum                      →  Adenda

Proposta Crédito:
  credit proposal               →  Proposta_BankName_YYYY-MM
  mortgage simulation           →  Simulacao_BankName_YYYY-MM
  life insurance simulation     →  Simulacao_SeguroVida_BankName
  multi-risk insurance sim      →  Simulacao_Multirriscos_BankName
  bank form                     →  FormularioBanco_BankName
  borrower declaration          →  DeclaracaoMutuarios

  To distinguish simulations: look for words like "seguro de vida", "vida",
  "morte", "invalidez" (→ SeguroVida) or "multirriscos", "habitação", "incêndio"
  (→ Multirriscos). If neither, it is a mortgage simulation → use BankName only.
  BankName examples: BPI BCP CGD Santander NovoBanco ActivoBank Montepio Caixa

Rules:
- Use _ to separate parts. No spaces. No special chars except - for dates.
- YYYY-MM = year and month shown in the document (e.g. 2025-11)
- Person = the person's first name as shown in the dossier (e.g. Tiago, Dalila)
- Property = which let property the rent document concerns, given to you above when
  the document says. A landlord receives one receipt a month per property, so without
  it the ground floor and the annex of one building look like two months of one let.
- BankName = short name of the bank/institution
- If a detail is not visible, omit that part
- Multi-period documents: if the file contains more than one period (e.g. two payslips,
  April and May in the same PDF), include both months: RecVenc_2026-04_2026-05_Person
- UK tax year (P60): use the two-year range, e.g. IRS_2025-26_Person
- Reply with ONLY the filename stem, nothing else
"""


def _is_standard_name(stem: str, category: Optional[str] = None) -> bool:
    """
    True if the filename already matches our strict naming convention.
    If `category` is given, only that category's patterns count — a filename that
    happens to match a DIFFERENT category's rule (e.g. an avença invoice previously
    misnamed "NotaLiq_IRS_...") should still be renamed once it's been recategorized.
    """
    rule_sets = [_COMPILED_RULES[category]] if category else _COMPILED_RULES.values()
    for patterns in rule_sets:
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
    # The model is told to leave a name out when it cannot read one; some replies
    # write UNKNOWN in its place instead, and it then sticks to the file for good.
    s = "_".join(t for t in s.split("_")
                 if t and t.lower() not in {"unknown", "desconhecido", "na", "none"})
    s = s.strip()
    if not s:
        return None
    # Guard against the model echoing back the prompt's meta-variable tokens
    # literally (e.g. "CC_Person", "Extrato_BankName") instead of a real value —
    # happens when it couldn't read the document (poor scan quality, etc).
    tokens = {t.lower() for t in re.split(r'[_\-]', s) if t}
    if tokens & {"person", "bankname", "firstname", "yyyy", "mm", "dd"}:
        return None
    return s


def generate_standard_name(path: Path, category: str, person: Optional[str], ai_client) -> Optional[str]:
    """
    Ask Claude to generate a standardized filename for an already-organized file.
    Returns the new full filename (stem + original extension), or None if unchanged/failed.
    """
    ext = path.suffix          # preserve original extension (including case)
    ext_lower = ext.lower()

    b64, media_type = _vision_image(path, ai_client, dpi=100)
    if not b64:
        return None

    person_line = (
        f"Person: {person}" if person
        else "Person: not known — do NOT guess, and do NOT write a placeholder such as "
             "UNKNOWN. Leave the name out of the filename entirely."
    )
    full_text = document_text(path, ai_client)
    # Only rent documents get a property: a payslip naming the employer's street
    # has an address too, and it identifies nothing.
    property_tag = _rent_property_tag(full_text) if _is_rent_document(full_text) else None
    property_line = (
        f"Property: {property_tag}  (include this in the filename exactly as written)"
        if property_tag else "Property: not applicable"
    )
    text_context = full_text[:4000].strip() if full_text else "(no text layer — image scan)"
    prompt = RENAME_PROMPT.format(
        category=category,
        person_line=person_line,
        property_line=property_line,
        text_context=text_context,
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
        # Telling two tenancies apart is the whole point of the name, so this is not
        # left to whether the model remembered to include it. It applies only when
        # the document was named as a rent document: dossiers hold PDFs with a
        # payslip and a rent receipt bound together, and a payslip takes no property.
        if property_tag and _RENT_NAME_RE.match(new_stem):
            new_stem = _insert_property_tag(new_stem, property_tag, person)
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
        # Recibo verde / fatura-recibo (self-employed invoice-receipt) and
        # recibo de renda (rent receipt) both count as income, not property docs
        r"fatura.?recibo", r"recibo verde",
        r"^recibo.*renda", r"recibo de renda",
    ],
    "Extratos Bancários": [
        r"^extratos?( |$)", r"^extractos?( |$)", r"^ext (sant|bpi|bcp|ctt|cgd|novo|millen)",
        r"^extrato combinado", r"^extracto integrado", r"^extrato global",
        r"itau.*extrato", r"^wise (trimestral|mensal)",
        r"^banco itau", r"^dif itau",
    ],
    "Património": [
        r"conta poupanca", r"^poupanca", r"solucao poupanca",
        r"aplicacao a prazo", r"deposito a prazo",
        r"certificado.*(aforro|tesouro)", r"^igcp", r"\bigcp\b",
        r"carteira.*investimento", r"interactive brokers", r"activity statement",
        r"investimentos financeiros",
    ],
    "IRS": [
        r"^irs( |\+|$)", r"nota de liquidac", r"nota liquidac",
        r"^dipf", r"declaracao.*irs", r"attestation.*impots",
        # Swiss / French-language tax returns — the equivalent of the Portuguese IRS
        r"declaration d.impot", r"declaracao.*rendimentos.*sui", r"avis de taxation",
        r"^declimpot", r"vaudtax", r"taxation.*(vaud|geneve|canton)",
        r"informederendimentosfinanceiros", r"declar.*ano.*ex",
        r"^comprovativo ir[_\s]",  # Brazilian IR (income tax) proof
        r"modelo.?3", r"comprovativo.*modelo",
        # UK annual tax forms — equivalent to Portuguese IRS
        r"\bp60\b", r"\bp45\b", r"\bp11d\b",
    ],
    "Mapa CRC": [
        r"mapa.?crc", r"resp(onsabilidades)?.?(bp|banco)", r"responsabilidades.*banco",
        r"banco.?portugal",  # catches typos like "Respsabilidades BANCO Portugal"
        r"^mapa_crc",
        # UK/international credit bureaus — their reports are equivalent to Mapa CRC
        r"experian", r"equifax", r"transunion", r"credit report", r"credit score",
    ],
    "Documentos Pessoais": [
        r"^cc ?-", r"^cc [a-z]", r"^nif( |$)", r"^niss( |$)",
        r"^passaporte( |$)", r"titulo.?residencia", r"titulo.?de.?residencia",
        r"^comprovativo de morada", r"^comprovativo morada", r"^comp.?morada",
        r"^identificacao", r"^numero de utente", r"^iban( |$)",
        r"certidao de casamento", r"^certidao.*casamento",
        r"atestado.*incapacidade", r"atestado multiusos", r"^amim",  # AMIM disability cert
        # UK/foreign utility providers used as address proof
        r"thames water", r"southern water", r"anglian water", r"severn trent",
        r"yorkshire water", r"united utilities", r"welsh water", r"wessex water",
        r"british gas", r"octopus energy", r"eon energy", r"edf energy",
        r"utility bill", r"water bill", r"electricity bill", r"gas bill",
    ],
    "Imóvel": [
        r"^escritura( de| $)", r"^caderneta", r"^certidao predial",
        r"certidao.*predial", r"unid.*aloj",
        r"^contrato.*arrendamento",  # lease contract itself (not the rent receipt)
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
        r"\bsimulac",           # catches "simulacao" anywhere in the name
        r"seguro.?(de.?)?vida", # life insurance simulation
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
    "IBAN proof CompIBAN, address proof CompMorada including foreign utility bills "
    "(Thames Water, British Gas, electricity/gas/water bill in any language), "
    "fiscal domicile, debt-free certificates CertNaoDivida, career history CarreiraContributiva, "
    "disability certificate AMIM / Atestado Médico de Incapacidade Multiúso)\n"
    "- Rendimentos (payslip RecVenc / Boletim de Vencimentos, employer declaration DeclPatronal, "
    "the AT lease registration form Comunicacao de Contratos de Arrendamento (Modelo 2) "
    "and the electronic rent receipt Recibo de Renda — the applicant is the landlord "
    "collecting the rent, so these are income, not a bank form, however box-ruled they look, "
    "work contract ContratoTrabalho, income declarations, freelance invoice-receipt "
    "recibo verde / fatura-recibo, rent receipt recibo de renda, "
    "an AT portal \"Faturas e Recibos\" screen listing issued FATURA-RECIBO documents "
    "(the applicant's own recibos verdes), "
    "freelance avença earnings statement / Nota Discriminativa dos Atos Clínicos / Contrato de Avença; "
    "NOT a P60 or P45 — those are annual tax summaries and go under IRS)\n"
    "- Extratos Bancários (CURRENT/CHECKING account statement only — depósito à ordem)\n"
    "- Património (savings/term-deposit account extract, treasury bonds/certificates IGCP, "
    "investment or brokerage account statement — stocks, funds, bonds; NOT a checking-account "
    "statement even if it looks similar)\n"
    "- IRS (tax return declaration, IRS liquidation note NotaLiquidacao, IES annual report, "
    "UK annual tax forms: P60 end-of-year certificate, P45 leaving employment, "
    "or a Swiss/French cantonal tax return - declaration d'impot, quittance or avis "
    "de taxation from an Administration cantonale des impots)\n"
    "- Imóvel (CPCV purchase promise, property certificate CertidaoPredial, "
    "land register Caderneta Predial, energy certificate, usage licence, "
    "lease/rental contract, property plans or drawings)\n"
    "- Mapa CRC (Banco de Portugal credit responsibility map, or foreign credit bureau report "
    "such as Experian, Equifax, TransUnion — even if in English)\n"
    "- RGPD (data protection / GDPR consent form with signature)\n"
    "- Proposta Crédito (credit proposal, bank simulation, mortgage application form, "
    "life insurance simulation / seguro de vida simulation, "
    "borrower declaration, solvency assessment)\n\n"
    "Important: the AT / Autoridade Tributaria e Aduaneira logo or portal chrome does "
    "NOT by itself make a page an IRS document. A list of issued fatura-recibo is "
    "income (Rendimentos); only an actual tax return, comprovativo de entrega or "
    "nota de liquidacao is IRS. Likewise, a bank statement that merely lists the AT "
    "as a direct-debit creditor is still Extratos Bancarios.\n\n"
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
    norm = normalize_stem(stem)
    # 0. Património's keyword rules are specific (igcp, poupança, interactive
    # brokers...) and must win over the generic "Extrato_"/"Extratos_" prefix,
    # which predates this category and can still appear on legacy savings/
    # investment filenames (e.g. "Extrato_2026-06_IGCP").
    for pat in _COMPILED_NORMALIZED.get("Património", []):
        if pat.search(norm):
            return "Património"
    # 1. Try strict prefix rules (original casing)
    for category, patterns in _COMPILED_RULES.items():
        for pat in patterns:
            if pat.match(stem):
                return category
    # 2. Try normalized rules (accent-stripped, space-normalized)
    for category, patterns in _COMPILED_NORMALIZED.items():
        for pat in patterns:
            if pat.search(norm):
                return category
    return None


# Phrases that only show up in the document's actual BODY TEXT, not in filenames.
# Used both to categorize freshly-found files with generic names, and to catch
# documents already filed in the wrong standard folder under a misleading name
# (e.g. a payslip someone mislabeled "NotaLiq_IRS_...").
CONTENT_RULES: Dict[str, List[str]] = {
    "Rendimentos": [
        r"boletim de vencimentos", r"recibo de vencimentos",
        r"nota discriminativa.*atos clinicos", r"contrato de avenca",
        # The AT form registering a lease (Modelo 2) and the electronic rent
        # receipt. An applicant who lets property is the landlord here, so both
        # evidence income. The form is a grid of boxes and was being read as a
        # bank credit form by sight alone.
        r"comunicacao de contratos? arrendamento",
        r"recibo de renda eletronico",
    ],
    "IRS": [
        # Portuguese IRS documents (born-digital PDFs from AT portal).
        # NOTE: the bare "Autoridade Tributaria e Aduaneira" letterhead is NOT a
        # signal - it also shows up inside a bank statement's direct-debit
        # authorisation table (the AT is the creditor for IUC/IMI debits), which
        # used to drag whole CGD statements into IRS. Only phrases that belong to
        # an actual tax return / assessment count.
        r"declaracao de rendimentos.*irs", r"modelo.?3",
        r"comprovativo.*modelo.?3", r"comprovativo de entrega.*irs",
        r"nota de liquidacao", r"nota liquidacao",
        # UK annual tax forms - equivalent to Portuguese IRS
        r"p60", r"end.of.year certificate", r"total for year",
        r"p45", r"details of employee leaving",
    ],
    "Documentos Pessoais": [
        # Utility bills (electricity/gas/water) double as comprovativo de morada
        # (address proof) — they share the "Extrato"/"Fatura" filename prefix
        # with real bank/income docs but are neither. Matched against the header
        # only (see categorize_by_content) — a bank statement can have a "DD EDP
        # COMERCIAL" direct-debit *line* deep in its transaction table, which is
        # not the same as the document itself being an EDP bill.
        r"periodo de fatura", r"periodo de factura",
        # AMIM disability certificate. "grau de incapacidade" on its own is NOT
        # enough: the blank Modelo 3 IRS form asks for it in Quadro 3, which used
        # to file whole IRS declarations under Documentos Pessoais.
        r"atestado.*incapacidade", r"atestado medico.*multiuso",
        r"junta medica.*incapacidade", r"incapacidade multiuso",
        # English-language utility bills (UK address proofs)
        r"thames water", r"southern water", r"anglian water", r"severn trent",
        r"yorkshire water", r"united utilities", r"welsh water", r"wessex water",
        r"british gas", r"octopus energy", r"eon energy", r"edf energy",
        r"billing period",
    ],
    "Património": [
        # Savings/term-deposit accounts and treasury bonds/certificates are wealth/
        # assets, not a current-account bank statement and not earned income.
        # Matched against the header only — a normal checking-account statement
        # can still contain an empty/zero "CONTA POUPANÇA" section, or a "pag.
        # igcp" treasury transfer line, deep in its transaction table.
        r"conta poupanca", r"solucao poupanca", r"aplicacao a prazo",
        r"deposito a prazo", r"certificados de aforro", r"certificados do tesouro",
        r"activity statement", r"net asset value",
        r"interactive brokers",
        # Brazilian investment-fund position statements (Itaú "Dados do fundo" /
        # "Saldo total em cotas" pages) — these share the "Extrato" filename
        # prefix with real bank statements but report fund quotas, not cash.
        r"dados do fundo", r"saldo total em cotas", r"fundo.?subconta",
    ],
}
_COMPILED_CONTENT_RULES: Dict[str, List] = {
    cat: [re.compile(p, re.IGNORECASE) for p in patterns]
    for cat, patterns in CONTENT_RULES.items()
}

# How many characters of the full extracted text (all pages) are checked for content
# classification. Large enough to cover a multi-page document's identifying headers,
# but capped to avoid false positives from transaction-table body text deep in bank
# statements (e.g. a "DD EDP COMERCIAL" debit line isn't the same as being an EDP bill).
_CONTENT_HEADER_CHARS = 3000


# Headings that name the tax document itself. They are only conclusive on the
# document's FIRST page: dossiers are full of merged PDFs — a bank credit form, a
# Mapa CRC or an address proof with an IRS declaration or assessment stapled behind
# it — and finding one of these a few pages in would re-file the whole thing as IRS.
# Phrases that only identify the document when they sit in its letterhead, at the
# very top of page 1. A Swiss bank statement names the cantonal tax office too, as
# the payee of a quarterly tax instalment, part-way down the transaction table.
_LETTERHEAD_CHARS = 400

LETTERHEAD_IRS_MARKERS = [
    r"administration cantonale des impots",
    r"declaration d.impot",
    r"avis de taxation",
]
_COMPILED_LETTERHEAD_IRS_MARKERS = [re.compile(p, re.IGNORECASE) for p in LETTERHEAD_IRS_MARKERS]

FIRST_PAGE_IRS_MARKERS = [
    # Quadro headings of the Modelo 3 declaration form
    r"estado civil do sujeito passivo",
    r"opcao pela tributacao conjunta",
    # Nota de liquidação (IRS assessment)
    r"demonstracao de liquidacao de irs",
]
_COMPILED_FIRST_PAGE_IRS_MARKERS = [re.compile(p, re.IGNORECASE) for p in FIRST_PAGE_IRS_MARKERS]


def _extract_pdf_first_page(path: Path) -> str:
    """Text layer of page 1 only — the part that says what the document IS."""
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        import fitz
        doc = fitz.open(str(path))
        text = doc[0].get_text() if len(doc) else ""
        doc.close()
        return text
    except Exception:
        return ""


def categorize_by_content(text: str, first_page: str = "") -> Optional[str]:
    if not text:
        return None
    norm = normalize_stem(text[:_CONTENT_HEADER_CHARS])

    if first_page:
        letterhead = normalize_stem(first_page[:_LETTERHEAD_CHARS])
        if any(pat.search(letterhead) for pat in _COMPILED_LETTERHEAD_IRS_MARKERS):
            return "IRS"
        head = normalize_stem(first_page[:_CONTENT_HEADER_CHARS])
        if any(pat.search(head) for pat in _COMPILED_FIRST_PAGE_IRS_MARKERS):
            return "IRS"

    for category, patterns in _COMPILED_CONTENT_RULES.items():
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


# ─── Page orientation ──────────────────────────────────────────────────────
# Phone photos of documents are routinely saved sideways, and a sideways page
# defeats the vision model outright: the back of a citizen's card came back
# classified as a bank credit proposal, and the front yielded a date of birth
# where its tax number should have been. Orientation is fixed before any vision
# call rather than left for the model to reason about.

_UPRIGHT_PAIR_PROMPT = (
    "Two versions of the same document, A first then B. Exactly one of them has its "
    "printed text upright and readable left-to-right; the other is sideways or upside "
    "down. Reply with exactly one letter: A or B."
)

# Printed lines make consecutive image rows alternate dark and light, so upright text
# varies far more down the page than across it, and a quarter-turned page is the
# reverse. Over 25 born-digital pages the ratio never fell below 1.5 upright and never
# rose above 0.7 once turned, so 0.8 separates them with room to spare. The ratio only
# nominates a candidate; the model still has to confirm before anything is rotated.
_UPRIGHT_RATIO_MIN = 0.8

# Well under the turned pages' worst case of 0.7, and far from any upright page seen.
# Below this the measurement is decisive, so only the direction is put to the model;
# asking it to re-confirm as well just adds a call that can go the wrong way by chance.
_UPRIGHT_RATIO_SURE = 0.55

# Rotation decided per file, so repeated vision calls on one document agree and pay
# for the orientation check once.
_ORIENTATION_CACHE: Dict[str, int] = {}


def _line_banding_ratio(im) -> float:
    """Horizontal line structure over vertical. Above 1 the text reads across."""
    import statistics
    from PIL import ImageOps
    g = ImageOps.grayscale(im.convert("RGB"))
    g.thumbnail((600, 600))
    w, h = g.size
    if w < 8 or h < 8:
        return 1.0
    px = g.load()
    rows = [sum(px[x, y] for x in range(w)) / w for y in range(h)]
    cols = [sum(px[x, y] for y in range(h)) / h for x in range(w)]
    dr = statistics.pstdev([rows[i + 1] - rows[i] for i in range(h - 1)]) or 1e-6
    dc = statistics.pstdev([cols[i + 1] - cols[i] for i in range(w - 1)]) or 1e-6
    return dr / dc


def _encode_image(im, quality: int = 85) -> str:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _ask_which_upright(im_a, im_b, ai_client) -> str:
    """Which of two candidate orientations reads upright. Comparing two pictures is
    far steadier than asking for a rotation in the abstract, which confuses 90 with 270."""
    def thumb(im):
        t = im.copy()
        t.thumbnail((760, 760))
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": _encode_image(t, 80)}}
    resp = ai_client.messages.create(
        model=VISION_MODEL,
        max_tokens=5,
        messages=[{"role": "user", "content": [thumb(im_a), thumb(im_b),
                                               {"type": "text", "text": _UPRIGHT_PAIR_PROMPT}]}],
    )
    return resp.content[0].text.strip().upper()[:1]


def _upright_rotation(path: Path, im, ai_client) -> int:
    """Degrees counter-clockwise needed to stand this page up. 0 when it already is."""
    key = str(path)
    if key in _ORIENTATION_CACHE:
        return _ORIENTATION_CACHE[key]
    rot = 0
    try:
        ratio = _line_banding_ratio(im) if (ai_client or ocr_available()) else 1.0

        if ratio >= _UPRIGHT_RATIO_MIN:
            # Text already runs across the page, so at worst it is upside down.
            if ocr_available() and _osd_says_upside_down(im):
                print(f"    [orient] {path.name}  reading it rotated 180deg")
                _ORIENTATION_CACHE[key] = 180
                return 180
            _ORIENTATION_CACHE[key] = 0
            return 0

        # Text runs down the page. Reading it both ways settles which quarter turn,
        # and the model is only asked when there is no OCR to read it with.
        if ocr_available():
            turn = _ocr_pick_quarter_turn(im)
            if turn:
                print(f"    [orient] {path.name}  reading it rotated {turn}deg (ocr)")
                _ORIENTATION_CACHE[key] = turn
                return turn

        if ai_client:
            quarter = 90 if _ask_which_upright(im.rotate(90, expand=True),
                                               im.rotate(270, expand=True), ai_client) == "A" else 270
            # Near the threshold, leaving the page alone is the default: the turned
            # version has to beat the original before anything is rotated.
            if (ratio < _UPRIGHT_RATIO_SURE
                    or _ask_which_upright(im.rotate(quarter, expand=True), im, ai_client) == "A"):
                rot = quarter
                print(f"    [orient] {path.name}  reading it rotated {rot}deg")
    except Exception as e:
        print(f"    [orient] {path.name}: {e}")
    _ORIENTATION_CACHE[key] = rot
    return rot


# ─── OCR (optional) ────────────────────────────────────────────────────────
# Tesseract is a system install rather than a Python package, so every use of it
# here is optional: without it the organizer behaves exactly as it did before.
#
# It does two jobs, and deliberately not a third. It settles which way up a page
# is, by reading it each way round and keeping whichever yields real words, so the
# question no longer goes to the model. And it gives a text layer to scans and
# photos, which until now arrived with none — so the content rules, the tax-number
# registry and the joint-document check simply did not apply to them.
#
# It does NOT get a say in re-filing a document that is already filed. OCR text is
# noisier than a real text layer, and a wrong reading there would move a document
# a human had already put in the right place.

# Dossiers arrive in Portuguese, and foreign applicants bring French and English
# ones. Whichever of these Tesseract actually has installed is what gets used.
OCR_WANTED_LANGS = os.environ.get("KOVA_OCR_LANGS", "por+fra+eng")
OCR_LANGS = "eng"          # narrowed to what is installed by ocr_available()

# The Windows installer only ships English unless it is run with administrator
# rights, so extra language files are looked for in a user-writable directory too.
OCR_TESSDATA_DIR = os.environ.get("KOVA_TESSDATA") or str(
    Path(os.environ.get("LOCALAPPDATA", "")) / "Kova-Tesseract" / "tessdata")
OCR_DPI = 200
# Everything read out of OCR text — the letterhead checks, the header rules, the
# names and tax numbers, the words handed to the renamer — comes from the opening
# pages. Reading a whole 13-page scanned contract costs minutes and adds nothing.
OCR_MAX_PAGES = 2

# Tesseract gains nothing from a 12-megapixel phone photo and takes noticeably
# longer over one. Shrinking further than this starts losing small print.
OCR_MAX_PIXELS = 2200
_OCR_MIN_CHARS = 40        # under this the result is speckle, not text
_OCR_MIN_OSD_CONF = 2.0    # tesseract's own confidence in the orientation it reports
_OCR_ORIENT_MARGIN = 1.25  # how much more readable a turn must be before it is used

OCR_CACHE_PATH = BASE_DIR / ".ocr_cache.sqlite"

_OCR_ENABLED = True        # cleared by --no-ocr
_OCR_READY: Optional[bool] = None


def disable_ocr() -> None:
    global _OCR_ENABLED
    _OCR_ENABLED = False


def ocr_available() -> bool:
    """True when Tesseract can actually be called. Resolved once per run."""
    global _OCR_READY, OCR_LANGS
    if not _OCR_ENABLED or not HAS_PYTESSERACT or not HAS_PIL:
        return False
    if _OCR_READY is not None:
        return _OCR_READY
    exe = shutil.which("tesseract")
    if not exe:
        for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        ):
            if candidate.is_file():
                exe = str(candidate)
                break
    if not exe:
        _OCR_READY = False
        return False
    # Point Tesseract at the extra language data through its own environment
    # variable. Passing --tessdata-dir instead would break on any path containing
    # a space, because pytesseract splits a config string on whitespace.
    if OCR_TESSDATA_DIR and Path(OCR_TESSDATA_DIR).is_dir():
        os.environ["TESSDATA_PREFIX"] = OCR_TESSDATA_DIR
    try:
        _pytesseract.pytesseract.tesseract_cmd = exe
        _pytesseract.get_tesseract_version()
        installed = set(_pytesseract.get_languages())
        wanted = [l for l in OCR_WANTED_LANGS.split("+") if l in installed]
        if not wanted:
            print(f"    [ocr] no usable language data for {OCR_WANTED_LANGS}")
            _OCR_READY = False
            return False
        OCR_LANGS = "+".join(wanted)
        _OCR_READY = True
    except Exception as e:
        print(f"    [ocr] Tesseract found at {exe} but unusable: {e}")
        _OCR_READY = False
    return _OCR_READY


def _ocr_cache_get(key: str) -> Optional[str]:
    try:
        with sqlite3.connect(OCR_CACHE_PATH) as con:
            con.execute("CREATE TABLE IF NOT EXISTS ocr (k TEXT PRIMARY KEY, text TEXT)")
            row = con.execute("SELECT text FROM ocr WHERE k=?", (key,)).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def _ocr_cache_put(key: str, text: str) -> None:
    try:
        with sqlite3.connect(OCR_CACHE_PATH) as con:
            con.execute("CREATE TABLE IF NOT EXISTS ocr (k TEXT PRIMARY KEY, text TEXT)")
            con.execute("INSERT OR REPLACE INTO ocr (k, text) VALUES (?, ?)", (key, text))
    except sqlite3.Error:
        pass


def _ocr_read(im) -> str:
    """Text of one already-upright page image."""
    try:
        page = im.convert("RGB")
        if max(page.size) > OCR_MAX_PIXELS:
            page = page.copy()
            page.thumbnail((OCR_MAX_PIXELS, OCR_MAX_PIXELS))
        return _pytesseract.image_to_string(page, lang=OCR_LANGS) or ""
    except Exception as e:
        print(f"    [ocr] read failed: {e}")
        return ""


def _osd_says_upside_down(im) -> bool:
    """
    Whether Tesseract reckons this page is turned through half a circle. That is the
    one case no measurement of line direction can see, since upside-down text still
    runs across the page. Only the half turn is taken from Tesseract: the sign of the
    angle it reports is a convention, and trusting it stood a citizen's card on its
    head, so the quarter turns are settled by reading the page instead.
    """
    try:
        from pytesseract import Output
        osd = _pytesseract.image_to_osd(im, output_type=Output.DICT)
    except Exception:
        return False                     # too little text to judge, or no osd data
    if float(osd.get("orientation_conf") or 0) < _OCR_MIN_OSD_CONF:
        return False
    if int(osd.get("rotate") or 0) % 360 != 180:
        return False
    # Confirm by reading: turning a page that was the right way up would be worse
    # than leaving a rare upside-down one alone.
    upside_down = _ocr_legibility(im.rotate(180, expand=True))
    return upside_down > _ocr_legibility(im) * _OCR_ORIENT_MARGIN


def _ocr_legibility(im) -> float:
    """How much readable text Tesseract finds, weighted by its own confidence."""
    try:
        from pytesseract import Output
        t = im.convert("RGB")
        t.thumbnail((OCR_MAX_PIXELS, OCR_MAX_PIXELS))
        data = _pytesseract.image_to_data(t, lang=OCR_LANGS, output_type=Output.DICT)
    except Exception:
        return 0.0
    score = 0.0
    for word, conf in zip(data.get("text", []), data.get("conf", [])):
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if c > 0 and word and word.strip():
            score += c * len(word.strip())
    return score


def _ocr_pick_quarter_turn(im) -> Optional[int]:
    """
    Which quarter turn stands a sideways page up, decided by reading it both ways.
    Orientation detection gives up on pages with few characters — a photographed
    form in a large frame, an ID card — and this still works there, without a
    vision call. A clear winner is required so a page of noise changes nothing.
    """
    scores = {r: _ocr_legibility(im.rotate(r, expand=True)) for r in (90, 270)}
    best, other = sorted(scores, key=lambda r: scores[r], reverse=True)
    if scores[best] <= 0 or scores[best] < scores[other] * _OCR_ORIENT_MARGIN:
        return None
    return best


# A photographed document is often pasted into a PDF at a fraction of the page, and
# rendering that page at a fixed resolution throws the detail away: an identity card
# sitting a third of the way across an A4 sheet came back as nothing at all. Pulling
# the embedded picture out at the size it was actually stored recovers it.
_OCR_MIN_EMBEDDED_PX = 400
_OCR_MAX_EMBEDDED = 4


def _pdf_page_images(doc, index: int) -> List:
    """Pictures embedded in a page, at their stored resolution."""
    import fitz
    out = []
    try:
        infos = doc[index].get_images(full=True)
    except Exception:
        return []
    if not infos or len(infos) > _OCR_MAX_EMBEDDED:
        return []
    for info in infos:
        try:
            pix = fitz.Pixmap(doc, info[0])
            if pix.n - pix.alpha > 3:              # CMYK and friends
                pix = fitz.Pixmap(fitz.csRGB, pix)
            im = PilImage.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        except Exception:
            continue
        if max(im.size) >= _OCR_MIN_EMBEDDED_PX:
            out.append(im)
    return out


def _document_pages(path: Path, ai_client, max_pages: int = OCR_MAX_PAGES) -> List:
    """Upright page images for OCR. First page only when max_pages is 1."""
    from PIL import ImageOps
    pages = []
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            import fitz
            doc = fitz.open(str(path))
            for i in range(min(len(doc), max_pages)):
                embedded = _pdf_page_images(doc, i)
                if embedded:
                    pages.extend(embedded)
                else:
                    raw = doc[i].get_pixmap(dpi=OCR_DPI).tobytes("png")
                    pages.append(PilImage.open(io.BytesIO(raw)))
            doc.close()
        elif ext in IMAGE_EXTS:
            pages.append(ImageOps.exif_transpose(PilImage.open(path)))
        else:
            return []
    except Exception as e:
        print(f"    [ocr] cannot render {path.name}: {e}")
        return []
    if not pages:
        return []
    # One orientation decision for the whole document, taken on its first page.
    rot = _upright_rotation(path, pages[0], ai_client)
    if rot:
        pages = [im.rotate(rot, expand=True) for im in pages]
    return pages


def ocr_document_text(path: Path, ai_client=None, first_page_only: bool = False) -> str:
    """
    OCR text for a scan or photo, cached by file content so a second run is free.
    Returns "" when OCR is unavailable or the page yields nothing worth reading.
    """
    if not ocr_available():
        return ""
    try:
        key = f"{file_md5(path)}:{'p1' if first_page_only else 'all'}:{OCR_LANGS}"
    except (OSError, PermissionError):
        return ""
    cached = _ocr_cache_get(key)
    if cached is not None:
        return cached
    pages = _document_pages(path, ai_client, max_pages=1 if first_page_only else OCR_MAX_PAGES)
    text = "\n".join(_ocr_read(im) for im in pages).strip()
    if len(text) < _OCR_MIN_CHARS:
        text = ""
    else:
        print(f"    [ocr] read {path.name} ({len(text)} chars)")
    _ocr_cache_put(key, text)
    return text


def document_text(path: Path, ai_client=None) -> str:
    """A document's text: its real layer when it has one, otherwise OCR."""
    text = _extract_pdf_text(path)
    return text if text.strip() else ocr_document_text(path, ai_client)


def document_first_page_text(path: Path, ai_client=None) -> str:
    """First page text: real layer when there is one, otherwise OCR of page 1."""
    text = _extract_pdf_first_page(path)
    return text if text.strip() else ocr_document_text(path, ai_client, first_page_only=True)


def _vision_image(path: Path, ai_client, dpi: int = 120) -> Tuple[Optional[str], Optional[str]]:
    """First page of a document as base64 for a vision call, stood upright first.
    Returns (base64, media_type), or (None, None) if it cannot be read."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            import fitz
            doc = fitz.open(str(path))
            pix = doc[0].get_pixmap(dpi=dpi)
            raw = pix.tobytes("jpeg")
            doc.close()
            media_type = "image/jpeg"
        elif ext in IMAGE_EXTS:
            media_type = "image/png" if ext == ".png" else "image/jpeg"
            with open(path, "rb") as f:
                raw = f.read()
        else:
            return None, None
    except Exception as e:
        print(f"    Read error for {path.name}: {e}")
        return None, None

    if not HAS_PIL:
        return base64.standard_b64encode(raw).decode(), media_type
    try:
        from PIL import ImageOps
        im = PilImage.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)      # honour a camera's own orientation tag
        rot = _upright_rotation(path, im, ai_client)
        if rot == 0:
            return base64.standard_b64encode(raw).decode(), media_type
        return _encode_image(im.rotate(rot, expand=True)), "image/jpeg"
    except Exception as e:
        print(f"    [orient] {path.name}: {e}")
        return base64.standard_b64encode(raw).decode(), media_type


def categorize_by_vision(path: Path, client) -> Optional[str]:
    """Ask Claude Haiku to classify a document by its first page (image or PDF)."""
    b64, media_type = _vision_image(path, client)
    if not b64:
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
        if item.name.lower() in SKIP_SUBFOLDER_NAMES:
            continue  # fixed mortgage-application folder — never touch

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
# Imóvel, Proposta Crédito and RGPD are always shared, so left at client root.
_PERSONAL_CATEGORIES = {
    "Documentos Pessoais", "Rendimentos", "Extratos Bancários",
    "Património", "IRS", "Mapa CRC",
}


NIF_RE = re.compile(r'\b\d{9}\b')

IDENTITY_PROMPT = (
    "This is a page from a Portuguese mortgage dossier document. "
    "Identify the person this document is ABOUT — the account holder, taxpayer, employee, "
    "tenant/landlord, or named subject — NOT any clerk, official, or bank employee who merely "
    "signs or issues it.\n"
    "Reply with exactly two lines:\n"
    "Name: <full name as printed, or unknown>\n"
    "NIF: <9-digit Portuguese tax number as printed, or unknown>"
)


def _extract_pdf_text(path: Path) -> str:
    """Text layer from all pages of a born-digital PDF. Empty string for scans/images."""
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        import fitz
        doc = fitz.open(str(path))
        pages = [doc[i].get_text() for i in range(len(doc))]
        doc.close()
        return "\n".join(pages)
    except Exception:
        return ""


def _find_nifs(text: str) -> set:
    """All 9-digit candidate NIFs in text. False positives are harmless — callers only
    act on exact matches against a person's own already-confirmed NIF."""
    return set(NIF_RE.findall(text)) if text else set()


# An individual Portuguese NIF starts with 1, 2 or 3; 5xx belongs to a company and
# 6xx-9xx to other entities. Filtering on that keeps an employer's, a landlord's or
# a bank's tax number out of an applicant's identity registry.
PERSONAL_NIF_RE = re.compile(r'\b[123]\d{8}\b')

# How far from a person's printed name a number still counts as *their* NIF.
_NIF_NAME_WINDOW = 160

# How far apart the two applicants' names can be printed and still count as a joint
# identification block rather than two unrelated mentions on the same page.
_JOINT_NAME_WINDOW = 400

# A currency symbol or a two-decimal amount beside the names. Statements print the
# holders in a plain addressee block, so money next to the pair means the match came
# from the transaction table instead — a transfer between the two, not co-ownership.
_MONEY_RE = re.compile(r'[€£$]|\d[.,]\d{2}(?!\d)')

# Returned instead of a person name when a document belongs to BOTH applicants.
SHARED_OWNER = "__SHARED__"

# Categories where a single document can legitimately belong to both applicants: the
# couple's one IRS declaration and liquidation note, and statements for accounts and
# holdings they own together. A payslip, an employment contract, a Mapa CRC and an ID
# card are the property of one person whatever else the page happens to mention.
_JOINTABLE_CATEGORIES = {"IRS", "Extratos Bancários", "Património"}


def _find_nifs_near_name(text: str, person_name: str) -> set:
    """Personal NIFs printed within a short window of this person's own name."""
    if not text:
        return set()
    norm = normalize_stem(text)
    found: set = set()
    for pat in _name_patterns(normalize_stem(person_name)):
        for m in pat.finditer(norm):
            seg = norm[max(0, m.start() - _NIF_NAME_WINDOW): m.end() + _NIF_NAME_WINDOW]
            found |= set(PERSONAL_NIF_RE.findall(seg))
    return found


def _names_printed_together(text: str, person_names: List[str]) -> bool:
    """
    True when two applicants' names are printed close to one another — the joint
    identification block of an IRS Modelo 3 (Quadro 3/5) or the two-holder header
    of a shared account.

    Callers pass the FIRST PAGE only, and that is what makes this safe. A statement's
    transaction table is full of transfers naming the spouse ("TRF P/ VANESSA"), and
    two such lines a few rows apart would otherwise read as a joint holder block.
    Who a document belongs to is declared on its first page, never in its tables.
    """
    if not text or len(person_names) < 2:
        return False
    from itertools import combinations
    norm = normalize_stem(text)
    positions: Dict[str, List[int]] = {}
    for name in person_names:
        hits = sorted(m.start() for pat in _name_patterns(normalize_stem(name))
                      for m in pat.finditer(norm))
        if hits:
            positions[name] = hits
    if len(positions) < 2:
        return False
    for a, b in combinations(positions, 2):
        for pa in positions[a]:
            for pb in positions[b]:
                if abs(pb - pa) > _JOINT_NAME_WINDOW:
                    continue
                lo, hi = min(pa, pb), max(pa, pb)
                if _MONEY_RE.search(norm[max(0, lo - 60):hi + 20]):
                    continue  # a transfer line ("ref: to Bruno & Elzbieta €3,094.85")
                return True
    return False


CLIENT_CONFIG_NAME = ".kova.json"


def load_client_config(client_folder: Path) -> Dict:
    """
    Optional per-client overrides, read from .kova.json at the client root.

    Portuguese banks address a statement to the first holder alone, so nothing
    printed on it says the account is held jointly. That one fact cannot be read
    out of the documents and has to be stated once, here:

        {"joint_accounts": ["PT50001800034342285602004", "0544.000251.100"],
         "joint_files": ["ReciboVerde_2026-01_2026-08.png"]}

    joint_accounts  IBANs or account numbers whose statements belong to both
                    applicants; matched against the document text, ignoring
                    spaces, dots and dashes.
    joint_files     exact filenames to treat as joint, for one-off documents
                    (including scans with no text layer).
    """
    path = client_folder / CLIENT_CONFIG_NAME
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        print(f"    [config] ignoring unreadable {path.name}: {e}")
        return {}


def _flatten_account(value: str) -> str:
    return re.sub(r"[\s.\-]", "", str(value)).lower()


def _config_says_joint(path: Path, text: str, config: Dict) -> bool:
    """Joint ownership declared in .kova.json rather than readable from the page."""
    if not config:
        return False
    if path.name in set(config.get("joint_files") or []):
        return True
    accounts = [_flatten_account(a) for a in (config.get("joint_accounts") or [])]
    if not accounts or not text:
        return False
    flat = _flatten_account(text)
    return any(a and a in flat for a in accounts)


def is_joint_document(path: Path, person_names: List[str],
                      nif_registry: Dict[str, set],
                      config: Optional[Dict] = None) -> bool:
    """
    True when the document belongs to BOTH applicants: a married couple's single
    IRS declaration, its liquidation note, or a statement for an account they hold
    together. Such a document belongs to the application as a whole and lives in
    the client-root standard folder, not in one person's.
    """
    if len(person_names) < 2:
        return False
    text = document_text(path)
    if _config_says_joint(path, text, config or {}):
        return True
    if not text:
        return False
    first_page = document_first_page_text(path)
    if _names_printed_together(first_page, person_names):
        return True
    known = {p: nifs for p, nifs in nif_registry.items() if nifs}
    if len(known) >= 2:
        page_nifs = _find_nifs(first_page)
        if sum(1 for nifs in known.values() if page_nifs & nifs) >= 2:
            return True
    return False


# A landlord with several let properties gets a rent receipt a month for each, and
# they are told apart only by the property. Receipts for the ground floor and for the
# annex of one building, named by month alone, read as two months of the same tenancy.
_STREET_RE = re.compile(
    r"\b(?:rua|avenida|av|travessa|praceta|praca|largo|estrada|beco|calcada|"
    r"azinhaga|quinta|urbanizacao|bairro|alameda|caminho)\b[\s.]+(.{3,60})",
    re.IGNORECASE)

# Words that carry no distinguishing weight in a street name.
_STREET_STOPWORDS = {"do", "da", "de", "dos", "das", "e"}

# How the part of a building that was let is written on these forms.
_PROPERTY_PART_RE = re.compile(
    r"\b(res\s*do\s*chao|r/c|rcesq|rcdto|anexo|cave|sotao|aguas furtadas|loja|"
    r"garagem|arrecadacao|armazem|\d\s*(?:esq|dto|dir|frente|tras|frt))\b",
    re.IGNORECASE)


def _camel(text: str) -> str:
    """Filename-safe CamelCase of a few words."""
    parts = [w for w in re.split(r"[^0-9a-z]+", normalize_stem(text)) if w]
    return "".join(w.capitalize() for w in parts)


# Only the two documents that are themselves about one tenancy, matched on their own
# headings. A passing mention is not enough: "contrato de arrendamento" turns up in
# the IRS Anexo F, in bank forms and in dossier summaries, and treating those as rent
# documents gave them property names taken from whatever address they happened to
# quote. The word "anexo" alone is worse still, being how every Portuguese form
# labels its attachments and its schedules.
_RENT_DOC_RE = re.compile(
    r"recibo de renda eletronico|comunicacao de contratos? arrendamento",
    re.IGNORECASE)


# The filename prefixes that denote a document about one tenancy.
_RENT_NAME_RE = re.compile(r"^(ReciboRenda|RecRenda|RegistoArrendamento)_", re.IGNORECASE)


def _is_rent_document(text: str) -> bool:
    """True for a rent receipt or a lease registration, where the property matters."""
    return bool(text) and bool(_RENT_DOC_RE.search(normalize_stem(text)))


def _rent_property_tag(text: str) -> Optional[str]:
    """
    A short label for the property a rent document concerns, from its street name
    and the part of the building let: 'Morangueiros_ResChao'. None when the document
    does not say, in which case naming carries on without it rather than guessing.
    """
    if not text:
        return None
    norm = normalize_stem(text)

    street = None
    m = _STREET_RE.search(norm)
    if m:
        # Keep the distinctive words of the name, stopping at the house number.
        words = []
        for w in re.split(r"[^0-9a-z]+", m.group(1)):
            if not w or w in _STREET_STOPWORDS:
                continue
            if w.isdigit() or w in {"n", "no", "numero"}:
                break
            words.append(w)
            if len(words) == 3:
                break
        if words:
            street = _camel(" ".join(words))

    part = None
    labelled = re.search(r"parte arrendada[^\n]*\n?\s*([^\n]{1,40})", text, re.IGNORECASE)
    if labelled:
        candidate = labelled.group(1).strip()
        if candidate and ":" not in candidate:
            part = _camel(candidate)
    if not part:
        found = _PROPERTY_PART_RE.search(norm)
        if found:
            part = _camel(found.group(1))

    tag = "_".join(x for x in (street, part) if x)
    return tag or None


def _insert_property_tag(stem: str, tag: str, person: Optional[str]) -> str:
    """Put the property into a filename, keeping any person suffix last."""
    if not tag or normalize_stem(tag) in normalize_stem(stem):
        return stem
    parts = stem.split("_")
    if person and parts and normalize_stem(parts[-1]) == normalize_stem(person):
        return "_".join(parts[:-1] + [tag, parts[-1]])
    return f"{stem}_{tag}"


# Names that pass the convention check but are still missing something. A standard
# prefix is normally reason enough to leave a filename alone, which is why a rent
# receipt named by month only was never revisited to gain its property.
_PLACEHOLDER_TOKENS = {"unknown", "desconhecido", "na", "none"}


def _name_needs_revisit(path: Path, category: str) -> bool:
    """Whether an otherwise-conventional filename is still worth regenerating."""
    stem_tokens = {t.lower() for t in path.stem.split("_") if t}
    if stem_tokens & _PLACEHOLDER_TOKENS:
        return True
    if category != "Rendimentos" or not _RENT_NAME_RE.match(path.stem):
        return False
    text = document_text(path)
    if not _is_rent_document(text):
        return False
    tag = _rent_property_tag(text)
    return bool(tag) and normalize_stem(tag) not in normalize_stem(path.stem)


def _strip_person_suffix(stem: str, person_names: List[str]) -> str:
    """Drop trailing _Person parts from the name of a file that turned out to be joint."""
    pats = [pat for name in person_names for pat in _name_patterns(normalize_stem(name))]
    parts = stem.split("_")
    while len(parts) > 1:
        last = normalize_stem(parts[-1])
        if last and any(pat.fullmatch(last) for pat in pats):
            parts.pop()
        else:
            break
    return "_".join(parts)


def _recategorize(f: Path, current_category: str) -> str:
    """Category a file should be in, judged from its text first and its name second."""
    return (categorize_by_content(_extract_pdf_text(f), _extract_pdf_first_page(f))
            or categorize_by_name(f.stem)
            or current_category)


def _match_person_in_text(text: str, person_names: List[str]) -> Optional[str]:
    """Search free-form document text for exactly one of the given person names."""
    if not text:
        return None
    norm_text = normalize_stem(text)
    hits = []
    for name in person_names:
        patterns = _name_patterns(normalize_stem(name))
        if any(pat.search(norm_text) for pat in patterns):
            hits.append(name)
    hits = list(dict.fromkeys(hits))
    return hits[0] if len(hits) == 1 else None


def _vision_identity(path: Path, ai_client) -> Tuple[Optional[str], Optional[str]]:
    """Ask Claude to transcribe the document subject's name + NIF (not pick a person —
    transcription is grounded in what's printed, far more reliable than a guess)."""
    b64, media_type = _vision_image(path, ai_client)
    if not b64:
        return None, None

    try:
        resp = ai_client.messages.create(
            model=VISION_MODEL,
            max_tokens=60,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": IDENTITY_PROMPT},
                ],
            }],
        )
        raw = resp.content[0].text.strip()
        name = nif = None
        for line in raw.splitlines():
            line = line.strip()
            low = line.lower()
            if low.startswith("name:"):
                v = line.split(":", 1)[1].strip()
                name = v if v and v.lower() != "unknown" else None
            elif low.startswith("nif:"):
                v = line.split(":", 1)[1].strip()
                m = re.search(r'\d{9}', v)
                nif = m.group(0) if m else None
        return name, nif
    except Exception as e:
        print(f"    Person-vision error ({path.name}): {e}")
        return None, None


def build_nif_registry(subclients: List[Path], ai_client) -> Dict[str, set]:
    """
    Work out each sub-client's own tax number from the documents already filed under
    their name. This is the ground truth used later both to attribute an ambiguous
    document to one applicant and to recognise a document that belongs to both.

    Every readable document counts, and a candidate only qualifies if it is printed
    next to that person's name. The applicant's own number then appears on nearly all
    of their documents, while a landlord's, an employer's or a bank client number
    appears on one or two — so the most frequent candidate wins. Precision matters
    here: a polluted registry would make unrelated documents look jointly owned.
    """
    registry: Dict[str, set] = {sc.name: set() for sc in subclients}
    if len(subclients) < 2:
        return registry

    scan_order = ["Documentos Pessoais", "Mapa CRC", "IRS", "Rendimentos",
                  "Extratos Bancários", "Património"]
    for sc in subclients:
        counts: Dict[str, int] = defaultdict(int)
        unreadable: List[Path] = []
        for std in scan_order:
            folder = sc / std
            if not folder.exists():
                continue
            for f in sorted(folder.iterdir()):
                if not f.is_file() or f.suffix.lower() in SKIP_EXTENSIONS:
                    continue
                text = document_text(f, ai_client)
                if not text.strip():
                    if f.suffix.lower() in IMAGE_EXTS | {".pdf"}:
                        unreadable.append(f)
                    continue
                for nif in _find_nifs_near_name(text, sc.name):
                    counts[nif] += 1
        if counts:
            top = max(counts.values())
            registry[sc.name] = {n for n, c in counts.items() if c == top}
            continue
        # Every document this person has is a scan or a photo — fall back to vision,
        # capped at a few calls, and stop at the first number it manages to read.
        if ai_client:
            for f in unreadable[:3]:
                _, nif = _vision_identity(f, ai_client)
                if nif:
                    registry[sc.name] = {nif}
                    break

    # A number that came out top for two different applicants identifies neither —
    # usually a shared employer or a bank's own tax number. Dropping it matters most
    # for the joint check, which would otherwise read every document as jointly owned.
    shared = {n for a in registry for b in registry if a != b
              for n in registry[a] & registry[b]}
    for name in registry:
        registry[name] -= shared
    return registry


def identify_person_in_document(
    path: Path, person_names: List[str], nif_registry: Dict[str, set], ai_client,
    config: Optional[Dict] = None, category: Optional[str] = None
) -> Optional[str]:
    """
    Determine which person a document belongs to by reading its actual content —
    full name and/or NIF — and cross-referencing against the known registry.
    Text layer is tried first (free, reliable); vision is only a fallback for scans.
    Returns SHARED_OWNER for a document that belongs to both applicants.
    """
    text = document_text(path, ai_client)

    if _config_says_joint(path, text, config or {}):
        return SHARED_OWNER

    # Both applicants named together on page 1 = a joint document; it stays at the
    # client root. This has to come before the vision fallback below, which
    # transcribes a single subject and would hand the couple's document to one of them.
    first_page = document_first_page_text(path, ai_client)
    if _names_printed_together(first_page, person_names):
        return SHARED_OWNER

    person = _match_person_in_text(text, person_names)
    if person:
        return person

    text_nifs = _find_nifs(text)
    page_nifs = _find_nifs(first_page)
    hits = [p for p, nifs in nif_registry.items() if nifs and (text_nifs & nifs)]
    page_hits = [p for p, nifs in nif_registry.items() if nifs and (page_nifs & nifs)]
    if len(page_hits) >= 2:
        return SHARED_OWNER
    if len(hits) == 1:
        # One applicant's number on an IRS return or an account statement only proves
        # sole ownership if we know the other applicant's number and it is absent.
        # Guessing here is exactly how a couple's joint declaration ends up filed
        # under whichever of them we happened to identify first.
        if category in _JOINTABLE_CATEGORIES and not all(nif_registry.get(n) for n in person_names):
            return None
        return hits[0]

    if not ai_client:
        return None

    name, nif = _vision_identity(path, ai_client)
    if name:
        person = _match_person_in_text(name, person_names)
        if person:
            return person
    if nif:
        hits = [p for p, nifs in nif_registry.items() if nif in nifs]
        if len(hits) == 1:
            return hits[0]
    return None


def find_joint_docs_in_subclients(
    client_folder: Path, subclients: List[Path], nif_registry: Dict[str, set],
    config: Optional[Dict] = None
) -> List[Dict]:
    """
    Pull documents that belong to both applicants out of a single applicant's folder.
    A married couple files one IRS Modelo 3 and receives one nota de liquidacao for
    the two of them; a jointly held account produces one statement. Whichever person
    an earlier pass happened to pick, the document belongs to the application as a
    whole, so it moves to the client-root standard folder — with its category
    re-checked on the way out and the now-wrong _Person suffix dropped.
    """
    if len(subclients) < 2:
        return []
    moves = []
    person_names = [sc.name for sc in subclients]
    for sc in subclients:
        for std in STANDARD_FOLDERS:
            if std not in _JOINTABLE_CATEGORIES:
                continue
            folder = sc / std
            if not folder.exists():
                continue
            for f in folder.iterdir():
                if not f.is_file() or f.suffix.lower() in SKIP_EXTENSIONS:
                    continue
                if not is_joint_document(f, person_names, nif_registry, config):
                    continue
                category = _recategorize(f, std)
                new_stem = _strip_person_suffix(f.stem, person_names)
                target = client_folder / category / (new_stem + f.suffix)
                if target.resolve() != f.resolve():
                    moves.append({"from": str(f), "to": str(target)})
    return moves


def find_misrouted_files(
    client_folder: Path,
    subclients: List[Path],
    use_vision: bool = False,
    ai_client=None,
    nif_registry: Optional[Dict[str, set]] = None,
    config: Optional[Dict] = None,
) -> List[Dict]:
    """
    Find files sitting in root standard folders that belong to a specific sub-client.
    1. Try filename pattern matching (fast, free).
    2. If no match, read the document's actual text/NIF and cross-reference against
       the sub-clients' known identity (name appearing in content, or NIF match).
    3. If still unresolved and vision is enabled, render the page and transcribe it.
    Files in shared categories (Imóvel, Proposta Crédito) are never reassigned.
    """
    moves = []
    person_names = [sc.name for sc in subclients]
    if nif_registry is None:
        nif_registry = build_nif_registry(subclients, ai_client if use_vision else None)

    for std in STANDARD_FOLDERS:
        if std in _SHARED_ONLY_CATEGORIES:  # always shared — never re-route to a sub-client
            continue
        std_folder = client_folder / std
        if not std_folder.exists():
            continue
        for f in std_folder.iterdir():
            if not f.is_file() or f.suffix.lower() in SKIP_EXTENSIONS:
                continue

            # 0. Joint documents belong to the application, not to one applicant —
            # and their filename often still carries a stale _Person suffix from an
            # earlier run, so this has to be checked before the filename match.
            if (len(subclients) >= 2 and std in _JOINTABLE_CATEGORIES
                    and is_joint_document(f, person_names, nif_registry, config)):
                continue

            stem_norm = normalize_stem(f.stem)

            # 1. Filename match
            matched = None
            for subclient in subclients:
                sc_name = normalize_stem(subclient.name)
                if any(pat.search(stem_norm) for pat in _name_patterns(sc_name)):
                    matched = subclient
                    break

            # 2. Content/NIF cross-reference — only for personal categories
            if matched is None and len(subclients) >= 2 and std in _PERSONAL_CATEGORIES:
                if f.suffix.lower() in IMAGE_EXTS | {".pdf"}:
                    person_name = identify_person_in_document(
                        f, person_names, nif_registry,
                        ai_client if use_vision else None, config, std
                    )
                    if person_name and person_name != SHARED_OWNER:
                        print(f"    [identify] {f.name}  ->  {person_name}")
                        matched = next((sc for sc in subclients if sc.name == person_name), None)

            if matched is not None:
                target = matched / std / f.name
                if target.resolve() != f.resolve():
                    moves.append({"from": str(f), "to": str(target)})
    return moves


def find_miscategorized_files(client_folder: Path, subclients: List[Path]) -> List[Dict]:
    """
    Catch files already filed under the wrong standard folder — e.g. a payslip or a
    freelance avença invoice that got mistakenly classified as IRS at some point.
    Re-derives the category from the filename, then (for born-digital PDFs) from the
    document's own text, and proposes a move only when that disagrees with the folder
    it's currently sitting in. Cheap — no AI calls — so safe to run on every pass.
    """
    moves = []
    for base in [client_folder] + subclients:
        for std in STANDARD_FOLDERS:
            folder = base / std
            if not folder.exists():
                continue
            for f in folder.iterdir():
                if not f.is_file() or f.suffix.lower() in SKIP_EXTENSIONS:
                    continue
                # Content wins over filename here: a filename can be wrong in a way
                # that still happens to match its (wrong) current folder's own rules
                # (e.g. an avença invoice previously misnamed "NotaLiq_IRS_...").
                new_category = _recategorize(f, std)
                if new_category and new_category != std:
                    target = base / new_category / f.name
                    if target.resolve() != f.resolve():
                        moves.append({"from": str(f), "to": str(target)})
    return moves


def find_shared_category_leaks(client_folder: Path, subclients: List[Path]) -> List[Dict]:
    """
    Imóvel / Proposta Crédito / RGPD belong to the mortgage application as a whole,
    never to one applicant — a per-person copy of one of these folders shouldn't
    exist. Move any files found in one back to the shared client root.
    """
    moves = []
    for sc in subclients:
        for std in _SHARED_ONLY_CATEGORIES:
            folder = sc / std
            if not folder.exists():
                continue
            for f in folder.iterdir():
                if f.is_file() and f.suffix.lower() not in SKIP_EXTENSIONS:
                    target = client_folder / std / f.name
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
    new_only: bool = False,
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

    config = load_client_config(client_folder)

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
    person_names = [sc.name for sc in all_subclients]
    nif_registry: Dict[str, set] = {}
    if len(all_subclients) >= 2:
        nif_registry = build_nif_registry(all_subclients, ai_client if use_vision else None)

    for subclient in all_subclients:
        for std in STANDARD_FOLDERS:
            if std in _SHARED_ONLY_CATEGORIES:  # shared — only one copy at client root
                continue
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
        # Scans get an ad-hoc filename (phone camera, "img001_p1.jpg") far more
        # often than a real one — vision runs alongside the filename check, not
        # just when it fails, and wins on disagreement.
        if use_vision and ai_client:
            print(f"    [vision] {images[0].name}  (group: {group_key})")
            vision_category = categorize_by_vision(images[0], ai_client)
            if vision_category:
                category = vision_category
        if category is None:
            category = "Documentos Pessoais"  # safe fallback for image groups

        # Route shared-root personal scans (e.g. a CC photographed as front+back
        # images) to the right sub-client, same as the single-file path below —
        # otherwise a merged ID card always lands at the shared couple root.
        if (category in _PERSONAL_CATEGORIES and base_folder == client_folder
                and len(all_subclients) >= 2):
            person = identify_person_in_document(
                images[0], person_names, nif_registry, ai_client if use_vision else None
            )
            if person:
                print(f"    [identify] {group_key} (merged)  ->  {person}")
                base_folder = next(sc for sc in all_subclients if sc.name == person)

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

        ext = file_path.suffix.lower()
        name_category = categorize_by_name(file_path.stem)

        if ext in IMAGE_EXTS:
            # Images have no text layer to cross-check, and their filename is
            # the least trustworthy of any file type here (phone-camera scans,
            # ad-hoc names). Run vision alongside the filename check rather than
            # only when it fails, and let vision win on disagreement.
            category = name_category
            if use_vision and ai_client:
                print(f"    [vision] {file_path.name}")
                vision_category = categorize_by_vision(file_path, ai_client)
                if vision_category:
                    category = vision_category
            if category is None:
                category = categorize_by_content(
                    ocr_document_text(file_path, ai_client if use_vision else None),
                    document_first_page_text(file_path, ai_client if use_vision else None))
        else:
            pdf_text = _extract_pdf_text(file_path)
            if pdf_text.strip():
                content_category = categorize_by_content(
                    pdf_text, _extract_pdf_first_page(file_path))
                # Content wins over filename: a filename can be wrong (legacy misnamed
                # file, or a real document with a generic/misleading name) in a way
                # that still happens to match a category's filename rule. The content
                # rules only cover a few high-confidence, header-scoped signals (see
                # categorize_by_content), so this can only override into Rendimentos/
                # Documentos Pessoais/Património — never an arbitrary category.
                category = content_category if content_category else name_category

                if category is None and use_vision and ai_client and ext == ".pdf":
                    print(f"    [vision] {file_path.name}")
                    category = categorize_by_vision(file_path, ai_client)
            else:
                # No text layer means this PDF is a scan/photo (e.g. a phone photo of a
                # multi-page deed saved straight to PDF) — exactly as unreliable as a
                # loose image file, since there's no body text to cross-check the
                # filename against. Run vision alongside the filename check rather than
                # only when it fails, and let vision win on disagreement, same as the
                # image-file branch above. Without this, a scanned document whose
                # filename happens to match an existing category prefix (e.g. a house
                # deed mistakenly saved as "DeclaracaoMutuarios.pdf") never gets a
                # vision check at all.
                category = name_category
                if use_vision and ai_client and ext == ".pdf":
                    print(f"    [vision] {file_path.name}")
                    vision_category = categorize_by_vision(file_path, ai_client)
                    if vision_category:
                        category = vision_category
                if category is None:
                    category = categorize_by_content(
                        ocr_document_text(file_path, ai_client if use_vision else None),
                        document_first_page_text(file_path, ai_client if use_vision else None))

        if category is None:
            plan["uncategorized"].append({
                "file": str(file_path),
                "target_base": str(base_folder),
            })
            continue

        # Some categories belong to the application as a whole, never to one
        # applicant, and always live at the client root.
        if category in _SHARED_ONLY_CATEGORIES and base_folder != client_folder:
            base_folder = client_folder

        # Route shared-root personal documents to the right sub-client by reading
        # the actual content (name/NIF), instead of leaving them stuck at the root
        # until a second organizer pass happens to catch them.
        if (category in _PERSONAL_CATEGORIES and base_folder == client_folder
                and len(all_subclients) >= 2):
            person = identify_person_in_document(
                file_path, person_names, nif_registry,
                ai_client if use_vision else None, config, category
            )
            if person == SHARED_OWNER:
                print(f"    [identify] {file_path.name}  ->  both applicants (stays at root)")
            elif person:
                print(f"    [identify] {file_path.name}  ->  {person}")
                base_folder = next(sc for sc in all_subclients if sc.name == person)

        target = base_folder / category / file_path.name
        if target.resolve() != file_path.resolve():
            plan["moves"].append({
                "from": str(file_path),
                "to": str(target),
            })

    # The passes below re-scan every file already filed in a standard folder —
    # skip them in --new-only mode, which only wants to touch newly added loose files.
    if not new_only:
        # The passes below can each have an opinion about the same file. The first
        # one to claim it wins, so they run most-specific first and a file already
        # scheduled to move is never queued a second time.
        planned_sources = {m["from"] for m in plan["moves"]}

        def _queue(new_moves: List[Dict]) -> None:
            for m in new_moves:
                if m["from"] in planned_sources:
                    continue
                planned_sources.add(m["from"])
                plan["moves"].append(m)

        # Return documents that belong to both applicants (the couple's single IRS
        # declaration and liquidation note, a joint account statement) to the client
        # root, correcting their category and dropping the stale _Person suffix
        _queue(find_joint_docs_in_subclients(client_folder, all_subclients,
                                             nif_registry, config))

        # Fix files already filed under the wrong standard folder (e.g. a payslip or
        # avença invoice that was mistakenly classified as IRS in an earlier run)
        _queue(find_miscategorized_files(client_folder, all_subclients))

        # Pull any files out of a per-person Imóvel/Proposta Crédito/RGPD folder —
        # those categories are always shared at the client root
        _queue(find_shared_category_leaks(client_folder, all_subclients))

        # Migrate files from root standard folders to per-sub-client folders
        _queue(find_misrouted_files(client_folder, all_subclients,
                                    use_vision=use_vision, ai_client=ai_client,
                                    nif_registry=nif_registry, config=config))

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
        _plan_renames(plan, client_folder, all_subclients, ai_client, new_only=new_only)

    # ── Empty root personal folders (two-person clients only) ────────────────
    if all_subclients:
        for std in STANDARD_FOLDERS:
            folder = client_folder / std
            if folder.exists() and not any(folder.rglob("*")):
                plan["empty_root_folders"].append(str(folder))
        for sc in all_subclients:
            for std in _SHARED_ONLY_CATEGORIES:
                folder = sc / std
                if folder.exists() and not any(folder.rglob("*")):
                    plan["empty_root_folders"].append(str(folder))

    return plan


def _plan_renames(plan: Dict, client_folder: Path, all_subclients: List[Path], ai_client,
                   new_only: bool = False) -> None:
    """
    Populate plan["renames"] with standardized-name proposals.

    Two passes:
    1. Files already in their final organized location. Skipped in --new-only mode.
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
        if _is_standard_name(source_path.stem, category=category) and not _name_needs_revisit(source_path, category):
            return
        print(f"    [rename] {source_path.name}")
        new_name = generate_standard_name(source_path, category, person, ai_client)
        if new_name:
            plan["renames"].append({
                "path": str(target_path),
                "new_name": new_name,
            })
            already_planned.add(key)

    # Files being relocated this run (misrouted/miscategorized) get renamed at their
    # NEW location in Pass 2 below — skip them here so they aren't also queued with
    # their current (wrong) category/person context, which would waste a vision call.
    moving_away = {Path(m["from"]) for m in plan["moves"]}

    if not new_only:
        # Pass 1: files already organized at root standard folders
        for std in STANDARD_FOLDERS:
            std_folder = client_folder / std
            if not std_folder.exists():
                continue
            for f in std_folder.iterdir():
                if f.is_file() and f.suffix.lower() not in SKIP_EXTENSIONS and f not in moving_away:
                    _add_rename(f, f, std, None)

        # Pass 1b: files already organized inside sub-client standard folders
        for sc in all_subclients:
            for std in STANDARD_FOLDERS:
                std_folder = sc / std
                if not std_folder.exists():
                    continue
                for f in std_folder.iterdir():
                    if f.is_file() and f.suffix.lower() not in SKIP_EXTENSIONS and f not in moving_away:
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


def cleanup_shared_only_subclient_folders(subclients: List[Path]) -> List[str]:
    """
    Remove per-person Imóvel/Proposta Crédito/RGPD folders — these categories belong
    to the application as a whole and should only ever exist at the client root.
    Any files in them were already relocated by find_shared_category_leaks.
    """
    removed = []
    for sc in subclients:
        for std in _SHARED_ONLY_CATEGORIES:
            folder = sc / std
            if folder.exists() and not any(folder.rglob("*")):
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

def apply_plan(plans: List[Dict], use_vision: bool = False, ai_client=None, new_only: bool = False) -> None:
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
        # Re-scans every root standard folder, so skip it in --new-only mode.
        subclient_paths = _get_subclient_folders(Path(p["client_folder"]))
        extra_parsed = _parse_person_names(p["client"])
        all_sc = subclient_paths + [
            Path(p["client_folder"]) / name
            for name in extra_parsed
            if name.lower() not in {sc.name.lower() for sc in subclient_paths}
        ]
        if all_sc and not new_only:
            client_config = load_client_config(Path(p["client_folder"]))
            for extra_move in find_misrouted_files(Path(p["client_folder"]), all_sc,
                                                     use_vision=use_vision, ai_client=ai_client,
                                                     config=client_config):
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

        # Per-person Imóvel/Proposta Crédito/RGPD folders should never exist
        removed_leaks = cleanup_shared_only_subclient_folders(all_sc)
        for r in removed_leaks:
            try:
                rel = Path(r).relative_to(BASE_DIR)
            except ValueError:
                rel = Path(r)
            print(f"  rmdir (shared-only leak)  {rel}")
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
    parser.add_argument("--new-only",                action="store_true", help="Only categorize/move newly added loose files — skip re-scanning already-organized folders")
    parser.add_argument("--skip-vision",             action="store_true", help="Skip AI vision for unrecognised images")
    parser.add_argument("--no-ocr",                  action="store_true", help="Do not OCR scans even if Tesseract is installed")
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
        if item.name.startswith("_"):
            continue  # _novo_* pending folders, __pycache__, _claude_review, etc.
        if args.client and item.name.lower() != args.client.lower():
            continue
        client_folders.append(item)

    if not client_folders:
        sys.exit(f"No matching client folders found under {BASE_DIR}")

    mode = "APPLYING" if args.apply else "DRY-RUN"
    scope = " (new files only)" if args.new_only else ""
    print(f"\n[{mode}]{scope} Scanning {len(client_folders)} client folder(s)...")
    if use_vision:
        print(f"Vision: enabled (model: {VISION_MODEL})")
    else:
        print("Vision: disabled")

    if args.no_ocr:
        disable_ocr()
    if ocr_available():
        print(f"OCR: enabled ({OCR_LANGS})")
    elif args.no_ocr:
        print("OCR: disabled (--no-ocr)")
    elif not HAS_PYTESSERACT:
        print("OCR: unavailable (pip install pytesseract, plus the Tesseract program)")
    else:
        print("OCR: unavailable (Tesseract program not found)")

    plans: List[Dict] = []
    for folder in client_folders:
        print(f"  {folder.name}")
        plans.append(scan_client(folder, use_vision=use_vision, ai_client=ai_client, new_only=args.new_only))

    # Save JSON plan
    plan_path = BASE_DIR / "organize_plan.json"
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plans, f, ensure_ascii=False, indent=2)
    print(f"\nPlan saved -> {plan_path}")

    if args.claude_mode:
        generate_claude_review(plans)
    elif args.apply:
        apply_plan(plans, use_vision=use_vision, ai_client=ai_client, new_only=args.new_only)
    else:
        print_plan(plans)


if __name__ == "__main__":
    main()
