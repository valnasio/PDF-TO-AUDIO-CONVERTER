#!/usr/bin/env bash
# Gera os pacotes Linux: dist/leitor-pdf_amd64.deb e dist/leitor-pdf.x86_64.rpm
# Uso (na pasta do projeto):  ./build.sh        (versão: VERSION=2.1.0 ./build.sh)
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${VERSION:-$(git describe --tags --abbrev=0 2>/dev/null || echo 0.0.0)}"
export VERSION="${VERSION#v}"

# 1) Executável (PyInstaller)
VENV=.venv-linux
if [ ! -x "$VENV/bin/python" ]; then
  rm -rf "$VENV"
  if ! python3 -m venv "$VENV" 2>/dev/null; then
    # Sem o pacote python3-venv (ex.: Ubuntu mínimo, sem sudo): usa o virtualenv oficial do PyPA.
    echo "python3-venv indisponível; usando virtualenv.pyz"
    rm -rf "$VENV"
    curl -fsSL -o /tmp/virtualenv.pyz https://bootstrap.pypa.io/virtualenv.pyz
    python3 /tmp/virtualenv.pyz -q "$VENV"
  fi
fi
"$VENV/bin/pip" install -q -r requirements.txt -r requirements-build.txt
"$VENV/bin/python" -m PyInstaller --noconfirm --clean LeitorPDF.spec

# 2) Pacotes .deb e .rpm (nfpm: binário único, não precisa de sudo)
NFPM="$(command -v nfpm || true)"
if [ -z "$NFPM" ]; then
  NFPM=/tmp/nfpm/nfpm
  if [ ! -x "$NFPM" ]; then
    TAG=$(curl -fsSL https://api.github.com/repos/goreleaser/nfpm/releases/latest | grep -o '"tag_name": *"[^"]*"' | cut -d'"' -f4)
    mkdir -p /tmp/nfpm
    curl -fsSL "https://github.com/goreleaser/nfpm/releases/download/${TAG}/nfpm_${TAG#v}_Linux_x86_64.tar.gz" | tar -xz -C /tmp/nfpm nfpm
  fi
fi
chmod +x packaging/linux/postinstall.sh
"$NFPM" package --config packaging/linux/nfpm.yaml --packager deb --target dist/leitor-pdf_amd64.deb
"$NFPM" package --config packaging/linux/nfpm.yaml --packager rpm --target dist/leitor-pdf.x86_64.rpm

echo
ls -lh dist/*.deb dist/*.rpm
