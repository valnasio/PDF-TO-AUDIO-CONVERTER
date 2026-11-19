"""Extração e preparação do texto do PDF para síntese de voz.

Objetivos:
- Não cortar palavras: hifenização de fim de linha é desfeita, quebras de
  linha dentro de parágrafos viram espaço e parágrafos que continuam na
  página seguinte são unidos.
- Não perder texto: o documento é processado por inteiro e dividido em
  trechos apenas em fronteiras de parágrafo/frase (nunca no meio de palavra).
- Remover ruído que deixaria a leitura estranha: cabeçalhos/rodapés
  repetidos, números de página, caracteres de controle.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable

import pymupdf

# Pronomes/partículas que, em português, seguem um hífen legítimo
# (ex.: "mover-se", "disse-lhe"). Nesses casos o hífen de fim de linha é mantido.
_CLITICS = {
    "se", "me", "te", "lhe", "lhes", "nos", "vos", "o", "a", "os", "as",
    "lo", "la", "los", "las", "no", "na", "nas", "ia", "ias", "iam", "ei",
    "á", "ás", "ão", "emos", "eis", "ão", "ia", "íamos",
}

_SENTENCE_END = tuple('.!?…:;"”»)')
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏﻿]")
_HYPHEN_BREAK_RE = re.compile(r"(\w)[-‐‑]\s*\n\s*(\w+)")
_WS_RE = re.compile(r"\s+")
_PAGE_NUMBER_RE = re.compile(
    r"^\s*(p[áa]g(ina)?\.?\s*)?\d{1,5}(\s*(/|de)\s*\d{1,5})?\s*$", re.IGNORECASE
)
_ROMAN_RE = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)


@dataclass
class Paragraph:
    page: int  # página (1-based) onde o parágrafo começa
    text: str
    heading: bool = False  # linha curta sem pontuação (título/capítulo)
    # (posição no texto, página) onde o parágrafo passa para a página seguinte
    breaks: list[tuple[int, int]] = field(default_factory=list)

    @property
    def last_page(self) -> int:
        return self.breaks[-1][1] if self.breaks else self.page

    def page_at(self, offset: int) -> int:
        page = self.page
        for pos, pg in self.breaks:
            if pos <= offset:
                page = pg
        return page


@dataclass
class Chunk:
    index: int
    page: int
    text: str


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)  # desfaz ligaduras (ﬁ, ﬂ...)
    text = text.replace("­", "")  # hífen "suave"
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\t", " ")
    return text


def _join_hyphenated(text: str) -> str:
    def repl(m: re.Match) -> str:
        left, right = m.group(1), m.group(2)
        if right[0].isupper() or right.lower() in _CLITICS:
            return f"{left}-{right}"
        return f"{left}{right}"

    return _HYPHEN_BREAK_RE.sub(repl, text)


def _clean_block(text: str) -> str:
    text = _join_hyphenated(_normalize(text))
    text = text.replace("\n", " ")
    text = re.sub(r"[•▪●◦■□►▶]\s*", "", text)
    return _WS_RE.sub(" ", text).strip()


def _signature(text: str) -> str:
    """Assinatura usada para detectar cabeçalhos/rodapés repetidos."""
    return re.sub(r"\d+", "#", text.lower()).strip()


def _is_noise(text: str) -> bool:
    if not text:
        return True
    if _PAGE_NUMBER_RE.match(text):
        return True
    # só pontuação/símbolos
    return not any(ch.isalnum() for ch in text)


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith(_SENTENCE_END)


def _looks_like_heading(text: str) -> bool:
    return (
        len(text) <= 80
        and not _ends_sentence(text)
        and not text.startswith(("-", "–", "—"))  # fala de diálogo
        and not any(ch in text for ch in "?!")
        and (text[0].isupper() or text[0].isdigit())
    )


def extract_paragraphs(
    pdf_path: str, progress: Callable[[int, int], None] | None = None
) -> tuple[list[Paragraph], int]:
    """Extrai os parágrafos do PDF inteiro, página a página (memória constante por página)."""
    doc = pymupdf.open(pdf_path)
    total = doc.page_count

    # 1ª passada: descobre cabeçalhos/rodapés repetidos (topo/base da página).
    margin_counter: Counter[str] = Counter()
    page_blocks: list[list[tuple[str, bool]]] = []
    for i, page in enumerate(doc):
        height = page.rect.height or 1
        blocks = []
        for b in page.get_text("blocks", sort=True):
            if b[6] != 0:  # bloco de imagem
                continue
            text = _clean_block(b[4])
            if not text:
                continue
            in_margin = b[3] < height * 0.09 or b[1] > height * 0.91
            if in_margin:
                margin_counter[_signature(text)] += 1
            blocks.append((text, in_margin))
        page_blocks.append(blocks)
        if progress:
            progress(i + 1, total)
    doc.close()

    min_repeat = max(3, int(total * 0.3))
    repeated = {sig for sig, n in margin_counter.items() if n >= min_repeat}

    # 2ª passada: monta parágrafos, unindo os que continuam na página seguinte.
    paragraphs: list[Paragraph] = []
    for page_no, blocks in enumerate(page_blocks, start=1):
        for text, in_margin in blocks:
            if _is_noise(text):
                continue
            if in_margin and _signature(text) in repeated:
                continue
            prev = paragraphs[-1] if paragraphs else None
            continues = (
                prev is not None
                and not _ends_sentence(prev.text)
                and not prev.heading
                and (text[0].islower() or text[0].isdigit() or prev.last_page != page_no)
                and not _ROMAN_RE.match(text)
            )
            if continues:
                if page_no != prev.last_page:
                    prev.breaks.append((len(prev.text) + 1, page_no))
                prev.text = f"{prev.text} {text}"
            else:
                paragraphs.append(Paragraph(page=page_no, text=text, heading=_looks_like_heading(text)))

    # Títulos e linhas soltas sem pontuação recebem ponto final para que a voz
    # faça uma pausa natural em vez de emendar com a frase seguinte.
    for p in paragraphs:
        if not _ends_sentence(p.text):
            p.text += "."
    return paragraphs, total


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])[\"”»)]*\s+(?=\S)")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;:—])\s+")


def _split_long(text: str, limit: int) -> list[str]:
    """Divide um texto longo em partes <= limit sem nunca cortar palavras."""
    if len(text) <= limit:
        return [text]
    for splitter in (_SENTENCE_SPLIT_RE, _CLAUSE_SPLIT_RE):
        parts = [p for p in splitter.split(text) if p.strip()]
        if len(parts) > 1:
            out: list[str] = []
            for part in parts:
                out.extend(_split_long(part.strip(), limit))
            return _merge(out, limit)
    # último recurso: quebra em espaços
    words = text.split(" ")
    out, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > limit:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        out.append(cur)
    return out


def _merge(parts: Iterable[str], limit: int) -> list[str]:
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) + 1 + len(p) <= limit:
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


def _sentences_with_offsets(text: str) -> list[tuple[int, str]]:
    """Divide em frases devolvendo (posição inicial, frase)."""
    out, pos = [], 0
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        piece = text[pos:m.end()].strip()
        if piece:
            out.append((pos, piece))
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        out.append((pos, tail))
    return out


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0]
    return text or "sem-titulo"


def _page_names(paragraphs: list[Paragraph], toc: list, first_words: dict[int, str]) -> Callable[[int], str]:
    """Nome de cada página: capítulo do sumário do PDF; senão o último título
    encontrado no texto; senão as primeiras palavras da página."""
    toc_entries = sorted(
        (entry[2], entry[1].strip()) for entry in toc
        if len(entry) >= 3 and entry[1] and entry[1].strip() and entry[2] > 0
    )
    headings = [(p.page, p.text.rstrip(".").strip()) for p in paragraphs if p.heading]

    def name(page: int) -> str:
        current = None
        for pg, title in (toc_entries or headings):
            # vale o título em vigor no início da página
            if pg < page or (pg == page and (toc_entries or first_words.get(page, "").startswith(title[:20]))):
                current = title
            elif pg > page:
                break
        if current and _ROMAN_RE.match(current):
            current = f"capitulo {current}"
        return slugify(current or first_words.get(page, ""))

    return name


def build_pages(
    paragraphs: list[Paragraph], total_pages: int, toc: list | None = None,
    target: int = 1000, limit: int = 1600,
) -> tuple[list[dict], list[int]]:
    """Organiza o texto por página para gerar um áudio por página.

    Cada frase fica inteira na página em que começa (nunca é cortada na
    virada de página). Cada página é dividida em trechos de até `limit`
    caracteres, sempre em fronteiras de frase.

    Retorna (páginas, páginas_sem_texto). Cada página:
        {"page": n, "pages": [n, ...], "name": "slug", "chunks": [{"index", "page", "text"}]}
    "pages" inclui páginas seguintes cujo texto é só a continuação de uma frase.
    """
    by_page: dict[int, list[tuple[str, bool]]] = {}
    pages_with_text: set[int] = set()
    first_words: dict[int, str] = {}
    for p in paragraphs:
        pages_with_text.update(range(p.page, p.last_page + 1))
        for i, (offset, sentence) in enumerate(_sentences_with_offsets(p.text)):
            page = p.page_at(offset)
            by_page.setdefault(page, []).append((sentence, i == 0))
            first_words.setdefault(page, " ".join(sentence.split()[:6]))

    name_of = _page_names(paragraphs, toc or [], first_words)
    units: list[dict] = []
    index = 0
    ordered = sorted(by_page)
    for n, page in enumerate(ordered):
        chunks: list[dict] = []
        cur = ""
        for sentence, new_paragraph in by_page[page]:
            for j, piece in enumerate(_split_long(sentence, limit)):
                sep = "\n" if (new_paragraph and j == 0) else " "
                if cur and len(cur) + 1 + len(piece) > target:
                    chunks.append({"index": index, "page": page, "text": cur})
                    index += 1
                    cur = ""
                cur = f"{cur}{sep}{piece}" if cur else piece
        if cur:
            chunks.append({"index": index, "page": page, "text": cur})
            index += 1
        next_page = ordered[n + 1] if n + 1 < len(ordered) else total_pages + 1
        covered = [pg for pg in range(page, next_page) if pg in pages_with_text]
        units.append({"page": page, "pages": covered or [page], "name": name_of(page), "chunks": chunks})

    no_text = [pg for pg in range(1, total_pages + 1) if pg not in pages_with_text]
    return units, no_text


def pdf_toc(pdf_path: str) -> list:
    try:
        with pymupdf.open(pdf_path) as doc:
            return doc.get_toc(simple=True)
    except Exception:
        return []


def pdf_title(pdf_path: str) -> str | None:
    try:
        with pymupdf.open(pdf_path) as doc:
            title = (doc.metadata or {}).get("title") or ""
            title = title.strip()
            return title or None
    except Exception:
        return None
