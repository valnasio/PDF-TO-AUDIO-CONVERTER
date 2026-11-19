// Extração e preparação do texto do PDF no navegador (pdf.js).
// Mesma lógica do app desktop (app/pdf_text.py): sem palavras cortadas,
// sem cabeçalhos/rodapés repetidos e com parágrafos unidos entre páginas.

import * as pdfjs from "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.min.mjs";

pdfjs.GlobalWorkerOptions.workerSrc =
  "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.min.mjs";

const CLITICS = new Set([
  "se", "me", "te", "lhe", "lhes", "nos", "vos", "o", "a", "os", "as",
  "lo", "la", "los", "las", "no", "na", "nas", "ia", "ias", "iam", "ei", "emos", "eis",
]);
const SENTENCE_END = /[.!?…:;"”»)]\s*$/;
const PAGE_NUMBER = /^\s*(p[áa]g(ina)?\.?\s*)?\d{1,5}(\s*(\/|de)\s*\d{1,5})?\s*$/i;
const ROMAN = /^[ivxlcdm]+\.?$/i;

const normalize = (t) =>
  t.normalize("NFKC")
    .replace(/­/g, "")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏﻿]/g, "")
    .replace(/[•▪●◦■□►▶]\s*/g, "")
    .replace(/\s+/g, " ")
    .trim();

const signature = (t) => t.toLowerCase().replace(/\d+/g, "#").trim();
const isNoise = (t) => !t || PAGE_NUMBER.test(t) || !/[\p{L}\p{N}]/u.test(t);
const endsSentence = (t) => SENTENCE_END.test(t);
const looksLikeHeading = (t) => t.length <= 80 && !endsSentence(t);

/** Junta duas linhas desfazendo a hifenização de fim de linha. */
function joinLines(a, b) {
  const m = a.match(/(\p{L})[-‐‑]$/u);
  if (m && /^\p{L}/u.test(b)) {
    const next = b.match(/^[\p{L}]+/u)[0];
    if (/^\p{Lu}/u.test(next) || CLITICS.has(next.toLowerCase())) return a + b; // mantém hífen
    return a.slice(0, -1) + b;
  }
  return `${a} ${b}`;
}

/** Agrupa os itens de texto do pdf.js em linhas. */
function pageLines(content) {
  const lines = [];
  let cur = null;
  for (const it of content.items) {
    if (it.str === undefined) continue;
    const x = it.transform[4], y = it.transform[5];
    const size = Math.hypot(it.transform[2], it.transform[3]) || it.height || 10;
    if (cur && Math.abs(cur.y - y) > size * 0.6 && it.str.trim()) {
      lines.push(cur);
      cur = null;
    }
    if (!cur) cur = { text: "", x, y, size, right: x };
    cur.text += it.str;
    cur.right = Math.max(cur.right, x + (it.width || 0));
    if (it.hasEOL) { lines.push(cur); cur = null; }
  }
  if (cur) lines.push(cur);
  return lines
    .map((l) => ({ ...l, text: normalize(l.text) }))
    .filter((l) => l.text);
}

/** Divide as linhas de uma página em blocos (parágrafos visuais). */
function pageBlocks(lines) {
  if (!lines.length) return [];
  const gaps = [];
  for (let i = 1; i < lines.length; i++) {
    const g = lines[i - 1].y - lines[i].y;
    if (g > 0) gaps.push(g);
  }
  gaps.sort((a, b) => a - b);
  const typicalGap = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 14;
  const minX = Math.min(...lines.map((l) => l.x));
  const maxRight = Math.max(...lines.map((l) => l.right));

  const blocks = [];
  let block = null;
  lines.forEach((l, i) => {
    const prev = lines[i - 1];
    let brk = !block;
    if (prev && block) {
      const gap = prev.y - l.y;
      const prevShort = prev.right < maxRight - (maxRight - minX) * 0.15;
      if (gap > typicalGap * 1.45 || gap < -typicalGap) brk = true; // espaço maior ou mudança de coluna
      else if (endsSentence(prev.text) && (prevShort || l.x > minX + 8)) brk = true; // fim de parágrafo / recuo
      else if (/^[-–—]\s/.test(l.text)) brk = true; // fala de diálogo
    }
    if (brk) {
      block = { text: l.text, top: l.y, bottom: l.y };
      blocks.push(block);
    } else {
      block.text = joinLines(block.text, l.text);
      block.bottom = l.y;
    }
  });
  return blocks;
}

