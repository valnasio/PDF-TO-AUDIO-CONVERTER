<div align="center">

<img src="docs/img/banner.svg" alt="Leitor de PDF em Áudio" width="100%" />

<br />
<br />

**Transforme qualquer PDF em audiolivro, com voz natural, sem palavras cortadas e sem limite de tamanho.**

<br />

<a href="https://github.com/valnasio/PDF-TO-AUDIO-CONVERTER/releases/latest/download/LeitorPDF.exe">
  <img src="https://img.shields.io/badge/⬇%20%20Baixar%20para%20Windows-LeitorPDF.exe-7c6cff?style=for-the-badge&labelColor=36c5b0" alt="Baixar para Windows" height="48" />
</a>

<br />
<br />

<sub>Windows 10 e 11 · arquivo único de ~36 MB · não precisa instalar nada</sub>

<br />
<br />

<a href="https://github.com/valnasio/PDF-TO-AUDIO-CONVERTER/releases"><img src="https://img.shields.io/github/v/release/valnasio/PDF-TO-AUDIO-CONVERTER?style=flat-square&color=7c6cff&label=versão" alt="Versão" /></a>
<a href="https://github.com/valnasio/PDF-TO-AUDIO-CONVERTER/releases"><img src="https://img.shields.io/github/downloads/valnasio/PDF-TO-AUDIO-CONVERTER/total?style=flat-square&color=36c5b0&label=downloads" alt="Downloads" /></a>
<img src="https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square" alt="Python 3.10+" />

</div>

---

## 📖 Descrição

O **Leitor de PDF em Áudio** converte documentos PDF em audiolivros narrados por vozes neurais em português. O programa tem uma biblioteca onde você pode **ouvir** o áudio ou **ler** o PDF original, e o texto acompanha a narração em tempo real.

<p align="center">
  <img src="docs/img/player.png" alt="Player com leitura acompanhada" width="90%" />
</p>

## 🕰️ A história do projeto

Este projeto nasceu há cerca de 4 anos, inspirado por um vídeo do canal **Hashtag Programação** no YouTube, que mostrava como transformar o texto de uma variável em áudio. Tive a ideia de aplicar isso à leitura de PDFs: extrair o texto do documento e entregá-lo ao leitor de voz.

A primeira versão foi escrita inteiramente à mão. Era um script de poucas linhas usando `gTTS` e `PyPDF2`: bastava colocar o PDF na pasta, trocar o nome do arquivo no código e rodar. Funcionava, mas tinha limitações que eu não consegui resolver na época:

- **PDFs grandes não funcionavam bem.** O texto era cortado em blocos fixos de caracteres, o que quebrava palavras e frases no meio. Livros inteiros geravam centenas de arquivos soltos, um por página.
- A voz era **robotizada** e a entonação ficava estranha em títulos, cabeçalhos e números de página.
- Era preciso **saber programar** para usar, além de instalar o FFmpeg manualmente.

O projeto ficou parado nesse ponto. Com a chegada das ferramentas de IA, voltei a ele para destravar justamente o que tinha ficado pendente, principalmente o **suporte a PDFs grandes**, e aproveitei para transformar o script num aplicativo que qualquer pessoa consegue usar.

| | Versão original (manual) | Versão atual |
|---|---|---|
| **PDFs grandes** | Cortes fixos de 9.999 caracteres, palavras quebradas | Divisão só entre frases e parágrafos, milhares de páginas |
| **Voz** | gTTS (robotizada) | Vozes neurais naturais (Francisca, Antônio, Thalita…) |
| **Resultado** | Um MP3 por página | Um único audiolivro com navegação por página |
| **Texto faltando** | Sem verificação | Cada trecho é conferido e refeito se faltar algo |
| **Interrupções** | Recomeçava do zero | Continua de onde parou |
| **Como usar** | Editar o código e rodar no terminal | Dois cliques no `.exe` |
| **Dependências** | Python + FFmpeg | Nenhuma |

## ✨ Funcionalidades

