"""Fila de conversão PDF → áudio.

Pipeline de cada documento:
1. extração do texto e organização por página (plano salvo em plan.json);
2. síntese de cada trecho em paralelo (limitado), salvando cada parte em disco,
   com verificação de que todo o texto foi falado;
3. montagem de um MP3 por página, numa pasta com nome legível:
       <livro>/audio001-pag001-prefacio.mp3, audio002-pag002-capitulo-i.mp3, ...
   O livro completo é servido como a sequência desses arquivos (sem duplicar
   espaço em disco), e um relatório informa páginas sem texto (imagens).

Como cada etapa é salva em disco, uma conversão interrompida (queda de rede,
programa fechado) continua de onde parou, sem refazer o que já foi feito.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import time
from pathlib import Path

from . import db
from .config import LIBRARY_DIR, TTS_CONCURRENCY
from .pdf_text import build_pages, extract_paragraphs, pdf_toc, slugify
from .tts import MP3_BYTES_PER_SECOND, synthesize_to_file

log = logging.getLogger(__name__)

PLAN_VERSION = 2


class NoTextError(RuntimeError):
    pass


def doc_dir(doc_id: str) -> Path:
    return LIBRARY_DIR / doc_id


def load_pages(doc_id: str) -> dict | None:
    """Páginas de áudio de um documento pronto (None para conversões antigas)."""
    path = doc_dir(doc_id) / "pages.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _keep_awake(on: bool) -> None:
    """Impede o Windows de suspender durante a conversão (a rede cairia)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


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
            _keep_awake(True)
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
                if self.queue.empty():
                    _keep_awake(False)

    # ------------------------------------------------------------------ etapas
    async def _process(self, doc_id: str) -> None:
        folder = doc_dir(doc_id)
        doc = db.get(doc_id)
        plan = await self._plan(doc_id, folder, doc)
        chunks = [c for unit in plan["pages"] for c in unit["chunks"]]
        await self._synthesize(doc_id, folder, chunks, doc["voice"])
        await asyncio.to_thread(self._finalize, doc_id, folder, plan)

    async def _plan(self, doc_id: str, folder: Path, doc: dict) -> dict:
        plan_file = folder / "plan.json"
        if plan_file.exists():
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
            if plan.get("version") == PLAN_VERSION:
                return plan
        # Conversão iniciada numa versão antiga do programa: recomeça no formato novo.
        for old in ("chunks.json", "parts"):
            target = folder / old
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()

        db.update(doc_id, status="extracting", error=None)
        last = 0.0

        def progress(done: int, total: int) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last > 0.5 or done == total:
                last = now
                db.update(doc_id, pages=total, pages_done=done)

        source = str(folder / "source.pdf")
        paragraphs, total = await asyncio.to_thread(extract_paragraphs, source, progress)
        toc = await asyncio.to_thread(pdf_toc, source)
        units, no_text = build_pages(paragraphs, total, toc)
        if not units:
            raise NoTextError(
                "Este PDF não possui texto selecionável (provavelmente é digitalizado/imagem)."
            )
        width = max(3, len(str(len(units))))
        page_width = max(3, len(str(total)))
        for n, unit in enumerate(units, start=1):
            unit["file"] = f"audio{n:0{width}d}-pag{unit['page']:0{page_width}d}-{unit['name']}.mp3"
        plan = {
            "version": PLAN_VERSION,
            "folder": slugify(doc["title"], 60),
            "total_pages": total,
            "no_text": no_text,
            "pages": units,
        }
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        db.update(
            doc_id, pages=total, pages_done=total, folder=plan["folder"],
            characters=sum(len(c["text"]) for u in units for c in u["chunks"]),
        )
        return plan

    async def _synthesize(self, doc_id: str, folder: Path, chunks: list[dict], voice: str) -> None:
        parts = folder / "parts"
        parts.mkdir(exist_ok=True)
        pending = [c for c in chunks if not (parts / f"{c['index']:06d}.json").exists()]
        done = len(chunks) - len(pending)
        db.update(doc_id, status="synthesizing", chunks_total=len(chunks), chunks_done=done, error=None)

        it = iter(pending)

        async def worker() -> None:
            nonlocal done
            for c in it:
                result = await synthesize_to_file(c["text"], voice, str(parts / f"{c['index']:06d}.mp3"))
                # o .json marca o trecho como concluído e verificado
                (parts / f"{c['index']:06d}.json").write_text(
                    json.dumps({"coverage": result.coverage, "split": result.split})
                )
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

    def _finalize(self, doc_id: str, folder: Path, plan: dict) -> None:
        db.update(doc_id, status="finalizing")
        parts = folder / "parts"
        out_dir = folder / plan["folder"]
        out_dir.mkdir(exist_ok=True)

        timeline, page_marks, files = [], [], []
        coverages, split_chunks = [], 0
        position = 0.0
        for unit in plan["pages"]:
            target = out_dir / unit["file"]
            tmp = target.with_suffix(".part")
            start = position
            with open(tmp, "wb") as out:
                for c in unit["chunks"]:
                    part = parts / f"{c['index']:06d}.mp3"
                    info = json.loads((parts / f"{c['index']:06d}.json").read_text())
                    coverages.append(info["coverage"])
                    split_chunks += bool(info.get("split"))
                    timeline.append({"start": round(position, 2), "page": c["page"], "text": c["text"]})
                    with open(part, "rb") as src:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    position += part.stat().st_size / MP3_BYTES_PER_SECOND
            tmp.replace(target)
            size = target.stat().st_size
            files.append({
                "page": unit["page"], "pages": unit["pages"], "name": unit["name"], "file": unit["file"],
                "start": round(start, 2), "duration": round(position - start, 2), "size": size,
            })
            for pg in unit["pages"]:
                page_marks.append({"page": pg, "time": round(start, 2)})

        (folder / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
        (folder / "pages.json").write_text(
            json.dumps({"folder": plan["folder"], "files": files, "no_text": plan["no_text"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        shutil.rmtree(parts, ignore_errors=True)
        old_audio = folder / "audio.mp3"
        if old_audio.exists():
            old_audio.unlink()

        report = {
            "pages_total": plan["total_pages"],
            "pages_with_audio": sum(len(f["pages"]) for f in files),
            "no_text": plan["no_text"],
            "chunks": len(coverages),
            "min_coverage": round(min(coverages, default=1.0), 4),
            "split_chunks": split_chunks,
        }
        db.update(
            doc_id, status="ready", duration=round(position, 2),
            file_size=sum(f["size"] for f in files), page_marks=page_marks,
            report=report, error=None,
        )


converter = Converter()
