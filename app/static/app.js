"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
let cleanup = null;

// ---------------------------------------------------------------- utilidades
const api = async (path, opts = {}) => {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
};

const fmtTime = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};
const fmtDuration = (s) => {
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h}h ${m}min` : `${Math.max(1, m)} min`;
};
const fmtSize = (b) => b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.ceil(b / 1e3)} KB`;
const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function coverStyle(title) {
  let h = 0;
  for (const ch of title) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `background: linear-gradient(145deg, hsl(${h} 65% 45%), hsl(${(h + 60) % 360} 70% 28%));`;
}

const STATUS_LABEL = {
  queued: "Na fila", extracting: "Lendo PDF", synthesizing: "Gerando áudio",
  finalizing: "Finalizando", ready: "Pronto", error: "Erro",
};
const isWorking = (s) => ["queued", "extracting", "synthesizing", "finalizing"].includes(s);

function progressOf(d) {
  if (d.status === "ready") return 1;
  if (d.status === "extracting") return d.pages ? 0.05 * (d.pages_done / d.pages) : 0;
  if (d.status === "synthesizing") return 0.05 + 0.9 * (d.chunks_total ? d.chunks_done / d.chunks_total : 0);
  if (d.status === "finalizing") return 0.97;
  if (d.status === "error" && d.chunks_total) return 0.05 + 0.9 * (d.chunks_done / d.chunks_total);
  return 0;
}

function mount(tplId) {
  app.replaceChildren($(tplId).content.cloneNode(true));
  return app.firstElementChild;
}

