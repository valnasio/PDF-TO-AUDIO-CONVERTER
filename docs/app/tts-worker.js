// Gera a fala com a voz neural Piper (pt_BR Faber) dentro do navegador.
// Roda num Web Worker para a página não travar enquanto a voz é gerada.
//
// A voz (~63 MB) é baixada uma única vez e guardada no aparelho (OPFS). O modelo
// e o módulo de pronúncia ficam carregados na memória entre as frases, então
// cada frase custa só a inferência (sem recarregar 63 MB a cada chamada).
import * as ort from "https://esm.sh/onnxruntime-web@1.18.0";
import { createPiperPhonemize } from "https://cdn.jsdelivr.net/npm/@diffusionstudio/vits-web@1.0.3/dist/piper-DeOu3H9E.js";

const HF = "https://huggingface.co/diffusionstudio/piper-voices/resolve/main/";
const RHASSPY = "https://huggingface.co/rhasspy/piper-voices/resolve/main/";
const VOICES = {
  "pt_BR-faber-medium": HF + "pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx",
  "pt_BR-cadu-medium": RHASSPY + "pt/pt_BR/cadu/medium/pt_BR-cadu-medium.onnx",
  "pt_BR-jeff-medium": RHASSPY + "pt/pt_BR/jeff/medium/pt_BR-jeff-medium.onnx",
};
const PHONEMIZE = "https://cdn.jsdelivr.net/npm/@diffusionstudio/piper-wasm@1.0.0/build/piper_phonemize";
const DIR = "leitor-pdf-vozes";

ort.env.wasm.wasmPaths = "https://cdnjs.cloudflare.com/ajax/libs/onnxruntime-web/1.18.0/";
ort.env.wasm.numThreads = 1; // sem isolamento de origem (GitHub Pages) só há 1 thread por gerador

// ---------- armazenamento no aparelho (OPFS)
async function voiceDir() {
  const root = await navigator.storage.getDirectory();
  return root.getDirectoryHandle(DIR, { create: true });
}
async function readStored(name) {
  try {
    const file = await (await (await voiceDir()).getFileHandle(name)).getFile();
    return file.size ? await file.arrayBuffer() : null;
  } catch { return null; }
}
async function storedSize(name) {
  try { return (await (await (await voiceDir()).getFileHandle(name)).getFile()).size; } catch { return 0; }
}
async function writeStored(name, buffer) {
  try {
    const handle = await (await voiceDir()).getFileHandle(name, { create: true });
    if (handle.createSyncAccessHandle) { // disponível em workers em todos os navegadores atuais
      const access = await handle.createSyncAccessHandle();
      access.truncate(0);
      access.write(new Uint8Array(buffer), { at: 0 });
      access.flush();
      access.close();
    } else {
      const w = await handle.createWritable();
      await w.write(buffer);
      await w.close();
    }
    return true;
  } catch { return false; } // sem armazenamento: a voz fica só na memória desta sessão
}
async function fetchWithProgress(url, onProgress) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`download da voz falhou (HTTP ${res.status})`);
  const total = Number(res.headers.get("Content-Length")) || 0;
  if (!res.body || !total) return res.arrayBuffer();
  const reader = res.body.getReader();
  const out = new Uint8Array(total);
  let loaded = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    out.set(value, loaded);
    loaded += value.length;
    onProgress?.(loaded / total);
  }
  return out.buffer;
}

async function voiceFiles(voiceId, onProgress) {
  const path = VOICES[voiceId];
  const modelName = path.split("/").at(-1);
  let config = await readStored(`${modelName}.json`);
  if (!config) {
    config = await fetchWithProgress(`${path}.json`);
    await writeStored(`${modelName}.json`, config);
  }
  let model = await readStored(modelName);
  if (!model) {
    model = await fetchWithProgress(path, onProgress);
    await writeStored(modelName, model);
  }
  return { model, config: JSON.parse(new TextDecoder().decode(config)) };
}

// ---------- modelo carregado uma vez
const sessions = new Map();
function loadVoice(voiceId) {
  if (!sessions.has(voiceId)) {
    const p = (async () => {
      const { model, config } = await voiceFiles(voiceId);
      const session = await ort.InferenceSession.create(model);
      return { session, config };
    })();
    p.catch(() => sessions.delete(voiceId));
    sessions.set(voiceId, p);
  }
  return sessions.get(voiceId);
}

// ---------- pronúncia (eSpeak NG compilado para WebAssembly)
let phonemizer = null;
let printed = null;
function getPhonemizer() {
  if (!phonemizer) {
    phonemizer = createPiperPhonemize({
      print: (line) => printed?.(line),
      printErr: () => {},
      locateFile: (f) => (f.endsWith(".wasm") ? `${PHONEMIZE}.wasm` : f.endsWith(".data") ? `${PHONEMIZE}.data` : f),
    });
    phonemizer.catch(() => { phonemizer = null; });
  }
  return phonemizer;
}
async function phonemeIds(text, espeakVoice, retry = true) {
  const mod = await getPhonemizer();
  try {
    let ids = null;
    printed = (line) => { ids = JSON.parse(line).phoneme_ids; };
    mod.callMain(["-l", espeakVoice, "--input", JSON.stringify([{ text }]), "--espeak_data", "/espeak-ng-data"]);
    printed = null;
    if (!ids) throw new Error("pronúncia vazia");
    return ids;
  } catch (err) {
    printed = null;
    if (!retry) throw err;
    phonemizer = null; // módulo encerrado: cria outro e tenta de novo uma vez
    return phonemeIds(text, espeakVoice, false);
  }
}

function toWav(samples, sampleRate) {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const str = (o, s) => [...s].forEach((c, i) => view.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  str(36, "data"); view.setUint32(40, samples.length * 2, true);
  for (let i = 0, o = 44; i < samples.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([view.buffer], { type: "audio/wav" });
}

async function predict(text, voiceId) {
  const { session, config } = await loadVoice(voiceId);
  const ids = await phonemeIds(text.trim(), config.espeak.voice);
  const { noise_scale, length_scale, noise_w } = config.inference;
  const feeds = {
    input: new ort.Tensor("int64", BigInt64Array.from(ids.map(BigInt)), [1, ids.length]),
    input_lengths: new ort.Tensor("int64", BigInt64Array.from([BigInt(ids.length)]), [1]),
    scales: new ort.Tensor("float32", Float32Array.from([noise_scale, length_scale, noise_w]), [3]),
  };
  if (Object.keys(config.speaker_id_map || {}).length) feeds.sid = new ort.Tensor("int64", BigInt64Array.from([0n]), [1]);
  const { output } = await session.run(feeds);
  return toWav(output.data, config.audio.sample_rate);
}

// Um pedido por vez: o ONNX Runtime não aceita duas inferências simultâneas na
// mesma sessão (no Firefox, uma delas ficava presa para sempre).
let chain = Promise.resolve();
self.onmessage = (e) => {
  chain = chain.then(() => handle(e)).catch(() => {});
};

async function handle(e) {
  const { id, type, text, voiceId } = e.data;
  try {
    if (type === "download") {
      await voiceFiles(voiceId, (p) => self.postMessage({ id, progress: p }));
      self.postMessage({ id, result: true });
    } else if (type === "stored") {
      const name = VOICES[voiceId].split("/").at(-1);
      self.postMessage({ id, result: (await storedSize(name)) > 1_000_000 });
    } else if (type === "predict") {
      self.postMessage({ id, result: await predict(text, voiceId) });
    }
  } catch (err) {
    self.postMessage({ id, error: String((err && err.message) || err) });
  }
};
