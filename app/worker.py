"""Fila de conversão PDF → áudio.

Pipeline de cada documento:
1. extração do texto e organização por página (plano salvo em plan.json);
2. síntese de cada trecho em paralelo (limitado), salvando cada parte em disco,
   com verificação de que todo o texto foi falado;
3. montagem do MP3 de cada página assim que todos os trechos dela ficam prontos
   (dá para ouvir as primeiras páginas antes de o livro inteiro terminar), numa
   pasta com nome legível dentro de Músicas/PDFAudioConvert:
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
from .config import LIBRARY_DIR, OUTPUT_ROOT, TTS_CONCURRENCY
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


def load_plan(doc_id: str) -> dict | None:
    path = doc_dir(doc_id) / "plan.json"
    if not path.exists():
        return None
    plan = json.loads(path.read_text(encoding="utf-8"))
    return plan if plan.get("version") == PLAN_VERSION else None


def audio_dir(doc_id: str, info: dict) -> Path:
    """Pasta dos áudios de um livro. Conversões novas ficam em Músicas/PDFAudioConvert
    ("audio_root" no plano); as feitas antes desta versão ficam na pasta interna."""
    root = info.get("audio_root")
    return (Path(root) if root else doc_dir(doc_id)) / info["folder"]


def remove_audio_dir(doc_id: str, info: dict | None) -> None:
    """Apaga a pasta de áudios de um livro, só se ela estiver num local do próprio app."""
    if not info or not info.get("folder"):
        return
    folder = audio_dir(doc_id, info).resolve()
    roots = {OUTPUT_ROOT.resolve(), doc_dir(doc_id).resolve()}
    if info.get("audio_root"):
        roots.add(Path(info["audio_root"]).resolve())
    if folder.is_dir() and folder.parent in roots and folder not in roots:
        shutil.rmtree(folder, ignore_errors=True)


def _unique_folder(name: str) -> str:
    """Evita que dois livros com o mesmo título usem a mesma pasta."""
    candidate, n = name, 2
    while (OUTPUT_ROOT / candidate).exists():
        candidate = f"{name}-{n}"
        n += 1
    return candidate


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
        await self._synthesize(doc_id, folder, plan, doc["voice"])
        await asyncio.to_thread(self._finalize, doc_id, folder, plan)

    async def _plan(self, doc_id: str, folder: Path, doc: dict) -> dict:
        plan = load_plan(doc_id)
        if plan:
            return plan  # retomada: mantém a pasta escolhida quando a conversão começou
        # Conversão iniciada numa versão antiga do programa: recomeça no formato novo.
        for old in ("chunks.json", "parts", "plan.json"):
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
        folder_name = _unique_folder(slugify(doc["title"], 60))
        (OUTPUT_ROOT / folder_name).mkdir(parents=True, exist_ok=True)  # reserva o nome
        plan = {
            "version": PLAN_VERSION,
            "audio_root": str(OUTPUT_ROOT),
            "folder": folder_name,
            "total_pages": total,
            "no_text": no_text,
            "pages": units,
        }
        tmp = folder / "plan.json.part"
        tmp.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        tmp.replace(folder / "plan.json")
        db.update(
            doc_id, pages=total, pages_done=total, folder=folder_name, pages_ready=0,
            characters=sum(len(c["text"]) for u in units for c in u["chunks"]),
        )
        return plan

    def _assemble_page(self, doc_id: str, folder: Path, plan: dict, unit: dict) -> None:
        """Junta os trechos de uma página no MP3 final (escrita atômica: o arquivo
        só aparece quando está completo) e apaga os trechos já usados."""
        parts = folder / "parts"
        out_dir = audio_dir(doc_id, plan)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / unit["file"]
        if not target.exists():
            tmp = target.with_name(target.name + ".part")
            with open(tmp, "wb") as out:
                for c in unit["chunks"]:
                    with open(parts / f"{c['index']:06d}.mp3", "rb") as src:
                        shutil.copyfileobj(src, out, 1024 * 1024)
            tmp.replace(target)
        for c in unit["chunks"]:
            mp3, mark = parts / f"{c['index']:06d}.mp3", parts / f"{c['index']:06d}.json"
            if mp3.exists() and mark.exists():
                info = json.loads(mark.read_text())
                if "size" not in info:  # trecho de uma versão anterior: guarda o tamanho antes de apagar
                    info["size"] = mp3.stat().st_size
                    mark.write_text(json.dumps(info))
            mp3.unlink(missing_ok=True)

    async def _synthesize(self, doc_id: str, folder: Path, plan: dict, voice: str) -> None:
        parts = folder / "parts"
        parts.mkdir(exist_ok=True)
        out_dir = audio_dir(doc_id, plan)

        def done_mark(c: dict) -> Path:
            return parts / f"{c['index']:06d}.json"  # trecho concluído e verificado

        remaining: dict[int, int] = {}   # página → trechos que faltam
        unit_of: dict[int, dict] = {}    # trecho → página
        ready = 0
        for unit in plan["pages"]:
            for c in unit["chunks"]:
                unit_of[c["index"]] = unit
            remaining[unit["page"]] = sum(not done_mark(c).exists() for c in unit["chunks"])
            if remaining[unit["page"]] == 0:
                # retomada: páginas com todos os trechos prontos são montadas já
                if not (out_dir / unit["file"]).exists():
                    await asyncio.to_thread(self._assemble_page, doc_id, folder, plan, unit)
                ready += 1

        chunks = [c for unit in plan["pages"] for c in unit["chunks"]]
        pending = [c for c in chunks if not done_mark(c).exists()]
        done = len(chunks) - len(pending)
        db.update(doc_id, status="synthesizing", chunks_total=len(chunks), chunks_done=done,
                  pages_ready=ready, error=None)

        it = iter(pending)

        async def worker() -> None:
            nonlocal done, ready
            for c in it:
                result = await synthesize_to_file(c["text"], voice, str(parts / f"{c['index']:06d}.mp3"))
                done_mark(c).write_text(json.dumps(
                    {"coverage": result.coverage, "split": result.split, "size": result.size}
                ))
                done += 1
                unit = unit_of[c["index"]]
                remaining[unit["page"]] -= 1
                if remaining[unit["page"]] == 0:  # página completa: já pode ser ouvida
                    await asyncio.to_thread(self._assemble_page, doc_id, folder, plan, unit)
                    ready += 1
                db.update(doc_id, chunks_done=done, pages_ready=ready)

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
        out_dir = audio_dir(doc_id, plan)

        timeline, page_marks, files = [], [], []
        coverages, split_chunks = [], 0
        position = 0.0
        for unit in plan["pages"]:
            target = out_dir / unit["file"]
            infos = []
            for c in unit["chunks"]:
                info = json.loads((parts / f"{c['index']:06d}.json").read_text())
                if "size" not in info:  # trecho gerado por uma versão anterior
                    info["size"] = (parts / f"{c['index']:06d}.mp3").stat().st_size
                infos.append(info)
            if not target.exists():  # segurança: monta se ainda faltar
                self._assemble_page(doc_id, folder, plan, unit)
            start = position
            for c, info in zip(unit["chunks"], infos):
                coverages.append(info["coverage"])
                split_chunks += bool(info.get("split"))
                timeline.append({"start": round(position, 2), "page": c["page"], "text": c["text"]})
                position += info["size"] / MP3_BYTES_PER_SECOND
            size = target.stat().st_size
            position = start + size / MP3_BYTES_PER_SECOND  # o arquivo final é a referência
            files.append({
                "page": unit["page"], "pages": unit["pages"], "name": unit["name"], "file": unit["file"],
                "start": round(start, 2), "duration": round(position - start, 2), "size": size,
            })
            for pg in unit["pages"]:
                page_marks.append({"page": pg, "time": round(start, 2)})

        (folder / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
        pages = {
            "folder": plan["folder"], "audio_root": plan.get("audio_root"),
            "files": files, "no_text": plan["no_text"],
        }
        (folder / "pages.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
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
            pages_ready=len(files), report=report, error=None,
        )


converter = Converter()