<table>
  <tr>
    <td width="50%"><img src="docs/img/envio.png" alt="Tela de envio" /></td>
    <td width="50%"><img src="docs/img/conversao.png" alt="Tela de conversão" /></td>
  </tr>
  <tr>
    <td><b>📤 Envio:</b> arraste o PDF, escolha a voz e ouça uma amostra antes de converter.</td>
    <td><b>⏳ Conversão:</b> acompanhe cada etapa, o percentual e o tempo restante.</td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/img/player.png" alt="Player" /></td>
    <td width="50%"><img src="docs/img/biblioteca.png" alt="Biblioteca" /></td>
  </tr>
  <tr>
    <td><b>🎧 Player:</b> velocidade de 0.75× a 2×, ir para página, texto acompanhando a narração, continuar de onde parou e baixar o MP3.</td>
    <td><b>📚 Biblioteca:</b> todos os seus arquivos, com busca e progresso de escuta. Ouça o áudio ou leia o PDF.</td>
  </tr>
</table>

## 🚀 Como usar

1. **[Baixe o `LeitorPDF.exe`](https://github.com/valnasio/PDF-TO-AUDIO-CONVERTER/releases/latest/download/LeitorPDF.exe)**.
2. Dê dois cliques. O programa abre em uma janela própria.
3. Arraste um PDF, escolha a voz e clique em **Converter em áudio**.

> [!NOTE]
> Na primeira execução, o Windows pode exibir **"O Windows protegeu o computador"**. Clique em **Mais informações → Executar assim mesmo**. Isso acontece com programas que não têm assinatura digital paga.

- É preciso estar **conectado à internet durante a conversão**, porque as vozes são geradas online. Depois de convertido, o áudio pode ser ouvido sem internet.
- A biblioteca fica salva em `%LOCALAPPDATA%\LeitorPDF`.
- Se o programa for fechado durante uma conversão, ela continua de onde parou na próxima vez que for aberto.

## 🧠 Como garantimos a qualidade

| Problema | Solução |
|---|---|
| Voz robótica | Vozes neurais da Microsoft via [`edge-tts`](https://github.com/rany2/edge-tts) |
| Palavras cortadas | O texto só é dividido entre parágrafos e frases. A hifenização de fim de linha é desfeita ("pala-/vra" → "palavra", mantendo "disse-lhe") e parágrafos que continuam na página seguinte são unidos |
| Palavras faltando | O serviço informa cada palavra falada. Se menos de 97% do trecho for lido, ele é gerado de novo (até 6 tentativas) |
| Entonação estranha | Cabeçalhos, rodapés e números de página são removidos. Títulos ganham pausa e travessões de diálogo viram pausas |
| PDFs enormes | Extração página a página, trechos salvos em disco, síntese em paralelo e montagem do áudio em streaming. Um PDF de 2.130 páginas é lido em ~2 segundos |

## 🛠️ Para desenvolvedores

Requer Python 3.10+. Não precisa de FFmpeg.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py             # abre a janela do aplicativo
python main.py --browser   # abre no navegador
```

Opções: `--port 9000`, `--host 0.0.0.0 --browser` (acesso pela rede).

Variáveis de ambiente opcionais:

- `PDF_AUDIO_DATA`: pasta da biblioteca (padrão: `./data`; no executável, `%LOCALAPPDATA%\LeitorPDF`)
- `TTS_CONCURRENCY`: trechos sintetizados em paralelo (padrão: 6)

### Gerar o executável

```powershell
.\build.ps1      # cria dist\LeitorPDF.exe
```

### Estrutura

```
main.py              # abre o aplicativo (janela + servidor interno)
LeitorPDF.spec       # receita do executável (PyInstaller)
build.ps1            # gera dist\LeitorPDF.exe
app/
  main.py            # API (FastAPI)
  pdf_text.py        # extração, limpeza e divisão do texto
  tts.py             # síntese de voz com validação e novas tentativas
  worker.py          # fila de conversão (retomável)
  db.py              # biblioteca (SQLite)
  static/            # interface (HTML/CSS/JS)
docs/                # página de download (GitHub Pages) e imagens
```

## ⚠️ Limitações

- PDFs digitalizados (somente imagem, sem texto selecionável) não são suportados. Para isso seria necessário OCR.
- A conversão leva cerca de 1 minuto para cada 15 a 20 minutos de áudio.

## 🤝 Contribuições

Contribuições são bem-vindas! Se quiser melhorar o projeto, abra uma issue para discutir sua ideia ou envie um pull request.

## 📜 Licença

[Não aplicável] 🚫
