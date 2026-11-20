"""Síntese de voz com dois motores:

- Vozes no computador (padrão): Piper (Faber, Cadu, Jeff), neurais, rodando no
  próprio computador. Sem limite de pedidos e sem internet depois de baixar a
  voz — ideal para livros grandes.
- Vozes Microsoft (Edge TTS): online, maior qualidade. Cada trecho é conferido
  palavra por palavra pelos eventos "WordBoundary" e refeito se faltar algo;
  o serviço pode limitar livros muito grandes.

Os dois produzem MP3 CBR 48 kbps mono, então a duração sai do tamanho do
arquivo e os trechos podem ser concatenados diretamente (sem ffmpeg).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import edge_tts

from .config import DATA_DIR

log = logging.getLogger(__name__)

# Formato padrão do edge-tts: MP3 24 kHz, 48 kbps CBR, mono.
# Por ser CBR, a duração pode ser calculada pelo tamanho do arquivo e os
# trechos podem ser concatenados diretamente (sem ffmpeg).
MP3_BYTES_PER_SECOND = 48_000 / 8

LOCAL_PREFIX = "piper:"
LOCAL_VOICES = [
    {"id": "piper:pt_BR-faber-medium", "name": "Faber", "gender": "Masculina", "locale": "pt-BR", "engine": "local",
     "path": "pt/pt_BR/faber/medium/pt_BR-faber-medium"},
    {"id": "piper:pt_BR-cadu-medium", "name": "Cadu", "gender": "Masculina", "locale": "pt-BR", "engine": "local",
     "path": "pt/pt_BR/cadu/medium/pt_BR-cadu-medium"},
    {"id": "piper:pt_BR-jeff-medium", "name": "Jeff", "gender": "Masculina", "locale": "pt-BR", "engine": "local",
     "path": "pt/pt_BR/jeff/medium/pt_BR-jeff-medium"},
]
MICROSOFT_VOICES = [
    {"id": "pt-BR-FranciscaNeural", "name": "Francisca", "gender": "Feminina", "locale": "pt-BR", "engine": "microsoft"},
    {"id": "pt-BR-AntonioNeural", "name": "Antônio", "gender": "Masculina", "locale": "pt-BR", "engine": "microsoft"},
    {"id": "pt-BR-ThalitaMultilingualNeural", "name": "Thalita", "gender": "Feminina", "locale": "pt-BR", "engine": "microsoft"},
    {"id": "pt-PT-RaquelNeural", "name": "Raquel (Portugal)", "gender": "Feminina", "locale": "pt-PT", "engine": "microsoft"},
    {"id": "pt-PT-DuarteNeural", "name": "Duarte (Portugal)", "gender": "Masculina", "locale": "pt-PT", "engine": "microsoft"},
]
VOICES = [{k: v for k, v in voice.items() if k != "path"} for voice in LOCAL_VOICES + MICROSOFT_VOICES]
VOICE_IDS = {v["id"] for v in VOICES}
DEFAULT_VOICE = "piper:pt_BR-faber-medium"
LOCAL_VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
VOICES_DIR = DATA_DIR / "vozes"


def is_local(voice: str) -> bool:
    return voice.startswith(LOCAL_PREFIX)


def voice_name(voice: str) -> str:
    return next((v["name"] for v in VOICES if v["id"] == voice), voice)

MIN_COVERAGE = 0.97
MAX_ATTEMPTS = 6


class SynthesisError(RuntimeError):
    pass


@dataclass
class SynthResult:
    size: int
    coverage: float
    split: bool = False  # precisou ser refeito frase a frase

    @property
    def duration(self) -> float:
        return self.size / MP3_BYTES_PER_SECOND


def _alnum(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if ch.isalnum())


def _coverage(sent: str, spoken_words: list[str]) -> float:
    expected = _alnum(sent)
    if not expected:
        return 1.0
    return min(1.0, len(_alnum("".join(spoken_words))) / len(expected))


def prepare_for_speech(text: str) -> str:
    """Pequenos ajustes que deixam a leitura mais natural."""
    # Travessão de diálogo no início da linha → pausa curta, não "menos".
    text = re.sub(r"(^|\n)\s*[-–—]\s+", r"\1— ", text)
    # Reticências repetidas
    text = re.sub(r"\.{4,}", "...", text)
    text = re.sub(r"\s+\.\.\.", "...", text)
    return text


class ServiceUnavailableError(SynthesisError):
    """O serviço de voz recusou ou não respondeu por vários minutos seguidos."""


class Hooks:
    """Avisos da síntese para quem está convertendo (tela e controle de ritmo)."""

    def trouble(self, message: str) -> None:  # serviço instável: tentando de novo
        pass

    def ok(self) -> None:  # serviço respondeu normalmente
        pass


NO_HOOKS = Hooks()
# Esperas entre tentativas quando o serviço falha (rede, 503, sem resposta, prazo
# estourado). Somam ~8 minutos de paciência antes de desistir com aviso claro.
SERVICE_BACKOFF = [5, 10, 20, 40, 60, 90, 120, 120]


def _attempt_timeout(text: str) -> float:
    """Prazo máximo de um pedido. Normalmente leva de 2 a 15 s; sem prazo, uma
    conexão presa deixava a conversão parada para sempre."""
    return 40 + len(text) / 12


async def _synthesize_once(text: str, voice: str, rate: str) -> tuple[bytes, float]:
    """Uma chamada ao serviço. Devolve (áudio, cobertura 0..1).

    A cobertura compara as palavras que o serviço diz ter falado com o texto
    enviado. Se o áudio terminar antes da última palavra anunciada (resposta
    cortada no meio), a cobertura é zerada para forçar nova tentativa."""
    communicate = edge_tts.Communicate(
        text, voice, rate=rate, boundary="WordBoundary",
        connect_timeout=20, receive_timeout=60,
    )
    audio = bytearray()
    words: list[str] = []
    last_word_end = 0.0

    async def run() -> None:
        nonlocal last_word_end
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append(chunk.get("text", ""))
                last_word_end = (chunk.get("offset", 0) + chunk.get("duration", 0)) / 1e7

    try:
        await asyncio.wait_for(run(), timeout=_attempt_timeout(text))
    except asyncio.TimeoutError:
        raise SynthesisError(f"o serviço de voz não respondeu em {int(_attempt_timeout(text))} s") from None
    if not audio:
        raise SynthesisError("o serviço de voz não devolveu áudio")
    coverage = _coverage(text, words)
    if words and len(audio) / MP3_BYTES_PER_SECOND < last_word_end - 0.5:
        coverage = 0.0  # áudio truncado
    return bytes(audio), coverage


def _describe(exc: Exception) -> str:
    msg = str(exc) or exc.__class__.__name__
    if "503" in msg:
        return "serviço da Microsoft sobrecarregado (503)"
    if "No audio was received" in msg:
        return "o serviço não devolveu áudio"
    if "getaddrinfo" in msg or "Cannot connect" in msg:
        return "sem conexão com a internet"
    return msg[:120]


async def _best_of(text: str, voice: str, rate: str, attempts: int, hooks: Hooks) -> tuple[bytes | None, float]:
    """Tenta até o texto sair completo.

    - Resposta incompleta (palavras faltando): até `attempts` tentativas rápidas.
    - Falha do serviço (rede, 503, sem resposta, prazo estourado): espera cada vez
      mais (até ~8 min no total) e avisa; depois disso, ServiceUnavailableError.
    """
    best: tuple[bytes, float] | None = None
    incomplete = 0
    failures = 0
    while True:
        try:
            audio, coverage = await _synthesize_once(text, voice, rate)
            hooks.ok()
            if best is None or coverage > best[1]:
                best = (audio, coverage)
            if coverage >= MIN_COVERAGE:
                break
            incomplete += 1
            log.warning("Trecho incompleto (%.0f%% falado), tentativa %d/%d", coverage * 100, incomplete, attempts)
            if incomplete >= attempts:
                break
            await asyncio.sleep(min(10, 1.5 * 2 ** (incomplete - 1)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if failures >= len(SERVICE_BACKOFF):
                raise ServiceUnavailableError(
                    f"O serviço de voz da Microsoft não está respondendo há vários minutos ({_describe(exc)}). "
                    "As páginas já convertidas continuam disponíveis; clique em Continuar para retomar de onde parou."
                ) from exc
            delay = SERVICE_BACKOFF[failures]
            failures += 1
            log.warning("Falha na síntese (%d/%d): %s; nova tentativa em %ds", failures, len(SERVICE_BACKOFF), exc, delay)
            hooks.trouble(f"Serviço de voz instável ({_describe(exc)}). Tentando de novo em {delay} s…")
            await asyncio.sleep(delay)
    if best is None:
        return None, 0.0
    return best


def _split_sentences(text: str) -> list[str]:
    from .pdf_text import _SENTENCE_SPLIT_RE, _split_long

    out: list[str] = []
    for line in text.splitlines():
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            if sentence.strip():
                out.extend(_split_long(sentence.strip(), 300))
    return out


async def synthesize_to_file(
    text: str, voice: str, out_path: str, rate: str = "+0%", hooks: Hooks = NO_HOOKS,
) -> SynthResult:
    """Sintetiza `text` em `out_path` (escrita atômica) garantindo o texto completo.

    1. Até 4 tentativas com o trecho inteiro (com paciência se o serviço falhar).
    2. Se continuar incompleto, sintetiza frase a frase (pedidos menores falham
       menos), verificando cada frase com até 6 tentativas.
    3. Se mesmo assim alguma frase não for lida por completo, ou se o serviço
       ficar fora do ar, gera erro com mensagem clara: a conversão pausa e pode
       ser retomada, mas nunca pula texto nem fica parada em silêncio.
    """
    text = prepare_for_speech(text)
    audio, coverage = await _best_of(text, voice, rate, 4, hooks)
    split = False
    if audio is None or coverage < MIN_COVERAGE:
        split = True
        log.warning("Trecho continua incompleto; sintetizando frase a frase")
        pieces: list[bytes] = []
        coverages: list[float] = []
        for sentence in _split_sentences(text):
            part, cov = await _best_of(sentence, voice, rate, MAX_ATTEMPTS, hooks)
            if part is None or cov < MIN_COVERAGE:
                raise SynthesisError(
                    f'O serviço de voz não conseguiu ler a frase "{sentence[:80]}…" por completo (só {cov:.0%} foi falado).'
                )
            pieces.append(part)
            coverages.append(cov)
        audio, coverage = b"".join(pieces), min(coverages, default=1.0)

    tmp = out_path + ".part"
    with open(tmp, "wb") as f:
        f.write(audio)
    os.replace(tmp, out_path)
    return SynthResult(size=len(audio), coverage=coverage, split=split)


# ---------------------------------------------------------------- vozes no computador (Piper)
_local_lock = threading.Lock()
_local_cache: dict = {}


def _local_files(voice: str) -> tuple[Path, Path]:
    name = voice[len(LOCAL_PREFIX):]
    return VOICES_DIR / f"{name}.onnx", VOICES_DIR / f"{name}.onnx.json"


def local_voice_ready(voice: str) -> bool:
    model, config = _local_files(voice)
    return model.exists() and config.exists() and model.stat().st_size > 1_000_000


def ensure_local_voice(voice: str, progress=None) -> None:
    """Baixa a voz (63 MB, uma única vez) para a pasta de dados. progress(0..1)."""
    if local_voice_ready(voice):
        return
    info = next(v for v in LOCAL_VOICES if v["id"] == voice)
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    model, config = _local_files(voice)
    for url, target, report in ((LOCAL_VOICES_URL + info["path"] + ".onnx.json", config, False),
                                (LOCAL_VOICES_URL + info["path"] + ".onnx", model, True)):
        tmp = target.with_name(target.name + ".part")
        try:
            with urllib.request.urlopen(url, timeout=60) as res, open(tmp, "wb") as out:
                total = int(res.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    block = res.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if report and progress and total:
                        progress(done / total)
            if total and tmp.stat().st_size != total:
                raise SynthesisError("o download da voz veio incompleto")
            tmp.replace(target)
        except SynthesisError:
            raise
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise SynthesisError(
                f"Não foi possível baixar a voz {info['name']} ({exc}). Verifique a internet e clique em Continuar; "
                "depois de baixada, a voz funciona sem internet."
            ) from exc


def _load_local(voice: str):
    with _local_lock:
        if voice not in _local_cache:
            from piper import PiperVoice  # carregado só quando usado

            model, config = _local_files(voice)
            _local_cache[voice] = PiperVoice.load(model, config_path=config)
        return _local_cache[voice]


def _to_mp3(pcm: bytes, sample_rate: int) -> bytes:
    import lameenc

    enc = lameenc.Encoder()
    enc.set_bit_rate(48)  # mesmo formato das vozes Microsoft: CBR 48 kbps mono
    enc.set_in_sample_rate(sample_rate)
    enc.set_channels(1)
    enc.set_quality(2)
    return bytes(enc.encode(pcm) + enc.flush())


def synthesize_local_to_file(text: str, voice: str, out_path: str) -> SynthResult:
    """Gera a fala no próprio computador (CPU). Chamar fora do loop (to_thread).

    Não há rede no caminho, então não existe resposta cortada; ainda assim, o
    áudio é conferido pela duração: fala muito curta para o tamanho do texto
    indica problema e vira erro, nunca texto pulado em silêncio."""
    text = prepare_for_speech(text)
    piper_voice = _load_local(voice)
    rate = piper_voice.config.sample_rate
    pcm = b"".join(chunk.audio_int16_bytes for chunk in piper_voice.synthesize(text))
    seconds = len(pcm) / 2 / rate
    letters = len(_alnum(text))
    if letters > 20 and seconds < letters * 0.02:  # normal: ~0,06 s por letra
        raise SynthesisError(f"a voz gerou só {seconds:.1f}s de fala para {letters} letras")
    audio = _to_mp3(pcm, rate)
    tmp = out_path + ".part"
    with open(tmp, "wb") as f:
        f.write(audio)
    os.replace(tmp, out_path)
    return SynthResult(size=len(audio), coverage=1.0)


async def voice_sample(voice: str) -> bytes:
    text = (
        "Olá! Esta é uma amostra da minha voz. "
        "Vou ler o seu documento com calma, respeitando a pontuação e cada palavra do texto."
    )
    if is_local(voice):
        def make() -> bytes:
            ensure_local_voice(voice)
            piper_voice = _load_local(voice)
            pcm = b"".join(chunk.audio_int16_bytes for chunk in piper_voice.synthesize(text))
            return _to_mp3(pcm, piper_voice.config.sample_rate)
        return await asyncio.to_thread(make)
    audio, _ = await _synthesize_once(text, voice, "+0%")
    return audio
