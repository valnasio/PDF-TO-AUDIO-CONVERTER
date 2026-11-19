import logging
import os
import re
import sys
import tempfile
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # True quando roda como executável (PyInstaller)

if FROZEN:
    # Arquivos embutidos no .exe são extraídos para sys._MEIPASS.
    STATIC_DIR = Path(sys._MEIPASS) / "app" / "static"
    # Dados internos (banco, PDFs, temporários) numa pasta gravável e persistente:
    #   Windows: %LOCALAPPDATA%\LeitorPDF     Linux: ~/.local/share/LeitorPDF
    if sys.platform == "win32":
        _base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        _base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    _default_data = _base / "LeitorPDF"
else:
    STATIC_DIR = Path(__file__).resolve().parent / "static"
    _default_data = Path(__file__).resolve().parent.parent / "data"

DATA_DIR = Path(os.environ.get("PDF_AUDIO_DATA", _default_data))
LIBRARY_DIR = DATA_DIR / "library"
DB_PATH = DATA_DIR / "library.db"
LOG_PATH = DATA_DIR / "leitor.log"

# Quantos trechos são sintetizados em paralelo (o serviço limita requisições
# simultâneas; 6 é um bom equilíbrio entre velocidade e estabilidade).
TTS_CONCURRENCY = int(os.environ.get("TTS_CONCURRENCY", "6"))

LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FOLDER_NAME = "PDFAudioConvert"


def _windows_music_dir() -> Path | None:
    """Pasta Músicas do usuário (respeita OneDrive e pastas movidas para outro disco)."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        # FOLDERID_Music {4BD8D571-6D19-48D3-BE97-422220080E43}
        folder_id = GUID(0x4BD8D571, 0x6D19, 0x48D3, (ctypes.c_ubyte * 8)(0xBE, 0x97, 0x42, 0x22, 0x20, 0x08, 0x0E, 0x43))
        path_ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr)) != 0:
            return None
        try:
            return Path(path_ptr.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
    except Exception:
        return None


def _linux_music_dir() -> Path | None:
    """Pasta de música do XDG (ex.: ~/Música em português, ~/Music em inglês)."""
    if os.environ.get("FLATPAK_ID"):
        # No Flatpak, só a subpasta liberada (xdg-music/PDFAudioConvert) fica visível.
        try:
            for child in Path.home().iterdir():
                if (child / OUTPUT_FOLDER_NAME).is_dir():
                    return child
        except OSError:
            pass
    config =Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "user-dirs.dirs"
    for candidate in (config, Path.home() / ".config" / "user-dirs.dirs"):
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                m = re.match(r'\s*XDG_MUSIC_DIR="(.+)"', line)
                if m:
                    path = Path(m.group(1).replace("$HOME", str(Path.home())))
                    if path != Path.home():
                        return path
        except OSError:
            continue
    for name in ("Música", "Músicas", "Music"):
        if (Path.home() / name).is_dir():
            return Path.home() / name
    return Path.home() / "Music"


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".teste-", delete=True):
            pass
        return True
    except OSError:
        return False


def _resolve_output_root() -> Path:
    """Onde ficam as pastas de áudio dos livros, fácil de achar pelo usuário.

    Windows: Músicas\\PDFAudioConvert    Linux/Flatpak: ~/Música/PDFAudioConvert
    Desenvolvimento e testes (sem executável, ou com PDF_AUDIO_DATA) usam uma
    pasta dentro dos dados. Se a pasta escolhida não puder ser gravada, usa a
    pasta interna em vez de falhar.
    """
    override = os.environ.get("PDF_AUDIO_OUTPUT")
    internal = DATA_DIR / OUTPUT_FOLDER_NAME
    if override:
        candidate = Path(override)
    elif not FROZEN or "PDF_AUDIO_DATA" in os.environ:
        candidate = internal
    else:
        music = _windows_music_dir() if sys.platform == "win32" else _linux_music_dir()
        candidate = (music or Path.home()) / OUTPUT_FOLDER_NAME
    if _writable(candidate):
        return candidate
    logging.getLogger(__name__).warning("Sem permissão para gravar em %s; usando %s", candidate, internal)
    internal.mkdir(parents=True, exist_ok=True)
    return internal


OUTPUT_ROOT = _resolve_output_root()