// ---------------------------------------------------------------- roteador
const routes = [
  [/^#?\/?$/, renderUpload, "upload"],
  [/^#\/biblioteca$/, renderLibrary, "library"],
  [/^#\/convertendo\/(\w+)$/, renderConverting, "upload"],
  [/^#\/ouvir\/(\w+)$/, renderPlayer, "library"],
  [/^#\/ler\/(\w+)$/, renderReader, "library"],
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

// ---------------------------------------------------------------- tela: envio
function renderUpload() {
  const root = mount("#tpl-upload");
  const input = $("#file", root), drop = $("#dropzone", root), btn = $("#convert", root);
  const chosen = $("#file-chosen", root), err = $("#upload-error", root);
  let file = null, voice = null, xhr = null;
  const sampleAudio = new Audio();

  const setFile = (f) => {
    err.hidden = true;
    if (!f) return;
    if (!/\.pdf$/i.test(f.name) && f.type !== "application/pdf") {
      err.textContent = "Selecione um arquivo PDF."; err.hidden = false; return;
    }
    file = f;
    chosen.textContent = `📎 ${f.name} — ${fmtSize(f.size)}`;
    chosen.hidden = false;
    btn.disabled = !voice;
  };
  input.addEventListener("change", () => setFile(input.files[0]));
  ["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (ev) => setFile(ev.dataTransfer.files[0]));

  api("/api/voices").then(({ voices, default: def }) => {
    const list = $("#voice-list", root);
    voice = localStorage.getItem("voice") || def;
    list.innerHTML = voices.map((v) => `
      <div class="voice ${v.id === voice ? "selected" : ""}" data-id="${v.id}">
        <div><strong>${esc(v.name)}</strong><small>${v.gender} · ${v.locale}</small></div>
        <button type="button" title="Ouvir amostra">▶</button>
      </div>`).join("");
    list.addEventListener("click", (ev) => {
      const card = ev.target.closest(".voice");
      if (!card) return;
      if (ev.target.closest("button")) {
        const b = ev.target.closest("button");
        b.textContent = "…";
        sampleAudio.src = `/api/voices/${card.dataset.id}/sample`;
        sampleAudio.play().catch(() => {}).finally(() => (b.textContent = "▶"));
        return;
      }
      voice = card.dataset.id;
      localStorage.setItem("voice", voice);
      list.querySelectorAll(".voice").forEach((c) => c.classList.toggle("selected", c === card));
      btn.disabled = !file;
    });
    btn.disabled = !file;
  });

  btn.addEventListener("click", () => {
    if (!file) return;
    btn.disabled = true; err.hidden = true;
    const prog = $("#upload-progress", root), fill = $("#upload-fill", root), label = $("#upload-label", root);
    prog.hidden = false;
    const form = new FormData();
    form.append("file", file);
    form.append("voice", voice);
    xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/documents");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const p = e.loaded / e.total;
      fill.style.width = `${p * 100}%`;
      label.textContent = p < 1 ? `Enviando… ${Math.round(p * 100)}%` : "Processando envio…";
    };
    xhr.onload = () => {
      if (xhr.status === 201) {
        location.hash = `#/convertendo/${JSON.parse(xhr.responseText).id}`;
      } else {
        let msg = "Falha no envio.";
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
        err.textContent = msg; err.hidden = false; prog.hidden = true; btn.disabled = false;
      }
    };
    xhr.onerror = () => { err.textContent = "Erro de conexão durante o envio."; err.hidden = false; prog.hidden = true; btn.disabled = false; };
    xhr.send(form);
  });

  return () => { sampleAudio.pause(); if (xhr && xhr.readyState !== 4) xhr.abort(); };
}

// ---------------------------------------------------------------- tela: conversão
function renderConverting(id) {
  const root = mount("#tpl-converting");
  const order = ["queued", "extracting", "synthesizing", "finalizing", "ready"];
  const samples = [];
  let timer = null, stopped = false;

  const phaseText = (d) => ({
    queued: d.queue_position ? `Na fila — ${d.queue_position} arquivo(s) antes deste` : "Na fila…",
    extracting: "Lendo e organizando o texto do PDF…",
    synthesizing: "Gerando a narração com voz natural…",
    finalizing: "Juntando as partes do áudio…",
    ready: "Concluído!",
    error: "Ocorreu um problema na conversão",
  }[d.status]);

  const eta = (d) => {
    if (d.status !== "synthesizing") return "";
    samples.push([Date.now(), d.chunks_done]);
    while (samples.length > 40) samples.shift();
    if (samples.length < 3) return "Calculando tempo…";
    const [t0, c0] = samples[0], [t1, c1] = samples[samples.length - 1];
    if (c1 <= c0) return "Calculando tempo…";
    const rate = (c1 - c0) / ((t1 - t0) / 1000);
    return `≈ ${fmtDuration((d.chunks_total - d.chunks_done) / rate)} restantes`;
  };

  const tick = async () => {
    if (stopped) return;
    try {
      const d = await api(`/api/documents/${id}`);
      $("#conv-title", root).textContent = d.title;
      $("#conv-phase", root).textContent = phaseText(d);
      const p = progressOf(d);
      $("#conv-fill", root).style.width = `${p * 100}%`;
      $("#conv-percent", root).textContent = `${Math.floor(p * 100)}%`;
      $("#conv-detail", root).textContent =
        d.status === "extracting" && d.pages ? `Página ${d.pages_done} de ${d.pages}` :
        d.chunks_total ? `Trecho ${d.chunks_done} de ${d.chunks_total} · ${d.pages} páginas` : "";
      $("#conv-eta", root).textContent = eta(d);
      const idx = order.indexOf(d.status);
      root.querySelectorAll("#conv-steps li").forEach((li) => {
        const i = order.indexOf(li.dataset.step);
        li.classList.toggle("done", idx > i || d.status === "ready");
        li.classList.toggle("current", idx === i && d.status !== "ready");
      });
      root.classList.toggle("failed", d.status === "error");
      $("#conv-error", root).hidden = d.status !== "error";
      if (d.status === "error") {
        $("#conv-error-msg", root).textContent = d.error || "Erro desconhecido.";
        return;
      }
      if (d.status === "ready") {
        setTimeout(() => { if (!stopped) location.hash = `#/ouvir/${id}`; }, 900);
        return;
      }
    } catch (e) {
      $("#conv-phase", root).textContent = `Sem conexão com o servidor (${e.message}) — tentando novamente…`;
    }
    timer = setTimeout(tick, 1500);
  };

  $("#conv-retry", root).addEventListener("click", async () => {
    await api(`/api/documents/${id}/retry`, { method: "POST" });
    samples.length = 0;
    tick();
  });

  tick();
  return () => { stopped = true; clearTimeout(timer); };
}

// ---------------------------------------------------------------- tela: player
function renderPlayer(id) {
  const root = mount("#tpl-player");
  const audio = $("#audio", root), seek = $("#pl-seek", root), playBtn = $("#pl-play", root);
  const textBox = $("#pl-text", root), pageSel = $("#pl-page", root), speed = $("#pl-speed", root);
  let timeline = [], duration = 0, active = -1, seeking = false, lastSave = 0, doc = null;
  const posKey = `pos:${id}`;

  const savePos = () => localStorage.setItem(posKey, String(audio.currentTime));

  const findIndex = (t) => {
    let lo = 0, hi = timeline.length - 1, ans = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (timeline[mid].start <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  };

  const highlight = (t, scroll = true) => {
    if (!timeline.length) return;
    const i = findIndex(t);
    if (i === active) return;
    textBox.children[active]?.classList.remove("active");
    active = i;
    const el = textBox.children[i];
    el?.classList.add("active");
    if (el && scroll) textBox.scrollTop = el.offsetTop - textBox.offsetTop - 40;
    $("#pl-curpage", root).textContent = `Página ${timeline[i].page}`;
    const pm = [...pageSel.options].filter((o) => Number(o.dataset.time) <= t + 0.01).pop();
    if (pm) pageSel.value = pm.value;
  };

  (async () => {
    try {
      doc = await api(`/api/documents/${id}`);
    } catch { location.hash = "#/biblioteca"; return; }
    if (doc.status !== "ready") { location.hash = `#/convertendo/${id}`; return; }

    duration = doc.duration;
    $("#pl-title", root).textContent = doc.title;
    $("#pl-meta", root).textContent = `${doc.pages} páginas · ${fmtDuration(doc.duration)} · ${fmtSize(doc.file_size)}`;
    const cover = $("#pl-cover", root);
    cover.setAttribute("style", coverStyle(doc.title));
    cover.textContent = doc.title;
    $("#pl-dur", root).textContent = fmtTime(duration);
    seek.max = Math.ceil(duration * 10);
    $("#pl-read", root).href = `#/ler/${id}`;
    $("#pl-download", root).href = `/api/documents/${id}/audio?download=true`;
    pageSel.innerHTML = doc.page_marks.map((m) => `<option value="${m.page}" data-time="${m.time}">Página ${m.page}</option>`).join("");

    speed.value = localStorage.getItem("speed") || "1";
    audio.src = `/api/documents/${id}/audio`;
    audio.playbackRate = Number(speed.value);
    const saved = Number(localStorage.getItem(posKey) || 0);
    if (saved > 0 && saved < duration - 2) audio.currentTime = saved;

    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: doc.title, artist: "Leitor de PDF" });
      navigator.mediaSession.setActionHandler("seekbackward", () => (audio.currentTime -= 15));
      navigator.mediaSession.setActionHandler("seekforward", () => (audio.currentTime += 30));
    }

    timeline = await api(`/api/documents/${id}/timeline`);
    textBox.innerHTML = timeline.map((t, i) => `<p data-i="${i}">${esc(t.text)}</p>`).join("");
    highlight(audio.currentTime || saved);
  })();

  playBtn.addEventListener("click", () => (audio.paused ? audio.play() : audio.pause()));
  $("#pl-back", root).addEventListener("click", () => (audio.currentTime = Math.max(0, audio.currentTime - 15)));
  $("#pl-fwd", root).addEventListener("click", () => (audio.currentTime = Math.min(duration, audio.currentTime + 30)));
  speed.addEventListener("change", () => { audio.playbackRate = Number(speed.value); localStorage.setItem("speed", speed.value); });
  pageSel.addEventListener("change", () => {
    audio.currentTime = Number(pageSel.selectedOptions[0].dataset.time);
    highlight(audio.currentTime);
  });
  textBox.addEventListener("click", (ev) => {
    const p = ev.target.closest("p");
    if (!p) return;
    audio.currentTime = timeline[Number(p.dataset.i)].start;
    highlight(audio.currentTime, false);
    audio.play();
  });

  audio.addEventListener("play", () => (playBtn.textContent = "❚❚"));
  audio.addEventListener("pause", () => { playBtn.textContent = "▶"; savePos(); });
  audio.addEventListener("ended", () => localStorage.removeItem(posKey));
  audio.addEventListener("timeupdate", () => {
    const t = audio.currentTime;
    if (!seeking) seek.value = Math.round(t * 10);
    $("#pl-cur", root).textContent = fmtTime(t);
    highlight(t);
    if (Date.now() - lastSave > 3000) { lastSave = Date.now(); savePos(); }
  });
  seek.addEventListener("input", () => { seeking = true; $("#pl-cur", root).textContent = fmtTime(seek.value / 10); });
  seek.addEventListener("change", () => { audio.currentTime = seek.value / 10; seeking = false; });

  const onKey = (ev) => {
    if (ev.target.matches("input, select, textarea")) return;
    if (ev.code === "Space") { ev.preventDefault(); playBtn.click(); }
    else if (ev.code === "ArrowLeft") audio.currentTime -= 5;
    else if (ev.code === "ArrowRight") audio.currentTime += 5;
  };
  document.addEventListener("keydown", onKey);

  return () => {
    if (audio.currentTime) savePos();
    audio.pause();
    audio.removeAttribute("src");
    document.removeEventListener("keydown", onKey);
  };
}

// ---------------------------------------------------------------- tela: biblioteca
function renderLibrary() {
  const root = mount("#tpl-library");
  const grid = $("#lib-grid", root), search = $("#lib-search", root);
  let docs = [], timer = null, stopped = false;

  const card = (d) => {
    const working = isWorking(d.status);
    const badge = d.status === "ready" ? "ready" : d.status === "error" ? "error" : "working";
    const meta = d.status === "ready"
      ? `${d.pages} pág. · ${fmtDuration(d.duration)}`
      : working ? `${Math.floor(progressOf(d) * 100)}% concluído` : esc(d.error || "");
    const pos = Number(localStorage.getItem(`pos:${d.id}`) || 0);
    const listened = d.status === "ready" && pos > 0 ? `<div class="bar"><div class="fill" style="width:${(pos / d.duration) * 100}%"></div></div>` : "";
    return `
      <article class="lib-card" data-id="${d.id}">
        <div class="cover" style="${coverStyle(d.title)}">${esc(d.title)}</div>
        <div class="lib-body">
          <span class="badge ${badge}">${STATUS_LABEL[d.status]}</span>
          <h3 title="${esc(d.title)}">${esc(d.title)}</h3>
          <div class="lib-meta">${meta}</div>
          ${listened}
          <div class="lib-actions">
            ${d.status === "ready" ? `<a class="btn primary" href="#/ouvir/${d.id}">🎧 Ouvir</a>` : ""}
            ${working || d.status === "error" ? `<a class="btn" href="#/convertendo/${d.id}">${working ? "⏳ Progresso" : "↻ Detalhes"}</a>` : ""}
            <a class="btn" href="#/ler/${d.id}">📖 Ler</a>
            <button class="btn danger" data-del="${d.id}" title="Excluir">🗑</button>
          </div>
        </div>
      </article>`;
  };

  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const list = docs.filter((d) => !q || d.title.toLowerCase().includes(q) || d.filename.toLowerCase().includes(q));
    grid.innerHTML = list.map(card).join("");
    $("#lib-empty", root).hidden = docs.length > 0;
  };

  const load = async () => {
    if (stopped) return;
    try { docs = (await api("/api/documents")).documents; draw(); } catch {}
    if (docs.some((d) => isWorking(d.status))) timer = setTimeout(load, 2500);
  };

  search.addEventListener("input", draw);
  grid.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-del]");
    if (!b) return;
    const d = docs.find((x) => x.id === b.dataset.del);
    if (!confirm(`Excluir "${d.title}" da biblioteca? O PDF e o áudio serão removidos.`)) return;
    await api(`/api/documents/${d.id}`, { method: "DELETE" });
    localStorage.removeItem(`pos:${d.id}`);
    docs = docs.filter((x) => x.id !== d.id);
    draw();
  });

  load();
  return () => { stopped = true; clearTimeout(timer); };
}

