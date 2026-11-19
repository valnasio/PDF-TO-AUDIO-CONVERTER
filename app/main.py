from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import db
from .config import LOG_PATH, STATIC_DIR
from .pdf_text import pdf_title
from .tts import DEFAULT_VOICE, VOICE_IDS, VOICES, voice_sample
from .worker import converter, doc_dir, load_pages

_handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
if sys.stderr is not None:  # no executável sem console não há stderr
    _handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=_handlers
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    converter.start()
    yield
    await converter.stop()


app = FastAPI(title="PDF em Áudio", lifespan=lifespan)
_sample_cache: dict[str, bytes] = {}

# Momento da última requisição vinda da interface. No modo navegador (Linux),
# o lançador usa isso para encerrar o programa quando a aba é fechada.
last_activity = time.monotonic()


@app.middleware("http")
async def _track_activity(request, call_next):
    global last_activity
    last_activity = time.monotonic()
    return await call_next(request)


@app.get("/api/ping")
def ping():
    return {"ok": True}


def _public(doc: dict) -> dict:
    d = dict(doc)
    if d["status"] == "queued":
        ahead = [x for x in db.list_active() if x["created_at"] < d["created_at"]]
        d["queue_position"] = len(ahead)
    return d


def _require(doc_id: str) -> dict:
    doc = db.get(doc_id)
    if not doc:
        raise HTTPException(404, "Documento não encontrado")
    return doc


@app.get("/api/voices")
def voices():
    return {"voices": VOICES, "default": DEFAULT_VOICE}


@app.get("/api/voices/{voice}/sample")
async def sample(voice: str):
    if voice not in VOICE_IDS:
        raise HTTPException(404, "Voz desconhecida")
    if voice not in _sample_cache:
        try:
            _sample_cache[voice] = await voice_sample(voice)
        except Exception as exc:
            raise HTTPException(502, f"Serviço de voz indisponível: {exc}")
    return Response(_sample_cache[voice], media_type="audio/mpeg")


@app.post("/api/documents", status_code=201)
async def upload(file: UploadFile = File(...), voice: str = Form(DEFAULT_VOICE)):
    if voice not in VOICE_IDS:
        raise HTTPException(400, "Voz inválida")
    head = await file.read(5)
    if head != b"%PDF-":
        raise HTTPException(400, "O arquivo enviado não é um PDF válido")
    await file.seek(0)

    doc_id = uuid.uuid4().hex[:12]
    folder = doc_dir(doc_id)
    folder.mkdir(parents=True)
    dest = folder / "source.pdf"

    def save() -> None:  # cópia em blocos: suporta arquivos grandes sem estourar memória
        with open(dest, "wb") as out:
            shutil.copyfileobj(file.file, out, 4 * 1024 * 1024)

    await asyncio.to_thread(save)
    filename = Path(file.filename or "documento.pdf").name
    title = await asyncio.to_thread(pdf_title, str(dest)) or Path(filename).stem.replace("_", " ").replace("-", " ").strip()
    doc = db.create(doc_id, title, filename, voice)
    converter.enqueue(doc_id)
    return _public(doc)


@app.get("/api/documents")
def list_documents():
    return {"documents": [_public(d) for d in db.list_all()]}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str):
    return _public(_require(doc_id))


@app.post("/api/documents/{doc_id}/retry")
def retry(doc_id: str):
    doc = _require(doc_id)
    if doc["status"] != "error":
        raise HTTPException(409, "Só é possível tentar novamente conversões com erro")
    db.update(doc_id, status="queued", error=None)
    converter.enqueue(doc_id)
    return _public(db.get(doc_id))


@app.post("/api/documents/{doc_id}/reconvert")
async def reconvert(doc_id: str):
    """Converte de novo com o PDF já salvo (ex.: arquivos da versão antiga, de MP3 único)."""
    doc = _require(doc_id)
    if doc["status"] not in ("ready", "error"):
        raise HTTPException(409, "A conversão deste arquivo ainda está em andamento")
    folder = doc_dir(doc_id)

    def clean() -> None:
        for item in folder.iterdir():
            if item.name == "source.pdf":
                continue
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)

    await asyncio.to_thread(clean)
    db.update(doc_id, status="queued", error=None, chunks_done=0, chunks_total=0,
              duration=0, file_size=0, page_marks=[], report=None)
    converter.enqueue(doc_id)
    return _public(db.get(doc_id))


