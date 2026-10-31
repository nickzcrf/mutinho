#!/usr/bin/env python3

"""
Mutinho - muta o microfone com um atalho global e permite mutar/desmutar
pelo celular lendo um QR code (sem instalar nada no celular).

Uso:
    python mutinho.py            # abre a interface
    python mutinho.py --no-gui   # sem interface (mostra o QR no terminal)
    python mutinho.py --fake     # microfone simulado, para testes
"""
import json
import os
import platform
import queue
import secrets
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_NAME = "Mutinho"
SYSTEM = platform.system()  # "Windows", "Darwin" ou "Linux"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".mutinho.json")

DEFAULTS = {
    "hotkey": "<ctrl>+<alt>+m",
    "port": 8765,
    "token": "",
    "sound": True,
    "on_top": False,
    "indicator": False,
    "mac_prev_volume": 70,
}


# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.data = dict(DEFAULTS)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        except Exception:
            pass
        if not self.data.get("token"):
            self.new_token()

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()

    def new_token(self):
        self.data["token"] = secrets.token_urlsafe(9)
        self.save()

    def save(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Controle do microfone (um backend por sistema)
# --------------------------------------------------------------------------
class FakeBackend:
    def __init__(self):
        self.muted = False

    def is_muted(self):
        return self.muted

    def set_muted(self, muted):
        self.muted = bool(muted)


class WindowsBackend:
    """Usa a API de áudio do Windows via pycaw (mute do dispositivo de captura padrão)."""

    def __init__(self):
        self._local = threading.local()

    def _endpoint(self):
        import comtypes
        from comtypes import CLSCTX_ALL
        from ctypes import POINTER, cast
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        if not getattr(self._local, "com_ready", False):
            try:
                comtypes.CoInitialize()
            except Exception:
                pass
            self._local.com_ready = True
        device = AudioUtilities.GetMicrophone()
        iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(iface, POINTER(IAudioEndpointVolume))

    def is_muted(self):
        return bool(self._endpoint().GetMute())

    def set_muted(self, muted):
        self._endpoint().SetMute(1 if muted else 0, None)


class MacBackend:
    """No macOS não existe 'mute' de entrada; zeramos o volume de entrada e lembramos o anterior."""

    def __init__(self, cfg):
        self.cfg = cfg

    @staticmethod
    def _osa(script):
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        return out.stdout.strip()

    def _volume(self):
        try:
            return int(self._osa("input volume of (get volume settings)"))
        except ValueError:
            return 0

    def is_muted(self):
        return self._volume() == 0

    def set_muted(self, muted):
        if muted:
            current = self._volume()
            if current > 0:
                self.cfg["mac_prev_volume"] = current
            self._osa("set volume input volume 0")
        else:
            prev = int(self.cfg["mac_prev_volume"] or 70)
            self._osa(f"set volume input volume {max(prev, 1)}")


class LinuxBackend:
    """PulseAudio / PipeWire via pactl."""

    @staticmethod
    def _run(*args):
        return subprocess.run(["pactl", *args], capture_output=True, text=True).stdout.strip()

    def is_muted(self):
        return "yes" in self._run("get-source-mute", "@DEFAULT_SOURCE@").lower()

    def set_muted(self, muted):
        self._run("set-source-mute", "@DEFAULT_SOURCE@", "1" if muted else "0")


def make_backend(cfg, fake=False):
    if fake:
        return FakeBackend()
    if SYSTEM == "Windows":
        return WindowsBackend()
    if SYSTEM == "Darwin":
        return MacBackend(cfg)
    return LinuxBackend()


# --------------------------------------------------------------------------
# Núcleo: estado + sons + eventos para a interface
# --------------------------------------------------------------------------
def play_sound(kind):
    def _play():
        try:
            if SYSTEM == "Windows":
                import winsound
                winsound.Beep(500 if kind == "mute" else 900, 90)
            elif SYSTEM == "Darwin":
                name = "Tink" if kind == "mute" else "Pop"
                subprocess.run(["afplay", f"/System/Library/Sounds/{name}.aiff"])
        except Exception:
            pass

    threading.Thread(target=_play, daemon=True).start()


class Controller:
    def __init__(self, cfg, backend):
        self.cfg = cfg
        self.backend = backend
        self.lock = threading.RLock()
        self.events = queue.Queue()

    def is_muted(self):
        with self.lock:
            return self.backend.is_muted()

    def set_muted(self, muted):
        with self.lock:
            self.backend.set_muted(muted)
            state = self.backend.is_muted()
        if self.cfg["sound"]:
            play_sound("mute" if state else "unmute")
        self.events.put(("refresh", None))
        return state

    def toggle(self):
        with self.lock:
            return self.set_muted(not self.backend.is_muted())

    def safe_toggle(self):
        try:
            self.toggle()
        except Exception as exc:
            self.events.put(("msg", f"Erro ao alternar: {exc}"))


# --------------------------------------------------------------------------
# Atalho global
# --------------------------------------------------------------------------
class HotkeyManager:
    def __init__(self, callback):
        self.callback = callback
        self.listener = None

    def start(self, combo):
        from pynput import keyboard

        keyboard.HotKey.parse(combo)  # levanta ValueError se for inválido
        self.stop()
        self.listener = keyboard.GlobalHotKeys({combo: self.callback})
        self.listener.daemon = True
        self.listener.start()

    def stop(self):
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None


def record_hotkey(on_done):
    """Escuta a próxima combinação de teclas e entrega no formato do pynput."""
    from pynput import keyboard

    mods = set()
    done = {"flag": False}
    order = ["ctrl", "alt", "shift", "cmd"]
    alias = {
        "ctrl_l": "ctrl", "ctrl_r": "ctrl", "ctrl": "ctrl",
        "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt", "alt": "alt",
        "shift_l": "shift", "shift_r": "shift", "shift": "shift",
        "cmd_l": "cmd", "cmd_r": "cmd", "cmd": "cmd",
    }

    def key_name(key):
        if isinstance(key, keyboard.Key):
            return alias.get(key.name, key.name), key.name in alias
        vk = getattr(key, "vk", None)
        if vk is not None and (48 <= vk <= 57 or 65 <= vk <= 90):
            return chr(vk).lower(), False
        char = getattr(key, "char", None)
        if char and char.isprintable():
            return char.lower(), False
        if vk is not None:
            return f"<{vk}>", False
        return None, False

    def on_press(key):
        if done["flag"]:
            return False
        name, is_mod = key_name(key)
        if name is None:
            return None
        if is_mod:
            mods.add(name)
            return None
        is_function_key = name.startswith("f") and name[1:].isdigit()
        if not mods and not is_function_key:
            return None  # exige pelo menos um modificador (exceto teclas F)
        mod_parts = [f"<{m}>" for m in order if m in mods]
        if len(name) == 1 or name.startswith("<"):
            key_part = name
        else:
            key_part = f"<{name}>"
        combo = "+".join(mod_parts + [key_part])
        done["flag"] = True
        on_done(combo)
        return False

    def on_release(key):
        name, is_mod = key_name(key)
        if is_mod:
            mods.discard(name)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    return listener


def pretty_combo(combo):
    names = {"<ctrl>": "Ctrl", "<alt>": "Alt", "<shift>": "Shift", "<cmd>": "Cmd"}
    out = []
    for part in combo.split("+"):
        if part in names:
            out.append(names[part])
        elif part.startswith("<") and part.endswith(">") and not part[1:-1].isdigit():
            out.append(part[1:-1].upper())
        else:
            out.append(part.upper())
    return " + ".join(out)


# --------------------------------------------------------------------------
# Servidor web para o celular
# --------------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="theme-color" content="#14161a">
<title>Mutinho</title>
<style>
  :root { --bg:#14161a; --fg:#e8eaed; --dim:#9aa0a6; --live:#22c55e; --mute:#ef4444; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; min-height:100vh; background:var(--bg); color:var(--fg);
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         display:flex; flex-direction:column; align-items:center; justify-content:center; gap:28px; padding:24px; }
  h1 { margin:0; font-size:20px; font-weight:600; letter-spacing:.5px; color:var(--dim); }
  #btn { width:min(72vw, 300px); aspect-ratio:1; border-radius:50%; border:0; font-size:84px; color:#fff;
         background:var(--live); box-shadow:0 0 0 14px rgba(34,197,94,.18); transition:all .15s; }
  #btn.muted { background:var(--mute); box-shadow:0 0 0 14px rgba(239,68,68,.18); }
  #btn:active { transform:scale(.96); }
  #label { font-size:22px; font-weight:600; text-align:center; }
  #hint { font-size:14px; color:var(--dim); text-align:center; min-height:18px; }
