// Leitor de PDF em Áudio — versão mini (100% no navegador).
import { extractSegments } from "./pdf-text.js";

const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
let cleanup = null;

// ------------------------------------------------------------ armazenamento (IndexedDB)
const db = await new Promise((resolve, reject) => {
  const req = indexedDB.open("leitor-pdf-mini", 1);
  req.onupgradeneeded = () => {
    req.result.createObjectStore("docs", { keyPath: "id" });
    req.result.createObjectStore("files");
  };
  req.onsuccess = () => resolve(req.result);
  req.onerror = () => reject(req.error);
});
navigator.storage?.persist?.().catch(() => {});

function tx(store, mode, fn) {
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, mode);
    const r = fn(t.objectStore(store));
    t.oncomplete = () => resolve(r?.result);
    t.onerror = () => reject(t.error);
  });
}
const store = {
  all: () => tx("docs", "readonly", (s) => s.getAll()),
  get: (id) => tx("docs", "readonly", (s) => s.get(id)),
  put: (doc) => tx("docs", "readwrite", (s) => s.put(doc)),
  file: (id) => tx("files", "readonly", (s) => s.get(id)),
  putFile: (id, blob) => tx("files", "readwrite", (s) => s.put(blob, id)),
  remove: async (id) => {
    await tx("docs", "readwrite", (s) => s.delete(id));
    await tx("files", "readwrite", (s) => s.delete(id));
  },
};

// ------------------------------------------------------------ utilidades
const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDuration = (s) => {
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h}h ${m}min` : `${Math.max(1, m)} min`;
};
const CHARS_PER_SECOND = 14; // ritmo médio de fala em português a 1×
function coverStyle(title) {
  let h = 0;
  for (const ch of title) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `background: linear-gradient(145deg, hsl(${h} 65% 45%), hsl(${(h + 60) % 360} 70% 28%));`;
}
function mount(tplId) {
  app.replaceChildren($(tplId).content.cloneNode(true));
  return app.firstElementChild;
}

// ------------------------------------------------------------ vozes do navegador
function loadVoices() {
  return new Promise((resolve) => {
    const ready = () => {
      const all = speechSynthesis.getVoices();
      if (all.length) resolve(all);
    };
    ready();
    speechSynthesis.addEventListener("voiceschanged", ready);
    setTimeout(() => resolve(speechSynthesis.getVoices()), 2500);
  });
}
const isNatural = (v) => /natural|neural|online|premium|enhanced/i.test(v.name);
function rankVoices(voices) {
  const pt = voices.filter((v) => v.lang?.toLowerCase().startsWith("pt"));
  const score = (v) =>
    (isNatural(v) ? 0 : 10) + (/google/i.test(v.name) ? 0 : 3) +
    (v.lang.toLowerCase() === "pt-br" || v.lang.toLowerCase() === "pt_br" ? 0 : 1) + (v.localService ? 1 : 0);
  return pt.sort((a, b) => score(a) - score(b));
}

// ------------------------------------------------------------ roteador
const routes = [
  [/^#?\/?$/, renderUpload, "upload"],
  [/^#\/biblioteca$/, renderLibrary, "library"],
  [/^#\/ouvir\/([\w-]+)$/, renderPlayer, "library"],
  [/^#\/ler\/([\w-]+)$/, renderReader, "library"],
];
function router() {
  if (cleanup) { cleanup(); cleanup = null; }
  const hash = location.hash || "#/";
  for (const [re, fn, nav] of routes) {
    const m = hash.match(re);
    if (m) {
      document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
      cleanup = fn(...m.slice(1)) || null;
      window.scrollTo(0, 0);
      return;
    }
  }
  location.hash = "#/";
}
window.addEventListener("hashchange", router);

// ------------------------------------------------------------ tela: envio
function renderUpload() {
  const root = mount("#tpl-upload");
  const input = $("#file", root), drop = $("#dropzone", root), err = $("#upload-error", root);
  let busy = false;

  const handle = async (file) => {
    if (!file || busy) return;
    err.hidden = true;
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
      err.textContent = "Selecione um arquivo PDF."; err.hidden = false; return;
    }
    busy = true;
    drop.hidden = true;
    const proc = $("#processing", root);
    proc.hidden = false;
    $("#proc-title", root).textContent = file.name;
    try {
      const buf = await file.arrayBuffer();
      const { segments, pages, title } = await extractSegments(new Uint8Array(buf.slice(0)), (n, total) => {
        $("#proc-fill", root).style.width = `${(n / total) * 100}%`;
        $("#proc-label", root).textContent = `Lendo página ${n} de ${total}…`;
      });
      if (!segments.length) throw new Error("Este PDF não tem texto selecionável (provavelmente é digitalizado).");
      const id = crypto.randomUUID?.() || String(Date.now());
      const name = file.name.replace(/\.pdf$/i, "").replace(/[_-]+/g, " ").trim();
      await store.putFile(id, new Blob([buf], { type: "application/pdf" }));
      await store.put({
        id, title: title || name, filename: file.name, pages, segments, pos: 0,
        chars: segments.reduce((n, s) => n + s.t.length, 0), createdAt: Date.now(),
      });
      location.hash = `#/ouvir/${id}`;
    } catch (e) {
      err.textContent = e.message || "Não foi possível ler este PDF.";
      err.hidden = false; proc.hidden = true; drop.hidden = false; busy = false;
    }
  };

  input.addEventListener("change", () => handle(input.files[0]));
  ["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (ev) => handle(ev.dataTransfer.files[0]));
}

