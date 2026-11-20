# -*- mode: python ; coding: utf-8 -*-
# Receita do PyInstaller. Gera dist/LeitorPDF.exe (Windows) ou dist/LeitorPDF (Linux).
# Use: .\build.ps1 (Windows) ou ./build.sh (Linux)
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

WINDOWS = sys.platform == "win32"

# O PyMuPDF 2.x traz pacotes e modelos de IA para análise de layout
# (pymupdf_layout, networkx, arquivos .onnx...) que este projeto não usa.
# onnxruntime e numpy ficam: são usados pelas vozes no computador (Piper).
EXCLUDE_MODULES = [
    "networkx", "pymupdf_layout", "pymupdf4llm", "pymupdf.layout",
    "tkinter", "unittest", "pydoc", "lib2to3", "watchfiles", "uvloop", "httptools",
    "websockets", "yaml", "dotenv",
]
DATAS = [("app/static", "app/static")]
# Vozes no computador: dados de pronúncia do eSpeak NG que acompanham o piper-tts
DATAS += collect_data_files("piper")
BINARIES = collect_dynamic_libs("piper") + collect_dynamic_libs("onnxruntime")
if not WINDOWS:
    # No Linux a janela é GTK/WebKitGTK do sistema (app/linux_window.py, executado
    # pelo Python do sistema); o pywebview não é usado.
    EXCLUDE_MODULES += ["webview", "clr", "clr_loader", "pythonnet", "bottle", "proxy_tools"]
    DATAS += [("app/linux_window.py", "app"), ("assets/icon.png", "assets")]

EXCLUDE_PATHS = ("pymupdf\\layout", "pymupdf/layout", "mupdf-devel", "pymupdf4llm")

a = Analysis(
    ["main.py"],
    datas=DATAS,
    binaries=BINARIES,
    hiddenimports=["piper", "lameenc"],
    excludes=EXCLUDE_MODULES,
    noarchive=False,
)
a.datas = [d for d in a.datas if not any(p in d[0] for p in EXCLUDE_PATHS)]


def _piper_unused(dest: str) -> bool:
    """Dados do Piper para outros idiomas (só português é usado): modelos de
    hebraico e árabe e os dicionários de pronúncia das outras línguas (~45 MB)."""
    d = dest.replace("\\", "/")
    if d.startswith("piper/hebrew/") or d.startswith("piper/tashkeel/"):
        return True
    name = d.rsplit("/", 1)[-1]
    # o eSpeak precisa do dicionário de inglês para iniciar (sem ele, encerra o programa)
    return d.startswith("piper/espeak-ng-data/") and name.endswith("_dict") and name not in ("pt_dict", "en_dict")


a.datas = [d for d in a.datas if not _piper_unused(d[0])]
a.binaries = [b for b in a.binaries if not any(p in b[0] for p in EXCLUDE_PATHS)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LeitorPDF",
    icon="assets/icon.ico" if WINDOWS else None,
    console=not WINDOWS,  # no Linux não abre terminal pelo menu; ajuda quem roda pelo terminal
    upx=False,
    strip=not WINDOWS,
    runtime_tmpdir=None,
)