/**
 * Lê o PDF inteiro e devolve os segmentos de fala:
 * [{ t: "frase", p: página, s: true se começa parágrafo }]
 */
export async function extractSegments(data, onProgress) {
  const pdf = await pdfjs.getDocument({ data, isEvalSupported: false }).promise;
  const total = pdf.numPages;
  const pages = [];
  const marginCount = new Map();

  for (let n = 1; n <= total; n++) {
    const page = await pdf.getPage(n);
    const height = page.getViewport({ scale: 1 }).height || 1;
    const content = await page.getTextContent();
    const blocks = pageBlocks(pageLines(content)).map((b) => {
      const inMargin = b.top > height * 0.91 || b.bottom < height * 0.09;
      if (inMargin) {
        const sig = signature(b.text);
        marginCount.set(sig, (marginCount.get(sig) || 0) + 1);
      }
      return { text: b.text, inMargin };
    });
    pages.push(blocks);
    page.cleanup();
    onProgress?.(n, total);
    if (n % 20 === 0) await new Promise((r) => setTimeout(r)); // mantém a página responsiva
  }

  let title = "";
  try {
    const meta = await pdf.getMetadata();
    title = (meta?.info?.Title || "").trim();
  } catch {}
  await pdf.destroy();

  const minRepeat = Math.max(3, Math.floor(total * 0.3));
  const paragraphs = [];
  pages.forEach((blocks, idx) => {
    const pageNo = idx + 1;
    for (const { text, inMargin } of blocks) {
      if (isNoise(text)) continue;
      if (inMargin && (marginCount.get(signature(text)) || 0) >= minRepeat) continue;
      const prev = paragraphs[paragraphs.length - 1];
      const continues = prev && !endsSentence(prev.text) && !looksLikeHeading(prev.text)
        && (/^[\p{Ll}\p{N}]/u.test(text) || prev.page !== pageNo) && !ROMAN.test(text);
      if (continues) prev.text = joinLines(prev.text, text);
      else paragraphs.push({ page: pageNo, text });
    }
  });

  const segments = [];
  for (const p of paragraphs) {
    const text = endsSentence(p.text) ? p.text : `${p.text}.`; // pausa natural em títulos
    splitSentences(text).forEach((t, i) => segments.push({ t, p: p.page, s: i === 0 }));
  }
  return { segments, pages: total, title };
}

const MAX_SEGMENT = 220; // frases curtas: evitam o corte de fala longa em alguns navegadores

export function splitSentences(text) {
  const sentences = text.split(/(?<=[.!?…]["”»)]*)\s+(?=\S)/u);
  const out = [];
  for (const s of sentences) out.push(...splitLong(s.trim()));
  return out.filter(Boolean);
}

function splitLong(text) {
  if (text.length <= MAX_SEGMENT) return [text];
  const clauses = text.split(/(?<=[,;:—])\s+/u);
  const parts = clauses.length > 1 ? clauses : text.split(" ");
  const sep = " ";
  const out = [];
  let cur = "";
  for (const part of parts) {
    if (part.length > MAX_SEGMENT && clauses.length > 1) {
      if (cur) { out.push(cur); cur = ""; }
      out.push(...splitLong(part));
      continue;
    }
    if (cur && cur.length + 1 + part.length > MAX_SEGMENT) { out.push(cur); cur = part; }
    else cur = cur ? cur + sep + part : part;
  }
  if (cur) out.push(cur);
  return out;
}
