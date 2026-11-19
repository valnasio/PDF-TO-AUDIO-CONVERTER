#!/usr/bin/env python3
"""Janela nativa do Leitor de PDF no Linux (GTK 3 + WebKitGTK).

Roda com o Python *do sistema* (não o embutido no executável), porque usa as
bibliotecas GTK/WebKit instaladas pelo .deb/.rpm como dependência.

Uso: linux_window.py <url> <título> [ícone]
Códigos de saída: 0 = janela fechada normalmente; 3 = GTK/WebKit indisponível.
"""
import json
import os
import sys
import urllib.request

# Evita tela em branco em algumas placas de vídeo / Wayland / máquinas virtuais.
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

try:
    import gi

    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("WebKit2", "4.1")
    except ValueError:
        gi.require_version("WebKit2", "4.0")
    from gi.repository import GLib, Gtk, WebKit2
except (ImportError, ValueError) as exc:
    print(f"GTK/WebKit indisponível: {exc}", file=sys.stderr)
    sys.exit(3)

ACTIVE = ("queued", "extracting", "synthesizing", "finalizing")


def active_conversions(url: str) -> int:
    try:
        with urllib.request.urlopen(f"{url}/api/documents", timeout=2) as r:
            return sum(d["status"] in ACTIVE for d in json.load(r)["documents"])
    except Exception:
        return 0


def main() -> None:
    url, title = sys.argv[1], sys.argv[2]
    GLib.set_prgname("leitor-pdf")  # liga a janela ao atalho .desktop (ícone na barra)
    GLib.set_application_name(title)
    icon = sys.argv[3] if len(sys.argv) > 3 else None

    win = Gtk.Window(title=title)
    win.set_default_size(1240, 860)
    win.set_size_request(820, 600)
    if icon and os.path.exists(icon):
        win.set_icon_from_file(icon)

    context = WebKit2.WebContext.get_default()
    view = WebKit2.WebView.new_with_context(context)
    settings = view.get_settings()
    settings.set_enable_developer_extras(False)
    settings.set_media_playback_requires_user_gesture(False)

    # --- downloads (botão "Baixar MP3"): pergunta onde salvar
    def on_decide_policy(_view, decision, kind):
        if kind == WebKit2.PolicyDecisionType.RESPONSE:
            headers = decision.get_response().get_http_headers()
            disposition = headers.get_one("Content-Disposition") if headers else None
            if (disposition and "attachment" in disposition.lower()) or not decision.is_mime_type_supported():
                decision.download()
                return True
        return False

    def on_decide_destination(download, suggested):
        # FileChooserNative usa o seletor de arquivos do sistema (portal), o que
        # também funciona dentro do sandbox do Flatpak.
        dialog = Gtk.FileChooserNative.new("Salvar áudio", win, Gtk.FileChooserAction.SAVE, "Salvar", "Cancelar")
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(suggested or "audio.mp3")
        music = os.path.expanduser("~/Música")
        if os.path.isdir(music):
            dialog.set_current_folder(music)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            download.set_destination(dialog.get_uri())
        else:
            download.cancel()
        dialog.destroy()
        return True

    def on_download_started(_ctx, download):
        download.connect("decide-destination", on_decide_destination)

    view.connect("decide-policy", on_decide_policy)
    context.connect("download-started", on_download_started)

    # --- fechar com conversão em andamento: confirma
    def on_delete(*_):
        n = active_conversions(url)
        if not n:
            return False
        dialog = Gtk.MessageDialog(
            transient_for=win, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text="Conversão em andamento",
        )
        dialog.format_secondary_text(
            f"Há {n} conversão(ões) em andamento.\n\n"
            "Se fechar agora, ela será pausada e continuará de onde parou "
            "na próxima vez que você abrir o programa.\n\nDeseja fechar?"
        )
        answer = dialog.run()
        dialog.destroy()
        return answer != Gtk.ResponseType.YES

    win.connect("delete-event", on_delete)
    win.connect("destroy", Gtk.main_quit)

    view.load_uri(url)
    win.add(view)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