// ------------------------------------------------------------ tela: player
function renderPlayer(id) {
  const root = mount("#tpl-player");
  const playBtn = $("#pl-play", root), seek = $("#pl-seek", root), textBox = $("#pl-text", root);
  const voiceSel = $("#pl-voice", root), speedSel = $("#pl-speed", root), pageSel = $("#pl-page", root);
  let doc = null, voices = [], idx = 0, playing = false, token = 0, renderedPage = 0, utter = null;
  let pageStart = new Map(), charsBefore = [];

  const supported = "speechSynthesis" in window;

  const save = () => { if (doc) { doc.pos = idx; store.put(doc); } };

  const updateProgress = () => {
    const segs = doc.segments;
    seek.value = idx;
    $("#pl-pct", root).textContent = `${Math.floor((idx / Math.max(1, segs.length - 1)) * 100)}%`;
    const left = doc.chars - charsBefore[idx];
    $("#pl-left", root).textContent = `≈ ${fmtDuration(left / (CHARS_PER_SECOND * Number(speedSel.value)))}`;
  };

  // Renderiza só a página atual (escala para livros com milhares de páginas).
  const renderPage = (page) => {
    if (page === renderedPage) return;
    renderedPage = page;
    const start = pageStart.get(page);
    let html = `<span class="page-mark">Página ${page}</span><p>`;
    for (let i = start; i < doc.segments.length && doc.segments[i].p === page; i++) {
      const s = doc.segments[i];
      if (s.s && i !== start) html += "</p><p>";
      html += `<span class="s" data-i="${i}">${esc(s.t)}</span> `;
    }
    textBox.innerHTML = html + "</p>";
    textBox.scrollTop = 0;
  };

  const highlight = () => {
    const seg = doc.segments[idx];
    renderPage(seg.p);
    textBox.querySelectorAll(".s.active").forEach((el) => el.classList.remove("active"));
    textBox.querySelectorAll(".s").forEach((el) => el.classList.toggle("done", Number(el.dataset.i) < idx));
    const el = textBox.querySelector(`.s[data-i="${idx}"]`);
    if (el) {
      el.classList.add("active");
      const top = el.offsetTop - textBox.offsetTop;
      if (top < textBox.scrollTop || top > textBox.scrollTop + textBox.clientHeight - 60) textBox.scrollTop = top - 40;
    }
    $("#pl-curpage", root).textContent = `Página ${seg.p} de ${doc.pages}`;
    pageSel.value = String(seg.p);
    updateProgress();
  };

  const speak = () => {
    const my = ++token;
    speechSynthesis.cancel();
    if (idx >= doc.segments.length) { stop(); idx = 0; save(); return; }
    highlight();
    utter = new SpeechSynthesisUtterance(doc.segments[idx].t);
    const v = voices.find((x) => x.voiceURI === voiceSel.value);
    if (v) { utter.voice = v; utter.lang = v.lang; } else utter.lang = "pt-BR";
    utter.rate = Number(speedSel.value);
    utter.onend = () => {
      if (my !== token || !playing) return;
      idx++;
      if (idx % 5 === 0) save();
      speak();
    };
    utter.onerror = (e) => {
      if (my !== token || e.error === "interrupted" || e.error === "canceled") return;
      idx++; speak();
    };
    speechSynthesis.speak(utter);
  };

  const play = () => { if (!supported) return; playing = true; playBtn.textContent = "❚❚"; speak(); };
  const stop = () => { playing = false; token++; speechSynthesis.cancel(); playBtn.textContent = "▶"; save(); };
  const jump = (i) => {
    idx = Math.max(0, Math.min(doc.segments.length - 1, i));
    if (playing) speak(); else { highlight(); save(); }
  };

  (async () => {
    doc = await store.get(id);
    if (!doc) { location.hash = "#/biblioteca"; return; }
    idx = Math.min(doc.pos || 0, doc.segments.length - 1);
    let acc = 0;
    doc.segments.forEach((s, i) => {
      if (!pageStart.has(s.p)) pageStart.set(s.p, i);
      charsBefore.push(acc); acc += s.t.length;
    });

    $("#pl-title", root).textContent = doc.title;
    $("#pl-meta", root).textContent = `${doc.pages} páginas · ≈ ${fmtDuration(doc.chars / CHARS_PER_SECOND)} de leitura`;
    const cover = $("#pl-cover", root);
    cover.setAttribute("style", coverStyle(doc.title));
    cover.textContent = doc.title;
    $("#pl-read", root).href = `#/ler/${id}`;
    seek.max = doc.segments.length - 1;
    pageSel.innerHTML = [...pageStart.keys()].map((p) => `<option value="${p}">Página ${p}</option>`).join("");
    speedSel.value = localStorage.getItem("speed") || "1";
    highlight();

    const hint = $("#pl-voice-hint", root);
    if (!supported) {
      hint.textContent = "Seu navegador não suporta leitura em voz alta. Use o Edge, Chrome ou Safari, ou baixe o app completo.";
      hint.hidden = false; playBtn.disabled = true; return;
    }
    voices = rankVoices(await loadVoices());
    if (!voices.length) {
      hint.innerHTML = 'Nenhuma voz em português encontrada neste navegador. Tente o <b>Microsoft Edge</b> ou <a class="link" href="../">baixe o app completo</a>.';
      hint.hidden = false;
    } else if (!voices.some(isNatural)) {
      hint.innerHTML = 'Dica: no <b>Microsoft Edge</b> você tem vozes naturais (Francisca, Antônio). Ou <a class="link" href="../">baixe o app completo</a> para vozes naturais em qualquer navegador e MP3.';
      hint.hidden = false;
    }
    const saved = localStorage.getItem("voice");
    voiceSel.innerHTML = voices.map((v) =>
      `<option value="${esc(v.voiceURI)}">${isNatural(v) ? "✨ " : ""}${esc(v.name.replace(/^Microsoft\s+/, "").replace(/\s*-\s*Portuguese.*$/i, "").replace(/\s*Online\s*\(Natural\)/i, ""))} (${esc(v.lang)})</option>`
    ).join("");
    if (saved && voices.some((v) => v.voiceURI === saved)) voiceSel.value = saved;

    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: doc.title, artist: "Leitor de PDF (mini)" });
      navigator.mediaSession.setActionHandler("play", play);
      navigator.mediaSession.setActionHandler("pause", stop);
      navigator.mediaSession.setActionHandler("previoustrack", () => jump(idx - 1));
      navigator.mediaSession.setActionHandler("nexttrack", () => jump(idx + 1));
    }
  })();

  playBtn.addEventListener("click", () => (playing ? stop() : play()));
  $("#pl-prev", root).addEventListener("click", () => jump(idx - 1));
  $("#pl-next", root).addEventListener("click", () => jump(idx + 1));
  seek.addEventListener("input", () => {
    const i = Number(seek.value);
    $("#pl-pct", root).textContent = `${Math.floor((i / Math.max(1, doc.segments.length - 1)) * 100)}%`;
  });
  seek.addEventListener("change", () => jump(Number(seek.value)));
  pageSel.addEventListener("change", () => jump(pageStart.get(Number(pageSel.value))));
  voiceSel.addEventListener("change", () => { localStorage.setItem("voice", voiceSel.value); if (playing) speak(); });
  speedSel.addEventListener("change", () => { localStorage.setItem("speed", speedSel.value); updateProgress(); if (playing) speak(); });
  textBox.addEventListener("click", (ev) => {
    const s = ev.target.closest(".s");
    if (s) { idx = Number(s.dataset.i); if (!playing) play(); else speak(); }
  });

  const onKey = (ev) => {
    if (ev.target.matches("input, select, textarea")) return;
    if (ev.code === "Space") { ev.preventDefault(); playBtn.click(); }
    else if (ev.code === "ArrowLeft") jump(idx - 1);
    else if (ev.code === "ArrowRight") jump(idx + 1);
  };
  document.addEventListener("keydown", onKey);
  const onHide = () => save();
  window.addEventListener("pagehide", onHide);

  return () => {
    if (supported) stop();
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("pagehide", onHide);
  };
}

