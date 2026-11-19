# -*- mode: python ; coding: utf-8 -*-
# Receita do PyInstaller. Gera dist/LeitorPDF.exe (Windows) ou dist/LeitorPDF (Linux).
# Use: .\build.ps1 (Windows) ou ./build.sh (Linux)
import sys

WINDOWS = sys.platform == "win32"

# O PyMuPDF 2.x traz pacotes e modelos de IA para análise de layout
# (onnxruntime, numpy, arquivos .onnx...) que este projeto não usa.
EXCLUDE_MODULES = [
    "onnxruntime", "numpy", "networkx", "pymupdf_layout", "pymupdf4llm", "pymupdf.layout",
    "tkinter", "unittest", "pydoc", "lib2to3", "watchfiles", "uvloop", "httptools",
    "websockets", "yaml", "dotenv",
]
DATAS = [("app/static", "app/static")]
if not WINDOWS:
    # No Linux a janela é GTK/WebKitGTK do sistema (app/linux_window.py, executado
    # pelo Python do sistema); o pywebview não é usado.
    EXCLUDE_MODULES += ["webview", "clr", "clr_loader", "pythonnet", "bottle", "proxy_tools"]
    DATAS += [("app/linux_window.py", "app"), ("assets/icon.png", "assets")]

EXCLUDE_PATHS = ("pymupdf\\layout", "pymupdf/layout", "mupdf-devel", "pymupdf4llm")

a = Analysis(
    ["main.py"],
    datas=DATAS,
    excludes=EXCLUDE_MODULES,
    noarchive=False,
)
a.datas = [d for d in a.datas if not any(p in d[0] for p in EXCLUDE_PATHS)]
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