</style>
</head>
<body>
  <h1>MUTINHO</h1>
  <button id="btn" aria-label="Alternar microfone">🎙️</button>
  <div id="label">Conectando…</div>
  <div id="hint"></div>
<script>
const token = new URLSearchParams(location.search).get('t') || '';
const btn = document.getElementById('btn');
const label = document.getElementById('label');
const hint = document.getElementById('hint');

function render(muted) {
  btn.classList.toggle('muted', muted);
  btn.textContent = muted ? '🔇' : '🎙️';
  label.textContent = muted ? 'Microfone MUTADO' : 'Microfone LIGADO';
  hint.textContent = muted ? 'Toque para desmutar' : 'Toque para mutar';
}
async function call(path, method) {
  const r = await fetch(path + '?t=' + encodeURIComponent(token), { method });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
async function refresh() {
  try { render((await call('/api/state', 'GET')).muted); }
  catch (e) { label.textContent = 'Sem conexão com o PC'; hint.textContent = 'Confira o Wi-Fi e escaneie o QR de novo'; }
}
btn.addEventListener('click', async () => {
  if (navigator.vibrate) navigator.vibrate(30);
  try { render((await call('/api/toggle', 'POST')).muted); } catch (e) { refresh(); }
});
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


def make_handler(ctrl):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            query = parse_qs(urlparse(self.path).query)
            given = (query.get("t") or [self.headers.get("X-Token", "")])[0]
            return secrets.compare_digest(given, ctrl.cfg["token"])

        def _api(self, path, allowed_method):
            if self.command != allowed_method[path]:
                return self._send(405, '{"error":"metodo"}')
            if not self._authorized():
                return self._send(403, '{"error":"token invalido"}')
            try:
                if path == "/api/toggle":
                    muted = ctrl.toggle()
                elif path == "/api/mute":
                    muted = ctrl.set_muted(True)
                elif path == "/api/unmute":
                    muted = ctrl.set_muted(False)
                else:
                    muted = ctrl.is_muted()
                self._send(200, json.dumps({"muted": muted}))
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}))

        METHODS = {
            "/api/state": "GET",
            "/api/toggle": "POST",
            "/api/mute": "POST",
            "/api/unmute": "POST",
        }

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path in self.METHODS:
                self._api(path, self.METHODS)
            else:
                self._send(404, '{"error":"nao encontrado"}')

        def do_POST(self):
            path = urlparse(self.path).path
            if path in self.METHODS:
                self._api(path, self.METHODS)
            else:
                self._send(404, '{"error":"nao encontrado"}')

    return Handler