// ---------------------------------------------------------------- tela: leitor PDF
function renderReader(id) {
  const root = mount("#tpl-reader");
  api(`/api/documents/${id}`).then((d) => {
    $("#rd-title", root).textContent = d.title;
    const listen = $("#rd-listen", root);
    if (d.status === "ready") listen.href = `#/ouvir/${id}`;
    else { listen.href = `#/convertendo/${id}`; listen.textContent = "⏳ Conversão"; }
    const frame = $("#rd-frame", root);
    if (navigator.pdfViewerEnabled) {
      frame.src = `/api/documents/${id}/pdf`; // Windows (WebView2): visualizador nativo
      return;
    }
    // Janela sem visualizador de PDF (WebKitGTK no Linux): páginas desenhadas pelo servidor.
    const pages = document.createElement("div");
    pages.className = "pdf-pages";
    frame.replaceWith(pages);
    const w = Math.round(Math.min(1100, pages.clientWidth || 900) * (window.devicePixelRatio || 1));
    api(`/api/documents/${id}/pages`).then(({ count }) => {
      pages.innerHTML = Array.from({ length: count }, (_, i) =>
        `<img loading="lazy" decoding="async" alt="Página ${i + 1}" src="/api/documents/${id}/page/${i + 1}?w=${w}" />`
      ).join("");
    });
  }).catch(() => (location.hash = "#/biblioteca"));
}

// Sinal de vida: no Linux o programa roda no navegador e se encerra sozinho
// quando nenhuma aba estiver aberta (e não houver conversão em andamento).
setInterval(() => fetch("/api/ping").catch(() => {}), 20000);

router();