@app.delete("/api/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: str):
    _require(doc_id)
    converter.cancel(doc_id)
    db.delete(doc_id)
    await asyncio.sleep(0.2)
    await asyncio.to_thread(shutil.rmtree, doc_dir(doc_id), True)
    return Response(status_code=204)


def _disposition(kind: str, filename: str) -> str:
    return f"{kind}; filename*=UTF-8''{quote(filename)}"


def _concat_response(paths: list[Path], request: Request, filename: str, download: bool) -> Response:
    """Serve vários MP3 como um arquivo só, com suporte a Range (avançar/voltar
    no player). Usado para o livro completo, formado pelos áudios das páginas."""
    sizes = [p.stat().st_size for p in paths]
    total = sum(sizes)
    start, end, status = 0, total - 1, 200
    rng = request.headers.get("range", "")
    if rng.startswith("bytes="):
        a, _, b = rng[6:].split(",")[0].partition("-")
        try:
            if a:
                start = int(a)
                end = min(int(b), total - 1) if b else total - 1
            elif b:  # últimos N bytes
                start = max(0, total - int(b))
            status = 206
        except ValueError:
            pass
        if start > end or start >= total:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})

    def body():
        pos = 0
        for path, size in zip(paths, sizes):
            if pos + size <= start:
                pos += size
                continue
            if pos > end:
                break
            with open(path, "rb") as f:
                f.seek(max(0, start - pos))
                remaining = min(end, pos + size - 1) - max(start, pos) + 1
                while remaining > 0:
                    data = f.read(min(256 * 1024, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
            pos += size

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Disposition": _disposition("attachment" if download else "inline", filename),
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return StreamingResponse(body(), status_code=status, media_type="audio/mpeg", headers=headers)


def _ready_pages(doc_id: str) -> tuple[dict, dict | None]:
    doc = _require(doc_id)
    if doc["status"] != "ready":
        raise HTTPException(409, "Áudio ainda não está pronto")
    return doc, load_pages(doc_id)


@app.get("/api/documents/{doc_id}/audio")
def audio(doc_id: str, request: Request, download: bool = False):
    """Livro completo em um único MP3."""
    doc, pages = _ready_pages(doc_id)
    if pages is None:  # convertido numa versão antiga: um único audio.mp3
        path = doc_dir(doc_id) / "audio.mp3"
        if not path.exists():
            raise HTTPException(404, "Áudio não encontrado")
        return FileResponse(
            path, media_type="audio/mpeg", filename=f"{Path(doc['filename']).stem}.mp3",
            content_disposition_type="attachment" if download else "inline",
        )
    folder = doc_dir(doc_id) / pages["folder"]
    paths = [folder / f["file"] for f in pages["files"]]
    return _concat_response(paths, request, f"{pages['folder']}.mp3", download)


@app.get("/api/documents/{doc_id}/files")
def files(doc_id: str):
    """Árvore de arquivos: pasta do livro com um áudio por página."""
    doc, pages = _ready_pages(doc_id)
    if pages is None:
        return {"folder": Path(doc["filename"]).stem, "files": [], "no_text": [], "legacy": True}
    return pages


@app.get("/api/documents/{doc_id}/files/{name}")
def file_item(doc_id: str, name: str, download: bool = False):
    _doc, pages = _ready_pages(doc_id)
    if pages is None or name not in {f["file"] for f in pages["files"]}:
        raise HTTPException(404, "Arquivo não encontrado")
    return FileResponse(
        doc_dir(doc_id) / pages["folder"] / name, media_type="audio/mpeg", filename=name,
        content_disposition_type="attachment" if download else "inline",
    )


@app.get("/api/documents/{doc_id}/zip")
async def zip_folder(doc_id: str):
    """Pasta do livro compactada (MP3 já é comprimido: ZIP sem recompressão)."""
    _doc, pages = _ready_pages(doc_id)
    if pages is None:
        raise HTTPException(409, "Conversão feita numa versão antiga: baixe o MP3 completo")
    folder = doc_dir(doc_id) / pages["folder"]
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])

    def build() -> None:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for f in pages["files"]:
                z.write(folder / f["file"], f"{pages['folder']}/{f['file']}")

    await asyncio.to_thread(build)
    return FileResponse(
        tmp, media_type="application/zip", filename=f"{pages['folder']}.zip",
        background=BackgroundTask(tmp.unlink, missing_ok=True),
    )


@app.post("/api/documents/{doc_id}/open-folder")
def open_folder(doc_id: str, request: Request):
    """Abre a pasta dos áudios no gerenciador de arquivos (só para o próprio computador)."""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Disponível apenas no computador onde o programa está aberto")
    _doc, pages = _ready_pages(doc_id)
    folder = doc_dir(doc_id) / (pages["folder"] if pages else "")
    if sys.platform == "win32":
        os.startfile(folder)  # noqa: S606
    else:
        env = dict(os.environ)
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        subprocess.Popen(["xdg-open", str(folder)], env=env)
    return {"ok": True}


@app.get("/api/documents/{doc_id}/pdf")
def pdf(doc_id: str):
    doc = _require(doc_id)
    return FileResponse(
        doc_dir(doc_id) / "source.pdf", media_type="application/pdf",
        filename=doc["filename"], content_disposition_type="inline",
    )


def _page_count(doc_id: str) -> int:
    import pymupdf

    with pymupdf.open(doc_dir(doc_id) / "source.pdf") as pdf_doc:
        return pdf_doc.page_count


@app.get("/api/documents/{doc_id}/pages")
async def pages_info(doc_id: str):
    """Usado pelo leitor quando a janela não tem visualizador de PDF (Linux)."""
    _require(doc_id)
    return {"count": await asyncio.to_thread(_page_count, doc_id)}


def _render_page(doc_id: str, number: int, width: int) -> bytes:
    import pymupdf

    with pymupdf.open(doc_dir(doc_id) / "source.pdf") as pdf_doc:
        if not 1 <= number <= pdf_doc.page_count:
            raise HTTPException(404, "Página inexistente")
        page = pdf_doc[number - 1]
        zoom = width / (page.rect.width or 1)
        return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")


@app.get("/api/documents/{doc_id}/page/{number}")
async def page_image(doc_id: str, number: int, w: int = 1100):
    _require(doc_id)
    png = await asyncio.to_thread(_render_page, doc_id, number, max(300, min(w, 2400)))
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/documents/{doc_id}/timeline")
def timeline(doc_id: str):
    _require(doc_id)
    path = doc_dir(doc_id) / "timeline.json"
    if not path.exists():
        raise HTTPException(409, "Linha do tempo ainda não está pronta")
    return json.loads(path.read_text(encoding="utf-8"))


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
