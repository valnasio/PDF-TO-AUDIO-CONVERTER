// Leitor de PDF em Áudio: versão para navegador (roda toda no aparelho).
import { extractSegments, renderFirstPage } from "./pdf-text.js?v=2.7.1";
import { NEURAL_ID, NEURAL_VOICES, neuralSupported, prepareNeural, splitForQuickStart, synthNeural, warmNeural } from "./neural.js?v=2.7.1";

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
  delFile: (id) => tx("files", "readwrite", (s) => s.delete(id)),
  remove: async (id) => {
    await tx("docs", "readwrite", (s) => s.delete(id));
    for (const key of [id, `cover:${id}`, `page1:${id}`]) await tx("files", "readwrite", (s) => s.delete(key));
  },
};

// ------------------------------------------------------------ utilidades
const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDuration = (s) => {
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h}h ${m}min` : `${Math.max(1, m)} min`;
};
const CHARS_PER_SECOND = 14; // ritmo médio de fala em português a 1×
// Capas em cores chapadas de "encadernação", escolhidas pelo título.
const COVER_COLORS = ["#3b2fd0", "#0e7c6b", "#8a3b12", "#5b2a86", "#1f5c99", "#7a2e3a", "#2f6b3a", "#9a5b00"];
function coverStyle(title) {
  let h = 0;
  for (const ch of title) h = (h * 31 + ch.charCodeAt(0)) % 9973;
  return `background: ${COVER_COLORS[h % COVER_COLORS.length]};`;
}

// Capa do livro: a imagem escolhida pelo usuário ou, sem ela, a página 1 do PDF.
// As duas ficam guardadas neste navegador junto do PDF ("cover:id" e "page1:id").
async function coverBlob(doc) {
  if (doc.cover) {
    const own = await store.file(`cover:${doc.id}`);
    if (own) return own;
  }
  let page1 = await store.file(`page1:${doc.id}`);
  if (!page1) {  // livros guardados antes desta versão: desenha agora e guarda
    const pdf = await store.file(doc.id);
    if (!pdf) return null;
    try { page1 = await renderFirstPage(new Uint8Array(await pdf.arrayBuffer())); } catch { return null; }
    if (page1) await store.putFile(`page1:${doc.id}`, page1);
  }
  return page1 || null;
}
// O título fica numa faixa escura por cima da imagem, legível em qualquer capa.
// Sem imagem, sobra a cor chapada de antes.
function coverHtml(d, url, tools = false) {
  return `<div class="cover ${url ? "has-img" : ""}" style="${coverStyle(d.title)}">
    ${url ? `<img src="${url}" alt="" decoding="async" />` : ""}
    <span class="cover-title">${esc(d.title)}</span>
    ${tools ? `<div class="cover-tools">
      <button type="button" data-cover-pick="${d.id}" title="Usar uma imagem sua como capa">🖼 Capa</button>
      ${d.cover ? `<button type="button" data-cover-reset="${d.id}" title="Tirar a capa e voltar a mostrar a página 1 do PDF">↺</button>` : ""}
    </div>` : ""}
  </div>`;
}

// Modo claro / noturno (mesma escolha da página de download).
(function setupThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const sync = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    btn.setAttribute("aria-label", dark ? "Ativar modo claro" : "Ativar modo noturno");
    btn.title = btn.getAttribute("aria-label");
  };
  btn.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("theme", next);
    sync();
  });
  sync();
})();
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
      try {
        const page1 = await renderFirstPage(new Uint8Array(buf.slice(0)));
        if (page1) await store.putFile(`page1:${id}`, page1);
      } catch { /* sem miniatura: a biblioteca usa a cor */ }
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
  const status = $("#pl-status", root), hint = $("#pl-voice-hint", root);
  let doc = null, voices = [], idx = 0, playing = false, token = 0, renderedPage = 0, utter = null;
  let pageStart = new Map(), charsBefore = [];
  let coverUrl = null;

  const browserTts = "speechSynthesis" in window;
  const canNeural = neuralSupported();
  const supported = browserTts || canNeural;
  const isNeural = () => voiceSel.value.startsWith("neural:");
  const neuralVoice = () => voiceSel.value.slice("neural:".length);

  // Motor da voz natural: fila de trechos gerados à frente; cada trecho pronto já
  // vira um áudio pré-carregado, então a troca de frase acontece sem silêncio.
  let cur = null;  // áudio tocando agora
  let endWaiter = null, neuralPaused = false;
  const AHEAD = 6;
  const urls = new Set();
  const releaseAll = () => { urls.forEach((u) => URL.revokeObjectURL(u)); urls.clear(); };

  const save = () => { if (doc) { doc.pos = idx; store.put(doc); } };
  const setStatus = (text) => { status.textContent = text || ""; status.hidden = !text; };

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

  const finished = () => { stop(); idx = 0; save(); highlight(); };

  // ---------- vozes do navegador (speechSynthesis)
  const speakBrowser = () => {
    const my = ++token;
    speechSynthesis.cancel();
    if (idx >= doc.segments.length) { finished(); return; }
    highlight();
    utter = new SpeechSynthesisUtterance(doc.segments[idx].t);
    const v = voices.find((x) => x.voiceURI === voiceSel.value);
    if (v) { utter.voice = v; utter.lang = v.lang; } else utter.lang = "pt-BR";
    utter.rate = Number(speedSel.value);
    utter.onend = () => {
      if (my !== token || !playing) return;
      idx++;
      if (idx % 5 === 0) save();
      speakBrowser();
    };
    utter.onerror = (e) => {
      if (my !== token || e.error === "interrupted" || e.error === "canceled") return;
      idx++; speakBrowser();
    };
    speechSynthesis.speak(utter);
  };

  // ---------- voz natural (Piper no aparelho)
  const fallbackToBrowser = (err) => {
    console.warn("Voz natural indisponível:", err);
    const first = voices[0];
    hint.innerHTML = 'A voz natural não funcionou neste navegador, então passamos a usar a voz do navegador. Para vozes naturais com MP3, <a class="link" href="../">baixe o aplicativo</a>.';
    hint.hidden = false;
    setStatus("");
    if (!first) { stop(); return; }
    voiceSel.value = first.voiceURI;
    localStorage.setItem("voice", first.voiceURI);
    if (playing) speakBrowser();
  };

  const speakNeural = async () => {
    const my = ++token;
    if (browserTts) speechSynthesis.cancel();
    cur?.pause();
    endWaiter?.(false);
    releaseAll();
    neuralPaused = false;
    if (idx >= doc.segments.length) { finished(); return; }
    highlight();
    try {
      await prepareNeural(neuralVoice(), (p) => { if (my === token) setStatus(`Baixando a voz natural (63 MB, só na primeira vez): ${Math.round(p * 100)}%`); });
    } catch (e) { if (my === token) fallbackToBrowser(e); return; }
    if (my !== token || !playing) return;

    // fila: a primeira frase é dividida nas vírgulas para a voz começar logo
    let nextSeg = idx;
    const queue = [];
    const fill = () => {
      while (queue.length < AHEAD && nextSeg < doc.segments.length) {
        const seg = nextSeg++;
        const pieces = seg === idx && queue.length === 0 ? splitForQuickStart(doc.segments[seg].t) : [doc.segments[seg].t];
        pieces.forEach((text, k) => {
          const voice = neuralVoice();
          const job = synthNeural(voice, text).then((blob) => {
            // trecho sem fala (ex.: só pontuação): WAV vazio ou com menos de ~50 ms
            if (blob.size < 44 + 22050 * 2 * 0.05) return null;
            const url = URL.createObjectURL(blob);
            urls.add(url);
            const a = new Audio(url);
            a.preload = "auto";
            a.preservesPitch = true;
            return a;
          });
          job.catch(() => {});
          queue.push({ seg, last: k === pieces.length - 1, job });
        });
      }
    };
    fill();
    while (my === token) {
      const item = queue.shift();
      if (!item) { finished(); return; }
      idx = item.seg;
      highlight();
      let a;
      const slow = setTimeout(() => { if (my === token) setStatus("Gerando a voz…"); }, 700);
      try { a = await item.job; } catch (e) { clearTimeout(slow); if (my === token) fallbackToBrowser(e); return; }
      clearTimeout(slow);
      if (my !== token) return;
      setStatus("");
      fill();
      if (!a) {  // trecho sem fala: segue para o próximo
        if (item.last) idx = item.seg + 1;
        continue;
      }
      cur = a;
      a.playbackRate = Number(speedSel.value);
      // Fim da frase: pelo evento "ended"; ou conferindo o estado do áudio a cada
      // 250 ms (alguns navegadores deixam de disparar o evento); ou, se o áudio
      // não começar em 3 s ou der erro, pula para a próxima frase em vez de travar.
      const tPlay = performance.now();
      const ended = new Promise((resolve) => {
        let done = false;
        const finish = (ok) => { if (!done) { done = true; clearInterval(poll); resolve(ok); } };
        const poll = setInterval(() => {
          const started = a.currentTime > 0;
          if (a.ended || (a.duration && started && a.currentTime >= a.duration - 0.02)) finish(true);
          else if (!started && !a.paused && performance.now() - tPlay > 3000) finish(true);
          else if (a.error) finish(true);
        }, 250);
        endWaiter = finish;
        a.onended = () => finish(true);
        a.onerror = () => finish(true);
      });
      const playing = a.play();
      const startOk = await Promise.race([
        playing.then(() => true, () => false),
        new Promise((r) => setTimeout(() => r("timeout"), 3000)),
      ]);
      if (startOk === false && my === token) { endWaiter?.(false); stop(); return; }  // navegador recusou tocar
      const ok = await ended;
      URL.revokeObjectURL(a.src); urls.delete(a.src);
      if (!ok || my !== token) return;
      if (item.last) {
        idx = item.seg + 1;
        if (idx % 5 === 0) save();
      }
    }
  };

  const speak = () => (isNeural() ? speakNeural() : speakBrowser());

  const play = () => {
    if (!supported) return;
    playing = true;
    playBtn.textContent = "❚❚";
    // continuar de onde pausou, sem gerar a frase de novo
    if (isNeural() && neuralPaused && cur) { neuralPaused = false; cur.play().catch(() => speak()); return; }
    speak();
  };
  const pause = () => {
    playing = false;
    playBtn.textContent = "▶";
    if (isNeural() && cur && !cur.ended) { cur.pause(); neuralPaused = true; save(); return; }
    stop();
  };
  const stop = () => {
    playing = false; token++;
    if (browserTts) speechSynthesis.cancel();
    cur?.pause(); endWaiter?.(false); releaseAll(); cur = null; neuralPaused = false;
    playBtn.textContent = "▶"; setStatus(""); save();
  };
  const jump = (i) => {
    idx = Math.max(0, Math.min(doc.segments.length - 1, i));
    if (playing) speak(); else { stop(); highlight(); save(); }
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
    const showCover = (url) => {
      const holder = document.createElement("div");
      holder.innerHTML = coverHtml(doc, url);
      holder.firstElementChild.id = "pl-cover";
      $("#pl-cover", root).replaceWith(holder.firstElementChild);
    };
    showCover(null);
    coverBlob(doc).then((blob) => {
      if (!blob || !root.isConnected) return;
      coverUrl = URL.createObjectURL(blob);
      showCover(coverUrl);
    });
    $("#pl-read", root).href = `#/ler/${id}`;
    seek.max = doc.segments.length - 1;
    pageSel.innerHTML = [...pageStart.keys()].map((p) => `<option value="${p}">Página ${p}</option>`).join("");
    speedSel.value = localStorage.getItem("speed") || "1";
    highlight();

    if (!supported) {
      hint.textContent = "Seu navegador não consegue ler em voz alta. Use uma versão atual do Chrome, Edge, Firefox ou Safari, ou baixe o aplicativo.";
      hint.hidden = false; playBtn.disabled = true; return;
    }
    voices = browserTts ? rankVoices(await loadVoices()) : [];
    const label = (v) => `${isNatural(v) ? "✨ " : ""}${esc(v.name.replace(/^Microsoft\s+/, "").replace(/\s*-\s*Portuguese.*$/i, "").replace(/\s*Online\s*\(Natural\)/i, ""))} (${esc(v.lang)})`;
    const options = [];
    if (canNeural) options.push(`<optgroup label="Vozes naturais no aparelho (qualquer navegador)">${NEURAL_VOICES.map((v) => `<option value="neural:${v.id}">✨ ${v.name} (pt-BR)</option>`).join("")}</optgroup>`);
    if (voices.length) options.push(`<optgroup label="Vozes deste navegador">${voices.map((v) => `<option value="${esc(v.voiceURI)}">${label(v)}</option>`).join("")}</optgroup>`);
    voiceSel.innerHTML = options.join("");

    // voz padrão: a escolhida antes; senão uma voz natural do navegador (Edge); senão a Faber
    const saved = localStorage.getItem("voice");
    const valid = (v) => [...voiceSel.options].some((o) => o.value === v);
    voiceSel.value = valid(saved) ? saved : (voices.find(isNatural)?.voiceURI || (canNeural ? NEURAL_ID : voices[0]?.voiceURI || ""));
    if (!voiceSel.value) {
      hint.innerHTML = 'Nenhuma voz em português disponível neste navegador. <a class="link" href="../">Baixe o aplicativo</a>.';
      hint.hidden = false; playBtn.disabled = true; return;
    }
    if (isNeural()) {
      hint.textContent = "Esta voz é gerada no seu aparelho. Na primeira vez, ela é baixada (63 MB) e fica guardada para as próximas.";
      hint.hidden = false;
      warmNeural(neuralVoice());  // voz já guardada: deixa pronta em segundo plano
    }

    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: doc.title, artist: "Leitor de PDF em Áudio" });
      navigator.mediaSession.setActionHandler("play", play);
      navigator.mediaSession.setActionHandler("pause", pause);
      navigator.mediaSession.setActionHandler("previoustrack", () => jump(idx - 1));
      navigator.mediaSession.setActionHandler("nexttrack", () => jump(idx + 1));
    }
  })();

  playBtn.addEventListener("click", () => (playing ? pause() : play()));
  $("#pl-prev", root).addEventListener("click", () => jump(idx - 1));
  $("#pl-next", root).addEventListener("click", () => jump(idx + 1));
  seek.addEventListener("input", () => {
    const i = Number(seek.value);
    $("#pl-pct", root).textContent = `${Math.floor((i / Math.max(1, doc.segments.length - 1)) * 100)}%`;
  });
  seek.addEventListener("change", () => jump(Number(seek.value)));
  pageSel.addEventListener("change", () => jump(pageStart.get(Number(pageSel.value))));
  voiceSel.addEventListener("change", () => {
    localStorage.setItem("voice", voiceSel.value);
    hint.hidden = !isNeural();
    if (isNeural()) hint.textContent = "Esta voz é gerada no seu aparelho. Na primeira vez, ela é baixada (63 MB) e fica guardada para as próximas.";
    if (playing) speak(); else stop();
  });
  speedSel.addEventListener("change", () => {
    localStorage.setItem("speed", speedSel.value);
    updateProgress();
    if (cur) cur.playbackRate = Number(speedSel.value);  // voz natural: muda na hora, sem reiniciar
    if (playing && !isNeural()) speak();
  });
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

  // para testes automáticos: estado do player
  window.__player = () => ({ idx, playing, neural: isNeural(), voice: voiceSel.value,
    audioTime: cur ? cur.currentTime : 0, audioPaused: cur ? cur.paused : true, rate: cur ? cur.playbackRate : 0,
    status: status.textContent, hint: hint.hidden ? "" : hint.textContent });

  return () => {
    stop();
    if (coverUrl) URL.revokeObjectURL(coverUrl);
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("pagehide", onHide);
  };
}

