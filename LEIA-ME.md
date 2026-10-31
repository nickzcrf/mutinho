# Mutinho

Muta/desmuta o microfone com um atalho global e também pelo celular (lendo um QR code).
Inclui redução de ruído com intensidade ajustável.

## Instalar e abrir

1. Instale o Python 3.9+ (no Windows, marque "Add Python to PATH"; no Linux, instale também `python3-tk`).
2. Na pasta do app:

       python -m pip install -r requirements.txt
       python mutinho.py

Atalho padrão: **Ctrl + Alt + M** (dá para trocar na janela, botão "Alterar").
Fechar a janela no X manda o app para a bandeja (perto do relógio); para sair, use o ícone > Sair.

## Pelo celular

Com o PC e o celular no mesmo Wi-Fi, abra a câmera do celular e aponte para o QR da janela.
Abre uma página com um botão grande para mutar/desmutar (sem instalar nada).
"Gerar novo link" invalida o QR antigo.

## Redução de ruído

O painel da direita limpa o ruído de fundo (ventilador, ar-condicionado, chiado) do seu microfone,
com um controle de intensidade de 0 a 100%.

Para os OUTROS programas (Meet, Discord, Zoom, OBS) ouvirem o áudio limpo, é preciso um
microfone virtual. O Windows não cria isso sozinho, então use o **VB-Cable** (grátis):

1. Instale o VB-Cable em vb-audio.com/Cable e reinicie o PC.
2. Abra o Mutinho. Em "Seu microfone", escolha o microfone real.
3. Em "Enviar o áudio limpo para", escolha **CABLE Input**.
4. Marque "Ativar redução de ruído" e ajuste a intensidade (os medidores mostram entrada e saída).
5. No Meet/Discord/Zoom, escolha **CABLE Output** como microfone.

Dicas:
- Use fones de ouvido, para o som da caixa não voltar para o microfone.
- Intensidade 40-70% costuma ser um bom ponto. Em 100% a voz pode soar um pouco mais "seca".
- Mutar pelo Mutinho silencia também a saída limpa.
- Adiciona uns 20 ms de atraso, imperceptível em chamadas.
- Se não instalar as bibliotecas numpy e sounddevice, o app funciona normalmente, só sem este recurso.

## Observações por sistema

- **Windows**: na primeira vez o firewall pergunta; permita em redes privadas, senão o celular não conecta.
- **macOS**: dê permissão de Acessibilidade e Monitoramento de Entrada ao Terminal/Python
  (Ajustes do Sistema > Privacidade e Segurança) para o atalho global funcionar.
  O mute zera o volume de entrada e restaura o valor anterior ao desmutar.
  A bandeja fica desligada no Mac.
- **Linux**: usa `pactl` (PulseAudio/PipeWire). Atalho global funciona em X11; no Wayland pode não funcionar.

## Atualização automática (para quem distribui o app)

O app consulta `version.json` no seu site ao abrir. Se houver versão mais nova, mostra um aviso com
o botão "Atualizar agora": baixa o `.exe`, confere o SHA-256, troca o arquivo e reabre sozinho.

Configuração (uma vez): no topo do `mutinho.py`, troque `SEU-SITE.vercel.app` em `UPDATE_URL`
pelo seu domínio. Enquanto tiver "SEU-SITE", a verificação fica desligada.

A cada nova versão:
1. Aumente `APP_VERSION` no `mutinho.py` (ex.: "1.2.0" -> "1.2.1").
2. Feche o Mutinho e gere o `.exe` (comando abaixo).
3. Rode `python release.py "o que mudou"` (gera `siteMutinho/version.json` com o SHA-256).
4. Faça commit e push do `.exe` e do `version.json` juntos.

Quem tem uma versão sem este recurso precisa baixar pelo site uma vez; depois disso é automático.

## Instalador com o microfone virtual (VB-CABLE)

O `installer.iss` gera o `MutinhoSetup.exe`: instala o Mutinho na pasta do usuário (assim a
atualização automática funciona) e, se a pessoa deixar marcado, instala também o VB-CABLE.
O Windows ainda pede permissão de administrador e a confirmação do driver, e é preciso reiniciar o PC.

Para gerar (só quem distribui precisa disto):
1. Gere o `Mutinho.exe` (comando abaixo).
2. Baixe o pacote do VB-CABLE em vb-audio.com/Cable, extraia o `.zip` e copie TODO o conteúdo para a
   pasta `vbcable\` (deve existir `vbcable\VBCABLE_Setup_x64.exe`).
3. Instale o Inno Setup (jrsoftware.org/isinfo.php).
4. Rode `python make_installer.py`. Sai em `siteMutinho\dist\MutinhoSetup.exe`.

Licença do VB-CABLE: pode ser distribuído junto com o app, desde que se credite a origem
(vb-cable.com) e se informe que é donationware. O instalador e o site já trazem esse aviso.
Só vale para o VB-CABLE padrão (as versões A+B e C+D não podem ser embutidas).
Não coloque a pasta `vbcable\` no GitHub se preferir; ela só é necessária para compilar o instalador.

## Opções

- `python mutinho.py --no-gui`: sem janela (mostra o QR no terminal).
- `python mutinho.py --hidden`: abre já escondido na bandeja.
- Configuração salva em `~/.mutinho.json`.

## Virar um .exe

    python -m pip install pyinstaller
    python -m PyInstaller --onefile --windowed --name Mutinho --hidden-import pystray._win32 --collect-all sounddevice mutinho.py
