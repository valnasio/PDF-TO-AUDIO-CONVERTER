"""Leitor de PDF em Áudio — aplicativo desktop.

Sobe o servidor internamente e abre a interface numa janela própria
(WebView2, já presente no Windows 10/11). Fechar a janela encerra o programa.

Uso:
    python main.py              # janela do aplicativo
    python main.py --browser    # abre no navegador (modo desenvolvimento)
    python main.py --host 0.0.0.0 --port 9000 --browser   # acesso pela rede
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

# Sem console (executável --windowed) stdout/stderr são None; várias libs
# tentam escrever neles, então apontamos para "nada".
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import uvicorn  # noqa: E402

APP_NAME = "Leitor de PDF em Áudio"
DEFAULT_PORT = 8765


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _is_our_app(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/voices", timeout=2) as r:
            return "voices" in json.load(r)
    except Exception:
        return False


def _active_conversions(url: str) -> int:
    try:
        with urllib.request.urlopen(f"{url}/api/documents", timeout=2) as r:
            docs = json.load(r)["documents"]
        return sum(d["status"] in ("queued", "extracting", "synthesizing", "finalizing") for d in docs)
    except Exception:
        return 0


def start_server(host: str, port: int) -> uvicorn.Server:
    from app.main import app

    config = uvicorn.Config(
        app, host=host, port=port, log_config=None,
        loop="asyncio", http="h11", ws="none", lifespan="on",
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Não foi possível iniciar o servidor interno")
    return server


def open_window(url: str) -> bool:
    """Abre a janela nativa. Retorna False se o WebView2 não estiver disponível."""
    try:
        import webview
    except Exception:
        return False

    webview.settings["ALLOW_DOWNLOADS"] = True
    window = webview.create_window(
        APP_NAME, url, width=1240, height=860, min_size=(820, 600), background_color="#0f1117",
    )

    def on_closing():
        n = _active_conversions(url)
        if not n:
            return True
        return window.create_confirmation_dialog(
            "Conversão em andamento",
            f"Há {n} conversão(ões) em andamento.\n\n"
            "Se fechar agora, ela será pausada e continuará de onde parou "
            "na próxima vez que você abrir o programa.\n\nDeseja fechar?",
        )

    window.events.closing += on_closing
    try:
        webview.start(private_mode=False)
        return True
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--browser", action="store_true", help="usar o navegador em vez da janela")
    args = parser.parse_args()

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"

    # Programa já aberto? Só mostra outra janela apontando para ele.
    server = None
    if not _port_free(args.host, args.port):
        if not _is_our_app(url):
            # porta ocupada por outro programa: usa uma porta livre qualquer
            with socket.socket() as s:
                s.bind((args.host, 0))
                args.port = s.getsockname()[1]
            url = f"http://127.0.0.1:{args.port}"
            server = start_server(args.host, args.port)
    else:
        server = start_server(args.host, args.port)

    if args.browser or not open_window(url):
        webbrowser.open(url)
        if server is None:
            return
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    if server is not None:
        server.should_exit = True
        time.sleep(1)
    os._exit(0)


if __name__ == "__main__":
    main()
