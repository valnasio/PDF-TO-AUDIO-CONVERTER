import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # True quando roda como executável (PyInstaller)

if FROZEN:
    # Arquivos embutidos no .exe são extraídos para sys._MEIPASS.
    STATIC_DIR = Path(sys._MEIPASS) / "app" / "static"
    # Dados do usuário ficam numa pasta gravável e persistente:
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
