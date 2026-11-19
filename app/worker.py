"""Fila de conversão PDF → áudio.

Pipeline de cada documento:
1. extração do texto (todas as páginas) e divisão em trechos;
2. síntese de cada trecho em paralelo (limitado), salvando cada parte em disco;
3. concatenação das partes em um único MP3 + linha do tempo (texto/página).

Como cada etapa é salva em disco, uma conversão interrompida (queda de rede,
servidor reiniciado) continua de onde parou, sem refazer o que já foi feito.
Isso permite converter PDFs muito grandes com segurança.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from . import db
from .config import LIBRARY_DIR, TTS_CONCURRENCY
from .pdf_text import build_chunks, extract_paragraphs
from .tts import MP3_BYTES_PER_SECOND, synthesize_to_file

log = logging.getLogger(__name__)


class NoTextError(RuntimeError):
    pass


def doc_dir(doc_id: str) -> Path:
    return LIBRARY_DIR / doc_id


class Converter:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.current: dict[str, asyncio.Task] = {}
        self._runner: asyncio.Task | None = None

    def start(self) -> None:
        for d in db.list_active():  # retoma conversões interrompidas
            db.update(d["id"], status="queued")
            self.queue.put_nowait(d["id"])
        self._runner = asyncio.create_task(self._run())

    async def stop(self) -> None:
        for t in list(self.current.values()):
            t.cancel()
        if self._runner:
            self._runner.cancel()

    def enqueue(self, doc_id: str) -> None:
        self.queue.put_nowait(doc_id)

    def cancel(self, doc_id: str) -> None:
        task = self.current.get(doc_id)
        if task:
            task.cancel()

    async def _run(self) -> None:
        while True:
            doc_id = await self.queue.get()
            if not db.get(doc_id):  # removido enquanto estava na fila
                continue
            task = asyncio.create_task(self._process(doc_id))
            self.current[doc_id] = task
            try:
                await task
            except asyncio.CancelledError:
                if self._runner and self._runner.cancelling():
                    raise
                log.info("Conversão %s cancelada", doc_id)
            except NoTextError as exc:
                db.update(doc_id, status="error", error=str(exc))
            except Exception as exc:
                log.exception("Falha ao converter %s", doc_id)
                db.update(doc_id, status="error", error=str(exc) or exc.__class__.__name__)
            finally:
                self.current.pop(doc_id, None)

    # ------------------------------------------------------------------ etapas
    async def _process(self, doc_id: str) -> None:
        folder = doc_dir(doc_id)
        doc = db.get(doc_id)
        chunks = await self._extract(doc_id, folder)
        await self._synthesize(doc_id, folder, chunks, doc["voice"])
        await asyncio.to_thread(self._finalize, doc_id, folder, chunks)

    async def _extract(self, doc_id: str, folder: Path) -> list[dict]:
        chunks_file = folder / "chunks.json"
        if chunks_file.exists():
            return json.loads(chunks_file.read_text(encoding="utf-8"))

        db.update(doc_id, status="extracting", error=None)
        last = 0.0

        def progress(done: int, total: int) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last > 0.5 or done == total:
                last = now
                db.update(doc_id, pages=total, pages_done=done)

        paragraphs, pages = await asyncio.to_thread(
            extract_paragraphs, str(folder / "source.pdf"), progress
        )
        chunks = [c.__dict__ for c in build_chunks(paragraphs)]
        if not chunks:
            raise NoTextError(
                "Este PDF não possui texto selecionável (provavelmente é digitalizado/imagem)."
            )
        chunks_file.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        db.update(
            doc_id, pages=pages, pages_done=pages,
            characters=sum(len(c["text"]) for c in chunks),
        )
        return chunks

    async def _synthesize(self, doc_id: str, folder: Path, chunks: list[dict], voice: str) -> None:
        parts = folder / "parts"
        parts.mkdir(exist_ok=True)
        pending = [c for c in chunks if not (parts / f"{c['index']:06d}.mp3").exists()]
        done = len(chunks) - len(pending)
        db.update(doc_id, status="synthesizing", chunks_total=len(chunks), chunks_done=done, error=None)

        it = iter(pending)

        async def worker() -> None:
            nonlocal done
            for c in it:
                await synthesize_to_file(c["text"], voice, str(parts / f"{c['index']:06d}.mp3"))
                done += 1
                db.update(doc_id, chunks_done=done)

        workers = [asyncio.create_task(worker()) for _ in range(TTS_CONCURRENCY)]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

    def _finalize(self, doc_id: str, folder: Path, chunks: list[dict]) -> None:
        db.update(doc_id, status="finalizing")
        parts = folder / "parts"
        tmp = folder / "audio.mp3.part"
        timeline, page_marks, seen_pages = [], [], set()
        position = 0.0
        with open(tmp, "wb") as out:
            for c in chunks:
                part = parts / f"{c['index']:06d}.mp3"
                timeline.append({"start": round(position, 2), "page": c["page"], "text": c["text"]})
                if c["page"] not in seen_pages:
                    seen_pages.add(c["page"])
                    page_marks.append({"page": c["page"], "time": round(position, 2)})
                with open(part, "rb") as src:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                position += part.stat().st_size / MP3_BYTES_PER_SECOND
        tmp.replace(folder / "audio.mp3")
        (folder / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
        shutil.rmtree(parts, ignore_errors=True)
        db.update(
            doc_id, status="ready", duration=round(position, 2),
            file_size=(folder / "audio.mp3").stat().st_size, page_marks=page_marks, error=None,
        )


converter = Converter()
