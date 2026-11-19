# -*- mode: python ; coding: utf-8 -*-
# Receita do PyInstaller para gerar dist\LeitorPDF.exe. Use: .\build.ps1

# O PyMuPDF 2.x traz pacotes e modelos de IA para análise de layout
# (onnxruntime, numpy, arquivos .onnx...) que este projeto não usa.
EXCLUDE_MODULES = [
    "onnxruntime", "numpy", "networkx", "pymupdf_layout", "pymupdf4llm", "pymupdf.layout",
    "tkinter", "unittest", "pydoc", "lib2to3", "watchfiles", "uvloop", "httptools",
    "websockets", "yaml", "dotenv",
]
EXCLUDE_PATHS = ("pymupdf\\layout", "pymupdf/layout", "mupdf-devel", "pymupdf4llm")

a = Analysis(
    ["main.py"],
    datas=[("app/static", "app/static")],
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
    icon="assets/icon.ico",
    console=False,
    upx=False,
    runtime_tmpdir=None,
)
