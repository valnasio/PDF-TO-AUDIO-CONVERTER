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
    communicate = edge_tts.Communicate(
        text, voice, rate=rate, boundary="WordBoundary",
        connect_timeout=20, receive_timeout=90,
    )
    audio = bytearray()
    words: list[str] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            words.append(chunk.get("text", ""))
    return bytes(audio), _coverage(text, words)


async def synthesize_to_file(
    text: str, voice: str, out_path: str, rate: str = "+0%"
) -> SynthResult:
    """Sintetiza `text` em `out_path` (escrita atômica), com validação e retries."""
    text = prepare_for_speech(text)
    best: tuple[bytes, float] | None = None
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            audio, coverage = await _synthesize_once(text, voice, rate)
            if not audio:
                raise SynthesisError("serviço de voz retornou áudio vazio")
            if best is None or coverage > best[1]:
                best = (audio, coverage)
            if coverage >= MIN_COVERAGE:
                break
            log.warning("Trecho incompleto (%.0f%% falado), tentativa %d", coverage * 100, attempt)
        except Exception as exc:  # rede, timeout, limite de requisições...
            last_error = exc
            log.warning("Falha na síntese (tentativa %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(min(30, 1.5 * 2 ** (attempt - 1)))

    if best is None:
        raise SynthesisError(f"Não foi possível sintetizar o trecho: {last_error}")

    audio, coverage = best
    if coverage < 0.9:
        raise SynthesisError(
            f"O serviço de voz não leu o trecho por completo ({coverage:.0%}) após {MAX_ATTEMPTS} tentativas"
        )
    tmp = out_path + ".part"
    with open(tmp, "wb") as f:
        f.write(audio)
    os.replace(tmp, out_path)
    return SynthResult(size=len(audio), coverage=coverage)


async def voice_sample(voice: str) -> bytes:
    text = (
        "Olá! Esta é uma amostra da minha voz. "
        "Vou ler o seu documento com calma, respeitando a pontuação e cada palavra do texto."
    )
    audio, _ = await _synthesize_once(text, voice, "+0%")
    return audio
