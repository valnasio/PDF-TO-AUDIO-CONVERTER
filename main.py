"""Leitor de PDF em Áudio — aplicativo desktop.

Sobe o servidor internamente e abre a interface:
- Windows: numa janela própria (WebView2, já presente no Windows 10/11).
  Fechar a janela encerra o programa.
- Linux: numa janela GTK/WebKitGTK (dependências instaladas pelo .deb/.rpm).
  Sem GTK/WebKit, abre no navegador padrão e se encerra sozinho quando a aba
  é fechada e não há conversão em andamento.

Uso:
    python main.py              # janela do aplicativo
    python main.py --browser    # abre no navegador (modo desenvolvimento)
    python main.py --host 0.0.0.0 --port 9000 --browser   # acesso pela rede
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
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
        APP_NAME, url, width=1240, height=860, min_size=(820, 600), background_color="#ffffff",
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


IDLE_EXIT_SECONDS = 90


def _system_env() -> dict:
    """Ambiente para programas do sistema (navegador, Python do sistema).

    O PyInstaller altera LD_LIBRARY_PATH; sem restaurar, programas externos
    carregam as bibliotecas embutidas no executável e falham ao abrir."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def _resource(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def open_linux_window(url: str) -> bool:
    """Abre a janela GTK usando o Python do sistema. Retorna False se o
    GTK/WebKitGTK não estiver instalado (aí usamos o navegador)."""
    python = shutil.which("python3", path="/usr/bin:/bin:/usr/local/bin") or shutil.which("python3")
    helper = _resource("app", "linux_window.py")
    if not python or not helper.exists():
        return False
    icon = _resource("assets", "icon.png")
    started = time.monotonic()
    try:
        proc = subprocess.Popen([python, str(helper), url, APP_NAME, str(icon)], env=_system_env())
        code = proc.wait()
    except Exception:
        return False
    # 3 = GTK/WebKit ausente; falha imediata = sem tela gráfica, etc.
    return not (code == 3 or (code != 0 and time.monotonic() - started < 5))


def open_browser(url: str) -> None:
    os.environ.clear()
    os.environ.update(_system_env())
    print(f"{APP_NAME} rodando em {url}  (Ctrl+C para encerrar)", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def wait_until_idle(url: str, auto_exit: bool) -> None:
    """Mantém o servidor vivo. Com auto_exit, encerra quando nenhuma aba do
    navegador estiver aberta há um tempo e não houver conversão em andamento."""
    from app import main as app_main

    try:
        while True:
            time.sleep(5)
            if not auto_exit:
                continue
            idle = time.monotonic() - app_main.last_activity
            if idle > IDLE_EXIT_SECONDS and not _active_conversions(url):
                return
    except KeyboardInterrupt:
        pass


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

    # Janela nativa: WebView2 no Windows, GTK/WebKitGTK no Linux.
    if args.browser:
        opened = False
    elif sys.platform == "win32":
        opened = open_window(url)
    elif sys.platform.startswith("linux"):
        opened = open_linux_window(url)
    else:
        opened = False
    if not opened:
        open_browser(url)
        if server is None:
            return
        # Modo navegador "de usuário" (não o --browser de desenvolvimento):
        # encerra sozinho quando a aba for fechada.
        wait_until_idle(url, auto_exit=not args.browser)

    if server is not None:
        server.should_exit = True
        time.sleep(1)
    os._exit(0)


if __name__ == "__main__":
    main()
