#!/bin/sh
# Atualiza o menu de aplicativos e o cache de ícones (sem falhar se as ferramentas não existirem).
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
exit 0