// ------------------------------------------------------------ tela: biblioteca
function renderLibrary() {
  const root = mount("#tpl-library");
  const grid = $("#lib-grid", root), search = $("#lib-search", root);
  let docs = [];

  const card = (d) => {
    const pct = Math.floor(((d.pos || 0) / Math.max(1, d.segments.length - 1)) * 100);
    return `
      <article class="lib-card">
        <div class="cover" style="${coverStyle(d.title)}">${esc(d.title)}</div>
        <div class="lib-body">
          <span class="badge ready">${pct ? `${pct}% ouvido` : "Novo"}</span>
          <h3 title="${esc(d.title)}">${esc(d.title)}</h3>
          <div class="lib-meta">${d.pages} pág. · ≈ ${fmtDuration(d.chars / CHARS_PER_SECOND)}</div>
          ${pct ? `<div class="bar"><div class="fill" style="width:${pct}%"></div></div>` : ""}
          <div class="lib-actions">
            <a class="btn primary" href="#/ouvir/${d.id}">🎧 Ouvir</a>
            <a class="btn" href="#/ler/${d.id}">📖 Ler</a>
            <button class="btn danger" data-del="${d.id}" title="Excluir">🗑</button>
          </div>
        </div>
      </article>`;
  };
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    grid.innerHTML = docs.filter((d) => !q || d.title.toLowerCase().includes(q)).map(card).join("");
    $("#lib-empty", root).hidden = docs.length > 0;
  };
  store.all().then((all) => { docs = all.sort((a, b) => b.createdAt - a.createdAt); draw(); });
  search.addEventListener("input", draw);
  grid.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-del]");
    if (!b) return;
    const d = docs.find((x) => x.id === b.dataset.del);
    if (!confirm(`Excluir "${d.title}" da biblioteca?`)) return;
    await store.remove(d.id);
    docs = docs.filter((x) => x.id !== d.id);
    draw();
  });
}

// ------------------------------------------------------------ tela: leitor PDF
function renderReader(id) {
  const root = mount("#tpl-reader");
  let url = null;
  (async () => {
    const [doc, blob] = await Promise.all([store.get(id), store.file(id)]);
    if (!doc || !blob) { location.hash = "#/biblioteca"; return; }
    $("#rd-title", root).textContent = doc.title;
    $("#rd-listen", root).href = `#/ouvir/${id}`;
    url = URL.createObjectURL(blob);
    $("#rd-frame", root).src = url;
    $("#rd-open", root).href = url;
  })();
  return () => { if (url) setTimeout(() => URL.revokeObjectURL(url), 1000); };
}

router();
