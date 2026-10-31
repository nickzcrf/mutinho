# Mutinho

Muta/desmuta o microfone com um atalho global e também pelo celular (lendo um QR code).

## Instalar e abrir

1. Instale o Python 3.9+ (no Windows, marque "Add Python to PATH"; no Linux, instale também `python3-tk`).
2. Na pasta do app:

       pip install -r requirements.txt
       python mutinho.py

Atalho padrão: **Ctrl + Alt + M** (dá para trocar na janela, botão "Alterar").

## Pelo celular

Com o PC e o celular no mesmo Wi-Fi, abra a câmera do celular e aponte para o QR da janela.
Abre uma página com um botão grande para mutar/desmutar (sem instalar nada).
"Gerar novo link" invalida o QR antigo.

## Observações por sistema

- **Windows**: na primeira vez o firewall pergunta; permita em redes privadas, senão o celular não conecta.
- **macOS**: dê permissão de Acessibilidade e Monitoramento de Entrada ao Terminal/Python
  (Ajustes do Sistema > Privacidade e Segurança) para o atalho global funcionar.
  O mute zera o volume de entrada e restaura o valor anterior ao desmutar.
- **Linux**: usa `pactl` (PulseAudio/PipeWire). Atalho global funciona em X11; no Wayland pode não funcionar.

## Opções

- `python mutinho.py --no-gui`: sem janela (mostra o QR no terminal).
- Configuração salva em `~/.mutinho.json`.

## Virar um .exe / app (opcional)

    pip install pyinstaller
    pyinstaller --onefile --windowed mutinho.py
