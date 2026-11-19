"""Síntese de voz neural (Microsoft Edge TTS) com validação e novas tentativas.

Cada trecho é sintetizado pedindo os eventos de "WordBoundary": o serviço
informa cada palavra que realmente foi falada. Comparamos isso com o texto
enviado e, se faltar conteúdo, o trecho é sintetizado novamente — assim o
áudio final não fica com palavras faltando.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from dataclasses import dataclass

import edge_tts

log = logging.getLogger(__name__)

# Formato padrão do edge-tts: MP3 24 kHz, 48 kbps CBR, mono.
# Por ser CBR, a duração pode ser calculada pelo tamanho do arquivo e os
# trechos podem ser concatenados diretamente (sem ffmpeg).
MP3_BYTES_PER_SECOND = 48_000 / 8

VOICES = [
    {"id": "pt-BR-FranciscaNeural", "name": "Francisca", "gender": "Feminina", "locale": "pt-BR"},
    {"id": "pt-BR-AntonioNeural", "name": "Antônio", "gender": "Masculina", "locale": "pt-BR"},
    {"id": "pt-BR-ThalitaMultilingualNeural", "name": "Thalita", "gender": "Feminina", "locale": "pt-BR"},
    {"id": "pt-PT-RaquelNeural", "name": "Raquel (Portugal)", "gender": "Feminina", "locale": "pt-PT"},
    {"id": "pt-PT-DuarteNeural", "name": "Duarte (Portugal)", "gender": "Masculina", "locale": "pt-PT"},
]
VOICE_IDS = {v["id"] for v in VOICES}
DEFAULT_VOICE = "pt-BR-FranciscaNeural"

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


async def _synthesize_once(text: str, voice: str, rate: str) -> tuple[bytes, float]:
    """Uma chamada ao serviço. Devolve (áudio, cobertura 0..1).

    A cobertura compara as palavras que o serviço diz ter falado com o texto
    enviado. Se o áudio terminar antes da última palavra anunciada (resposta
    cortada no meio), a cobertura é zerada para forçar nova tentativa."""
    communicate = edge_tts.Communicate(
        text, voice, rate=rate, boundary="WordBoundary",
        connect_timeout=20, receive_timeout=90,
    )
    audio = bytearray()
    words: list[str] = []
    last_word_end = 0.0
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            words.append(chunk.get("text", ""))
            last_word_end = (chunk.get("offset", 0) + chunk.get("duration", 0)) / 1e7
    coverage = _coverage(text, words)
    if words and len(audio) / MP3_BYTES_PER_SECOND < last_word_end - 0.5:
        coverage = 0.0  # áudio truncado
    return bytes(audio), coverage


async def _best_of(text: str, voice: str, rate: str, attempts: int) -> tuple[bytes | None, float, Exception | None]:
    """Tenta até `attempts` vezes; para assim que o texto sair completo."""
    best: tuple[bytes, float] | None = None
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            audio, coverage = await _synthesize_once(text, voice, rate)
            if not audio:
                raise SynthesisError("serviço de voz retornou áudio vazio")
            if best is None or coverage > best[1]:
                best = (audio, coverage)
            if coverage >= MIN_COVERAGE:
                break
            log.warning("Trecho incompleto (%.0f%% falado), tentativa %d/%d", coverage * 100, attempt, attempts)
        except Exception as exc:  # rede, timeout, limite de requisições...
            last_error = exc
            log.warning("Falha na síntese (tentativa %d/%d): %s", attempt, attempts, exc)
        if attempt < attempts:
            await asyncio.sleep(min(30, 1.5 * 2 ** (attempt - 1)))
    if best is None:
        return None, 0.0, last_error
    return best[0], best[1], last_error


def _split_sentences(text: str) -> list[str]:
    from .pdf_text import _SENTENCE_SPLIT_RE, _split_long

    out: list[str] = []
    for line in text.splitlines():
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            if sentence.strip():
                out.extend(_split_long(sentence.strip(), 300))
    return out


async def synthesize_to_file(
    text: str, voice: str, out_path: str, rate: str = "+0%"
) -> SynthResult:
    """Sintetiza `text` em `out_path` (escrita atômica) garantindo o texto completo.

    1. Até 4 tentativas com o trecho inteiro.
    2. Se continuar incompleto, sintetiza frase a frase (pedidos menores falham
       menos), verificando cada frase com até 6 tentativas.
    3. Se mesmo assim alguma frase não for lida por completo, gera erro — a
       conversão pausa e pode ser retomada, mas nunca pula texto em silêncio.
    """
    text = prepare_for_speech(text)
    audio, coverage, error = await _best_of(text, voice, rate, attempts=4)
    split = False
    if audio is None or coverage < MIN_COVERAGE:
        split = True
        log.warning("Trecho continua incompleto; sintetizando frase a frase")
        pieces: list[bytes] = []
        coverages: list[float] = []
        for sentence in _split_sentences(text):
            part, cov, err = await _best_of(sentence, voice, rate, attempts=MAX_ATTEMPTS)
            if part is None or cov < MIN_COVERAGE:
                motivo = f"erro de conexão: {err}" if part is None else f"só {cov:.0%} foi falado"
                raise SynthesisError(f'O serviço de voz não conseguiu ler a frase "{sentence[:80]}…" ({motivo}).')
            pieces.append(part)
            coverages.append(cov)
        audio, coverage = b"".join(pieces), min(coverages, default=1.0)

    tmp = out_path + ".part"
    with open(tmp, "wb") as f:
        f.write(audio)
    os.replace(tmp, out_path)
    return SynthResult(size=len(audio), coverage=coverage, split=split)


async def voice_sample(voice: str) -> bytes:
    text = (
        "Olá! Esta é uma amostra da minha voz. "
        "Vou ler o seu documento com calma, respeitando a pontuação e cada palavra do texto."
    )
    audio, _ = await _synthesize_once(text, voice, "+0%")
    return audio