// ------------------------------------------------------------ tela: biblioteca
function renderLibrary() {
  const root = mount("#tpl-library");
  const grid = $("#lib-grid", root), search = $("#lib-search", root);
  let docs = [];
  const urls = new Map();  // id → endereço da imagem da capa

  const card = (d) => {
    const pct = Math.floor(((d.pos || 0) / Math.max(1, d.segments.length - 1)) * 100);
    return `
      <article class="lib-card">
        ${coverHtml(d, urls.get(d.id), true)}
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
  const loadCover = async (d) => {
    const blob = await coverBlob(d);
    if (urls.has(d.id)) URL.revokeObjectURL(urls.get(d.id));
    if (blob) urls.set(d.id, URL.createObjectURL(blob)); else urls.delete(d.id);
  };
  store.all().then(async (all) => {
    docs = all.sort((a, b) => b.createdAt - a.createdAt);
    draw();
    for (const d of docs) { await loadCover(d); draw(); }
  });

  // Capa própria: fica guardada junto do livro até o usuário trocar ou remover.
  const picker = $("#lib-cover-file", root);
  let pickFor = null;
  const saveDoc = async (doc) => {
    await store.put(doc);
    docs = docs.map((x) => (x.id === doc.id ? doc : x));
    await loadCover(doc);
    draw();
  };
  picker.addEventListener("change", async () => {
    const file = picker.files[0];
    picker.value = "";
    const doc = docs.find((x) => x.id === pickFor);
    if (!file || !doc) return;
    if (!/^image\/(jpeg|png|webp|gif)$/.test(file.type)) { alert("Use uma imagem JPG, PNG, WebP ou GIF."); return; }
    if (file.size > 15 * 1024 * 1024) { alert("A imagem passa de 15 MB. Escolha uma imagem menor."); return; }
    await store.putFile(`cover:${doc.id}`, new Blob([await file.arrayBuffer()], { type: file.type }));
    await saveDoc({ ...doc, cover: true });
  });

  search.addEventListener("input", draw);
  grid.addEventListener("click", async (ev) => {
    const pick = ev.target.closest("[data-cover-pick]");
    if (pick) { pickFor = pick.dataset.coverPick; picker.click(); return; }
    const reset = ev.target.closest("[data-cover-reset]");
    if (reset) {
      const doc = docs.find((x) => x.id === reset.dataset.coverReset);
      if (!doc || !confirm("Tirar esta capa? A biblioteca volta a mostrar a página 1 do PDF.")) return;
      await store.delFile(`cover:${doc.id}`);
      await saveDoc({ ...doc, cover: false });
      return;
    }
    const b = ev.target.closest("[data-del]");
    if (!b) return;
    const d = docs.find((x) => x.id === b.dataset.del);
    if (!confirm(`Excluir "${d.title}" da biblioteca?`)) return;
    await store.remove(d.id);
    docs = docs.filter((x) => x.id !== d.id);
    draw();
  });
  return () => { for (const u of urls.values()) URL.revokeObjectURL(u); };
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
