// Voz natural gratuita que funciona em qualquer navegador: Piper (pt_BR Faber),
// executada no próprio aparelho via WebAssembly. Nada é enviado para servidores:
// só a voz é baixada uma vez (~63 MB) e guardada no navegador.

export const NEURAL_VOICES = [
  { id: "pt_BR-faber-medium", name: "Faber" },
  { id: "pt_BR-cadu-medium", name: "Cadu" },
  { id: "pt_BR-jeff-medium", name: "Jeff" },
];
export const NEURAL_ID = `neural:${NEURAL_VOICES[0].id}`;

export const neuralSupported = () =>
  typeof Worker !== "undefined" && typeof WebAssembly !== "undefined" && !!navigator.storage?.getDirectory;

let pool = null;

function makeWorker() {
  const w = new Worker(new URL("./tts-worker.js?v=2.6.0", import.meta.url), { type: "module" });
  let n = 0;
  const pending = new Map();
  w.onmessage = (e) => {
    const { id, progress, result, error } = e.data;
    const p = pending.get(id);
    if (!p) return;
    if (progress !== undefined) { p.onProgress?.(progress); return; }
    pending.delete(id);
    error ? p.reject(new Error(error)) : p.resolve(result);
  };
  w.onerror = (e) => {
    for (const p of pending.values()) p.reject(new Error(e.message || "falha no gerador de voz"));
    pending.clear();
  };
  w.call = (msg, onProgress) => new Promise((resolve, reject) => {
    const id = ++n;
    pending.set(id, { resolve, reject, onProgress });
    w.postMessage({ ...msg, id });
  });
  return w;
}

// Um segundo gerador em paralelo quando o aparelho aguenta: a fala sai ~2x mais
// rápido do que é tocada. Ele só é criado depois da primeira frase, para não
// disputar o processador com o carregamento inicial.
function wantedWorkers() {
  const cores = navigator.hardwareConcurrency || 2;
  const memory = navigator.deviceMemory || 4;
  return cores >= 4 && memory >= 4 ? 2 : 1;
}

function getPool() {
  if (!pool) pool = { workers: [makeWorker()], next: 0, ready: {}, warm: {}, firstDone: false };
  return pool;
}

/** Baixa a voz se preciso (onProgress de 0 a 1). */
export function prepareNeural(voiceId, onProgress) {
  const p = getPool();
  if (!p.ready[voiceId]) {
    p.ready[voiceId] = p.workers[0].call({ type: "download", voiceId }, onProgress).then(() => true);
    p.ready[voiceId].catch(() => { delete p.ready[voiceId]; });
  }
  return p.ready[voiceId];
}

/** Se a voz já está guardada no aparelho, carrega o modelo em segundo plano
 *  (sem baixar nada), para a fala começar logo ao apertar ▶. */
export function warmNeural(voiceId) {
  const p = getPool();
  if (!p.warm[voiceId]) {
    p.warm[voiceId] = p.workers[0].call({ type: "stored", voiceId }).then((stored) => {
      if (!stored) return false;
      const t0 = performance.now();
      return p.workers[0].call({ type: "predict", voiceId, text: "Olá." })
        .then(() => { globalThis.__ttsWarmMs = Math.round(performance.now() - t0); return true; });
    }).catch(() => false);
  }
  return p.warm[voiceId];
}

/** Carrega a voz num gerador extra em segundo plano; ele só passa a receber
 *  frases dessa voz depois de pronto (carregar leva até ~15 s em alguns
 *  navegadores, e a leitura não pode ficar esperando por isso). */
function warmExtra(w, voiceId) {
  w.voices ||= new Map();
  if (w.voices.has(voiceId)) return;
  const state = { ready: false };
  w.voices.set(voiceId, state);
  w.call({ type: "predict", voiceId, text: "Olá." }).then(
    () => { state.ready = true; },
    () => { w.voices.delete(voiceId); },
  );
}

/** Gera a fala de um texto. Devolve um Blob WAV. */
export function synthNeural(voiceId, text) {
  const p = getPool();
  if (p.firstDone && p.workers.length < wantedWorkers()) p.workers.push(makeWorker());
  p.workers.slice(1).forEach((w) => warmExtra(w, voiceId));
  // o primeiro gerador sempre atende; os extras, só quando já têm esta voz carregada
  const usable = p.workers.filter((w, i) => i === 0 || w.voices?.get(voiceId)?.ready);
  const w = usable[p.next++ % usable.length];
  const wi = p.workers.indexOf(w);
  const t0 = performance.now();
  const job = w.call({ type: "predict", voiceId, text });
  job.then(() => {
    p.firstDone = true;
    // diagnóstico leve (usado pelos testes automáticos)
    (globalThis.__ttsLog ||= []).push({ w: wi, chars: text.length, ms: Math.round(performance.now() - t0) });
  }, () => {});
  return job;
}

/** Divide uma frase longa nas vírgulas, para a voz começar a falar mais rápido. */
export function splitForQuickStart(text) {
  if (text.length <= 90) return [text];
  const parts = text.split(/(?<=[,;:])\s+/);
  const out = [];
  for (const p of parts) {
    if (out.length && (out[out.length - 1].length < 40 || p.length < 25)) out[out.length - 1] += " " + p;
    else out.push(p);
  }
  return out;
}