class RemoteServer:
    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.httpd = None
        self.port = None

    def start(self):
        base = int(self.ctrl.cfg["port"])
        last_error = None
        for port in range(base, base + 10):
            try:
                self.httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(self.ctrl))
                self.port = port
                break
            except OSError as exc:
                last_error = exc
        if self.httpd is None:
            raise RuntimeError(f"Não foi possível abrir uma porta: {last_error}")
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def url(self):
        return f"http://{local_ip()}:{self.port}/?t={self.ctrl.cfg['token']}"


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------
# Interface gráfica (tkinter)
# --------------------------------------------------------------------------
class GUI:
    BG = "#14161a"
    CARD = "#1d2026"
    FG = "#e8eaed"
    DIM = "#9aa0a6"
    LIVE = "#22c55e"
    MUTE = "#ef4444"
    ACCENT = "#3b82f6"

    def __init__(self, ctrl, server, hotkeys):
        import tkinter as tk

        self.tk = tk
        self.ctrl, self.server, self.hotkeys = ctrl, server, hotkeys
        self.cfg = ctrl.cfg
        self.muted = False
        self.recording = False
        self.indicator = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.configure(bg=self.BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", bool(self.cfg["on_top"]))
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._build()
        self.refresh()
        self.root.after(100, self._pump_events)
        self.root.after(1500, self._poll_state)
        if self.cfg["indicator"]:
            self._set_indicator(True)

    # ---- construção -------------------------------------------------------
    def _button(self, parent, text, command, bg=None):
        tk = self.tk
        bg = bg or self.ACCENT
        lbl = tk.Label(parent, text=text, bg=bg, fg="white", padx=14, pady=6,
                       font=("Helvetica", 10, "bold"), cursor="hand2")
        lbl.bind("<Button-1>", lambda e: command())
        return lbl

    def _card(self, parent):
        tk = self.tk
        frame = tk.Frame(parent, bg=self.CARD, padx=16, pady=14)
        frame.pack(fill="x", padx=18, pady=(0, 12))
        return frame

    def _build(self):
        tk = self.tk
        root = self.root

        tk.Label(root, text="MUTINHO", bg=self.BG, fg=self.DIM,
                 font=("Helvetica", 11, "bold")).pack(pady=(16, 6))

        self.circle = tk.Canvas(root, width=160, height=160, bg=self.BG,
                                highlightthickness=0, cursor="hand2")
        self.circle.pack()
        self.circle_shape = self.circle.create_oval(10, 10, 150, 150, fill=self.LIVE, outline="")
        self.circle_icon = self.circle.create_text(80, 80, text="🎙", font=("Helvetica", 54), fill="white")
        self.circle.bind("<Button-1>", lambda e: self._click_toggle())

        self.status = tk.Label(root, text="", bg=self.BG, fg=self.FG, font=("Helvetica", 15, "bold"))
        self.status.pack(pady=(8, 14))

        # Atalho
        card = self._card(root)
        tk.Label(card, text="Atalho global", bg=self.CARD, fg=self.DIM,
                 font=("Helvetica", 9)).pack(anchor="w")
        row = tk.Frame(card, bg=self.CARD)
        row.pack(fill="x", pady=(4, 0))
        self.hotkey_label = tk.Label(row, text=pretty_combo(self.cfg["hotkey"]), bg=self.CARD,
                                     fg=self.FG, font=("Helvetica", 13, "bold"))
        self.hotkey_label.pack(side="left")
        self.record_btn = self._button(row, "Alterar", self._start_recording)
        self.record_btn.pack(side="right")

        # Opções
        card = self._card(root)
        self.var_sound = tk.BooleanVar(value=bool(self.cfg["sound"]))
        self.var_top = tk.BooleanVar(value=bool(self.cfg["on_top"]))
        self.var_ind = tk.BooleanVar(value=bool(self.cfg["indicator"]))
        for text, var, cmd in (
            ("Som ao mutar/desmutar", self.var_sound, self._opt_sound),
            ("Manter janela sempre no topo", self.var_top, self._opt_top),
            ("Indicador flutuante na tela", self.var_ind, self._opt_indicator),
        ):
            tk.Checkbutton(card, text=text, variable=var, command=cmd, bg=self.CARD, fg=self.FG,
                           selectcolor=self.BG, activebackground=self.CARD, activeforeground=self.FG,
                           highlightthickness=0, bd=0, anchor="w").pack(fill="x")

        # Celular
        card = self._card(root)
        tk.Label(card, text="Controle pelo celular", bg=self.CARD, fg=self.DIM,
                 font=("Helvetica", 9)).pack(anchor="w")
        self.qr_canvas = tk.Canvas(card, bg="white", highlightthickness=0)
        self.qr_canvas.pack(pady=(8, 6))
        self.url_label = tk.Label(card, text="", bg=self.CARD, fg=self.DIM,
                                  font=("Helvetica", 8), wraplength=300)
        self.url_label.pack()
        tk.Label(card, text="Celular e PC precisam estar no mesmo Wi-Fi.", bg=self.CARD, fg=self.DIM,
                 font=("Helvetica", 8)).pack(pady=(2, 8))
        btns = tk.Frame(card, bg=self.CARD)
        btns.pack()
        self._button(btns, "Copiar link", self._copy_url).pack(side="left", padx=4)
        self._button(btns, "Gerar novo link", self._new_link, bg="#4b5563").pack(side="left", padx=4)
        self._draw_qr()

        self.msg = tk.Label(root, text="", bg=self.BG, fg=self.DIM, font=("Helvetica", 9),
                            wraplength=340)
        self.msg.pack(pady=(0, 12))

    # ---- QR ---------------------------------------------------------------
    def _draw_qr(self):
        import qrcode

        url = self.server.url()
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        cell = max(2, 200 // len(matrix))
        size = cell * len(matrix)
        self.qr_canvas.delete("all")
        self.qr_canvas.configure(width=size, height=size)
        for y, row in enumerate(matrix):
            for x, filled in enumerate(row):
                if filled:
                    self.qr_canvas.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell,
                                                    fill="black", outline="")
        self.url_label.configure(text=url)

    def _copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.server.url())
        self._say("Link copiado.")

    def _new_link(self):
        self.cfg.new_token()
        self._draw_qr()
        self._say("Novo link gerado. O QR antigo parou de funcionar.")

    # ---- estado -----------------------------------------------------------
    def _say(self, text):
        self.msg.configure(text=text)

    def _click_toggle(self):
        threading.Thread(target=self.ctrl.safe_toggle, daemon=True).start()

    def refresh(self):
        try:
            self.muted = self.ctrl.is_muted()
        except Exception as exc:
            self._say(f"Não consegui ler o microfone: {exc}")
            return
        color = self.MUTE if self.muted else self.LIVE
        self.circle.itemconfigure(self.circle_shape, fill=color)
        self.circle.itemconfigure(self.circle_icon, text="🔇" if self.muted else "🎙")
        self.status.configure(text="Microfone MUTADO" if self.muted else "Microfone LIGADO",
                              fg=color)
        if self.indicator is not None:
            self.indicator_label.configure(
                text="● MUTADO" if self.muted else "● MIC LIGADO", fg=color)

    def _poll_state(self):
        self.refresh()
        self.root.after(1500, self._poll_state)

    def _pump_events(self):
        try:
            while True:
                kind, payload = self.ctrl.events.get_nowait()
                if kind == "refresh":
                    self.refresh()
                elif kind == "msg":
                    self._say(payload)
                elif kind == "hotkey":
                    self._apply_hotkey(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._pump_events)

    # ---- atalho -----------------------------------------------------------
    def _start_recording(self):
        if self.recording:
            return
        try:
            record_hotkey(lambda combo: self.ctrl.events.put(("hotkey", combo)))
        except Exception as exc:
            self._say(f"Não foi possível gravar o atalho: {exc}")
            return
        self.recording = True
        self.hotkey_label.configure(text="Pressione o novo atalho…")
        self.record_btn.configure(text="Gravando…")

    def _apply_hotkey(self, combo):
        self.recording = False
        self.record_btn.configure(text="Alterar")
        try:
            self.hotkeys.start(combo)
            self.cfg["hotkey"] = combo
            self._say("Atalho atualizado.")
        except Exception as exc:
            self._say(f"Atalho inválido ({exc}). Mantido o anterior.")
            try:
                self.hotkeys.start(self.cfg["hotkey"])
            except Exception:
                pass
        self.hotkey_label.configure(text=pretty_combo(self.cfg["hotkey"]))

    # ---- opções -----------------------------------------------------------
    def _opt_sound(self):
        self.cfg["sound"] = bool(self.var_sound.get())

    def _opt_top(self):
        self.cfg["on_top"] = bool(self.var_top.get())
        self.root.attributes("-topmost", bool(self.var_top.get()))

    def _opt_indicator(self):
        self.cfg["indicator"] = bool(self.var_ind.get())
        self._set_indicator(bool(self.var_ind.get()))

    def _set_indicator(self, on):
        tk = self.tk
        if on and self.indicator is None:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg="#000000")
            width, height = 130, 34
            x = self.root.winfo_screenwidth() - width - 20
            win.geometry(f"{width}x{height}+{x}+20")
            self.indicator_label = tk.Label(win, text="", bg="#000000", fg=self.LIVE,
                                            font=("Helvetica", 10, "bold"))
            self.indicator_label.pack(expand=True, fill="both")
            self.indicator = win
            self.refresh()
        elif not on and self.indicator is not None:
            self.indicator.destroy()
            self.indicator = None

    # ---- ciclo de vida ----------------------------------------------------
    def close(self):
        self.hotkeys.stop()
        self.server.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    cfg = Config()
    backend = make_backend(cfg, fake="--fake" in args)
    ctrl = Controller(cfg, backend)

    server = RemoteServer(ctrl)
    server.start()

    hotkeys = HotkeyManager(ctrl.safe_toggle)
    hotkey_error = None
    try:
        hotkeys.start(cfg["hotkey"])
    except Exception as exc:
        hotkey_error = str(exc)

    if "--no-gui" in args:
        import qrcode

        print(f"{APP_NAME} rodando.")
        print(f"Atalho: {pretty_combo(cfg['hotkey'])}" + (f"  (indisponível: {hotkey_error})" if hotkey_error else ""))
        print(f"Link do celular: {server.url()}\n")
        qr = qrcode.QRCode(border=1)
        qr.add_data(server.url())
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return

    gui = GUI(ctrl, server, hotkeys)
    if hotkey_error:
        gui._say(f"Atalho global indisponível: {hotkey_error}")
    gui.run()


if __name__ == "__main__":
    main()
