# Mutinho

Muta o microfone com um atalho global e desmuta pelo celular com um QR code. Grátis, sem cadastro, sem app no celular.

**Site e download:** https://mutinho.vercel.app/

## Recursos

- **Atalho global** para mutar/desmutar de qualquer programa (padrão `Ctrl + Alt + M`, configurável).
- **Controle pelo celular**: escaneie o QR da janela e use um botão grande no navegador do celular (mesmo Wi-Fi, link com código secreto que pode ser trocado).
- **Falar mesmo mutado**: estando mutado, segure uma tecla (padrão `F9`), o círculo da janela ou o botão no celular. O microfone abre enquanto você segura e volta a mutar ao soltar. Um toque rápido no círculo desmuta de vez.
- **Modo "Apertar para falar"**: o microfone fica sempre fechado e abre só enquanto você segura.
- **Redução de ruído** com intensidade de 0 a 100% (ventilador, ar-condicionado, chiado). Usa um microfone virtual (VB-CABLE, incluído no instalador do Windows).
- **Tela cheia** (`F11`) e janela **maximizável**: a interface se adapta ao tamanho.
- **Bandeja do sistema**, indicador flutuante, som ao mutar e início escondido.
- **Iniciar com o computador**, escondido na bandeja (opção na janela ou no instalador).
- **Atualização automática** com verificação SHA-256 (versão `.exe`/instalador no Windows).

## Instalação

### Windows 10/11 (recomendado)

1. Baixe o `MutinhoSetup.exe` em https://mutinho.vercel.app/ e execute.
2. Se aparecer "o Windows protegeu seu PC", clique em **Mais informações → Executar assim mesmo** (o app não tem assinatura digital paga).
3. Deixe marcada a opção do microfone virtual se quiser a redução de ruído; aceite as permissões e reinicie o PC quando pedido.
4. No firewall, permita **Redes privadas**: é isso que deixa o celular conectar.

### macOS

Roda pelo código-fonte (sem instalador). Pouco testado.

```bash
brew install python python-tk
git clone https://github.com/nickzcrf/mutinho.git
cd mutinho
pip3 install -r requirements.txt
python3 mutinho.py
```

Libere o Terminal em **Ajustes → Privacidade e Segurança → Acessibilidade** e **Monitoramento de entrada**. O mute zera o volume de entrada (`osascript`).

### Linux

Roda pelo código-fonte. Pouco testado; o atalho global precisa de sessão X11.

```bash
sudo apt install python3 python3-pip python3-tk pulseaudio-utils
git clone https://github.com/nickzcrf/mutinho.git
cd mutinho
pip3 install -r requirements.txt
python3 mutinho.py
```

O mute usa `pactl` (PulseAudio/PipeWire).

## Uso

| Ação | Como |
| --- | --- |
| Mutar / desmutar | `Ctrl + Alt + M`, o círculo da janela ou o botão no celular |
| Falar estando mutado | Segurar `F9`, o círculo ou "Segure para falar" no celular |
| Modo apertar para falar | Seletor **Alternar / Apertar para falar** na janela |
| Tela cheia | `F11` (sair com `Esc`) ou maximizar a janela |
| Trocar teclas | Botão **Alterar** ao lado de cada atalho |

Nas opções da janela dá para desligar "Segurar para falar estando mutado".

### Redução de ruído (Windows)

1. Marque **Ativar redução de ruído** e escolha seu microfone.
2. Em "Enviar o áudio limpo para", escolha **CABLE Input (VB-Audio Virtual Cable)**.
3. No Meet, Discord, Zoom etc., escolha **CABLE Output** como microfone. Use fones para evitar eco.

## Rodar a partir do código

Requer Python 3.10+ com tkinter.

```bash
pip install -r requirements.txt
python mutinho.py            # abre a janela
python mutinho.py --hidden   # inicia escondido na bandeja
python mutinho.py --fullscreen
python mutinho.py --fake     # microfone simulado, para testes
```

## Gerar os executáveis (Windows)

```powershell
python -m PyInstaller --onefile --windowed --name Mutinho --hidden-import pystray._win32 --collect-all _sounddevice_data --distpath siteMutinho\dist mutinho.py
python release.py "o que mudou"   # grava siteMutinho/version.json (versão + SHA-256)
python make_installer.py          # gera siteMutinho/dist/MutinhoSetup.exe (Inno Setup 6+)
```

Para o instalador, coloque o conteúdo do pacote do VB-CABLE em `vbcable\` (baixado em [vb-audio.com/Cable](https://vb-audio.com/Cable/)). A versão vem de `APP_VERSION` em `mutinho.py`. O site (pasta `siteMutinho`) é estático e fica na Vercel; faça commit do `.exe`, do instalador e do `version.json` juntos.

## Privacidade

O Mutinho não grava nem envia áudio. O controle pelo celular conversa só com o seu PC, na rede local.

Na primeira abertura, o app envia **um único aviso anônimo** para contar instalações: um código aleatório, a versão e o sistema operacional (o IP não é gravado). Para não participar, coloque `"count_installs": false` no arquivo `.mutinho.json` da sua pasta de usuário antes da primeira abertura (ou rode o código-fonte sem acesso à internet). O app também consulta `version.json` no site para checar atualizações.

## Contagem de instalações (para quem publica o site)

O site tem duas funções na Vercel (`siteMutinho/api`) e um painel em `/admin`, protegido por senha:

1. Na Vercel, abra o projeto → **Storage** → crie um banco **Upstash Redis** (plano gratuito) e conecte ao projeto. Isso cria as variáveis `KV_REST_API_URL` e `KV_REST_API_TOKEN`.
2. Em **Settings → Environment Variables**, crie `ADMIN_TOKEN` com uma senha longa (8+ caracteres).
3. Faça um novo deploy (commit + push) e abra `https://SEU-SITE/admin`.

## Créditos e licenças

- O instalador do Windows inclui o **VB-CABLE**, microfone virtual da [VB-Audio Software](https://vb-cable.com) (donationware; contribuições são bem-vindas).
- Feito com Python, tkinter, pynput, pycaw, sounddevice, numpy, qrcode e pystray.
