#!/usr/bin/env python3
"""
Mutinho - muta o microfone com um atalho global e permite mutar/desmutar
pelo celular lendo um QR code (sem instalar nada no celular).

Uso:
    python mutinho.py            # abre a interface
    python mutinho.py --no-gui   # sem interface (mostra o QR no terminal)
    python mutinho.py --fake     # microfone simulado, para testes
"""
import hashlib
import json
import os
import platform
import queue
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

APP_NAME = "Mutinho"
APP_VERSION = "1.4.0"
# Endereço do version.json publicado no seu site (troque SEU-SITE pelo seu domínio da Vercel).
# Enquanto contiver "SEU-SITE", a verificação de atualização fica desligada.
UPDATE_URL = "https://mutinho.vercel.app/version.json"
SYSTEM = platform.system()  # "Windows", "Darwin" ou "Linux"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".mutinho.json")

DEFAULTS = {
    "hotkey": "<ctrl>+<alt>+m",
    "port": 8765,
    "token": "",
    "sound": True,
    "on_top": False,
    "indicator": False,
    "start_hidden": False,
    "mode": "toggle",          # "toggle" (alternar) ou "ptt" (apertar para falar)
    "ptt_hotkey": "<f9>",
    "talk_muted": True,  # no modo alternar, segurar a tecla/botão fala sem desmutar de vez
    "ns_enabled": False,
    "ns_intensity": 60,
    "ns_in": "",
    "ns_out": "",
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

    def get(self, key, default=None):
        return self.data.get(key, default)

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


TAP_SECONDS = 0.30  # no círculo, apertar e soltar antes disso conta como toque (desmutar)
REMOTE_HOLD_TIMEOUT = 1.5  # segundos sem sinal do celular com o botão apertado => fecha o microfone


class Controller:
    def __init__(self, cfg, backend):
        self.cfg = cfg
        self.backend = backend
        self.lock = threading.RLock()
        self.events = queue.Queue()
        self.last_muted = False  # usado pela redução de ruído para silenciar a saída
        self._talk_q = queue.Queue()
        self._talk_thread = None
        self._beat = None        # instante do último sinal do celular segurando o botão
        self._watch_thread = None
        self._seqs = {}          # último número de pedido recebido de cada celular
        self._temp = False       # microfone aberto só enquanto se segura (estando mutado)

    # ---- estado básico ----
    def get_mode(self):
        return "ptt" if self.cfg["mode"] == "ptt" else "toggle"

    def is_muted(self):
        with self.lock:
            return self.backend.is_muted()

    def set_muted(self, muted, sound=True):
        with self.lock:
            self._temp = False
            self.backend.set_muted(muted)
            state = self.backend.is_muted()
        self.last_muted = state
        if sound and self.cfg["sound"]:
            play_sound("mute" if state else "unmute")
        self.events.put(("refresh", None))
        return state

    def toggle(self):
        with self.lock:
            if self._temp:
                # apertou o atalho segurando a tecla de falar: deixa aberto de vez
                self._temp = False
                return self.set_muted(False)
            return self.set_muted(not self.backend.is_muted())

    def is_temp(self):
        return self._temp

    def release_temp(self):
        """Fecha o microfone se ele estava aberto só por estar segurando a tecla."""
        with self.lock:
            if self._temp:
                self.set_muted(True, sound=False)

    def safe_toggle(self):
        try:
            self.toggle()
        except Exception as exc:
            self.events.put(("msg", f"Erro ao alternar: {exc}"))

    # ---- modo ----
    def set_mode(self, mode):
        mode = "ptt" if mode == "ptt" else "toggle"
        self.cfg["mode"] = mode
        self._temp = False
        if mode == "ptt":
            self.set_muted(True, sound=False)  # em apertar para falar o microfone fica fechado
        self.events.put(("refresh", None))
        return mode

    def apply_startup(self):
        """Ao abrir em modo apertar para falar, o microfone começa fechado."""
        if self.get_mode() == "ptt":
            try:
                self.set_muted(True, sound=False)
            except Exception:
                pass

    def shutdown(self):
        """Ao sair, não deixa o microfone aberto no modo apertar para falar."""
        self._beat = None
        if self.get_mode() == "ptt" or self._temp:
            try:
                self.set_muted(True, sound=False)
            except Exception:
                pass

    # ---- apertar para falar ----
    def talk(self, on):
        """Microfone aberto enquanto 'on' for verdadeiro.
        Modo apertar para falar: sempre. Modo alternar: só estando mutado (e com a opção ligada);
        ao soltar, volta a mutar."""
        if self.get_mode() != "ptt":
            return self._hold_while_muted(on)
        with self.lock:
            if self.backend.is_muted() == (not on):
                return not on  # já está no estado pedido
        return self.set_muted(not on)

    def _hold_while_muted(self, on):
        if not self.cfg.get("talk_muted", True):
            return self.is_muted()
        with self.lock:
            if on:
                if self._temp or not self.backend.is_muted():
                    return self.backend.is_muted()
                state = self.set_muted(False, sound=False)
                self._temp = True  # set_muted zera a marca; só vale depois de abrir
                return state
            if not self._temp:
                return self.backend.is_muted()
            return self.set_muted(True, sound=False)

    def talk_async(self, on):
        """Para ganchos de teclado: não bloqueia e mantém a ordem dos eventos."""
        self._talk_q.put(bool(on))
        if self._talk_thread is None or not self._talk_thread.is_alive():
            self._talk_thread = threading.Thread(target=self._talk_worker, daemon=True)
            self._talk_thread.start()

    def _talk_worker(self):
        while True:
            on = self._talk_q.get()
            try:
                self.talk(on)
            except Exception as exc:
                self.events.put(("msg", f"Erro ao falar: {exc}"))

    def remote_talk(self, on, client=None, seq=None):
        """Celular: 'on' enquanto o botão está apertado, com um sinal a cada ~0,4 s.
        Pedidos atrasados (seq menor que o último do mesmo celular) são ignorados."""
        with self.lock:
            if client is not None and seq is not None:
                if seq < self._seqs.get(client, -1):
                    return self.backend.is_muted()
                if len(self._seqs) > 50:
                    self._seqs.clear()
                self._seqs[client] = seq
            self._beat = time.monotonic() if on else None
        if on:
            self._ensure_watchdog()
        return self.talk(on)

    def _ensure_watchdog(self):
        if self._watch_thread is None or not self._watch_thread.is_alive():
            self._watch_thread = threading.Thread(target=self._watchdog, daemon=True)
            self._watch_thread.start()

    def _watchdog(self):
        """Se o celular parar de avisar que o botão está apertado (sinal fraco, tela
        bloqueada, Wi-Fi caiu), fecha o microfone sozinho."""
        while True:
            time.sleep(0.25)
            with self.lock:
                beat = self._beat
                expired = beat is not None and time.monotonic() - beat > REMOTE_HOLD_TIMEOUT
                if expired:
                    self._beat = None
            if expired:
                try:
                    self.talk(False)
                except Exception:
                    pass


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


class ComboTracker:
    """Detecta quando todas as teclas de uma combinação estão apertadas e quando soltam."""

    def __init__(self, keys, on_down, on_up):
        self.keys = set(keys)
        self.down = set()
        self.active = False
        self.on_down = on_down
        self.on_up = on_up

    def press(self, key):
        if key not in self.keys:
            return
        self.down.add(key)
        if not self.active and self.down == self.keys:
            self.active = True
            self.on_down()

    def release(self, key):
        self.down.discard(key)
        if self.active and key in self.keys:
            self.active = False
            self.on_up()


class PushToTalk:
    """Tecla global de apertar para falar: on_change(True) ao apertar, on_change(False) ao soltar."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.listener = None
        self.tracker = None

    def start(self, combo):
        from pynput import keyboard

        keys = keyboard.HotKey.parse(combo)  # levanta ValueError se for inválido
        self.stop()
        self.tracker = ComboTracker(keys, lambda: self.on_change(True), lambda: self.on_change(False))
        self.listener = keyboard.Listener(on_press=self._press, on_release=self._release)
        self.listener.daemon = True
        self.listener.start()

    def _press(self, key):
        listener, tracker = self.listener, self.tracker
        if listener is not None and tracker is not None:
            tracker.press(listener.canonical(key))

    def _release(self, key):
        listener, tracker = self.listener, self.tracker
        if listener is not None and tracker is not None:
            tracker.release(listener.canonical(key))

    def stop(self):
        was_active = self.tracker is not None and self.tracker.active
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
        self.listener = None
        self.tracker = None
        if was_active:
            self.on_change(False)  # não deixa o microfone aberto


def activate_hotkeys(cfg, toggle_mgr, ptt_mgr):
    """Liga só o atalho do modo atual: alternar ou apertar para falar."""
    toggle_mgr.stop()
    ptt_mgr.stop()
    if cfg["mode"] == "ptt":
        ptt_mgr.start(cfg["ptt_hotkey"])
    else:
        toggle_mgr.start(cfg["hotkey"])
        if cfg.get("talk_muted", True) and cfg["ptt_hotkey"] != cfg["hotkey"]:
            ptt_mgr.start(cfg["ptt_hotkey"])  # segurar para falar enquanto estiver mutado


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
         display:flex; flex-direction:column; align-items:center; justify-content:center; gap:28px; padding:24px;
         -webkit-user-select:none; user-select:none; }
  h1 { margin:0; font-size:20px; font-weight:600; letter-spacing:.5px; color:var(--dim); }
  #btn { width:min(72vw, 300px); aspect-ratio:1; border-radius:50%; border:0; font-size:84px; color:#fff;
         background:var(--live); box-shadow:0 0 0 14px rgba(34,197,94,.18); transition:all .15s;
         touch-action:none; -webkit-touch-callout:none; }
  #btn.muted { background:var(--mute); box-shadow:0 0 0 14px rgba(239,68,68,.18); }
  #btn:active { transform:scale(.96); }
  #label { font-size:22px; font-weight:600; text-align:center; }
  #hint { font-size:14px; color:var(--dim); text-align:center; min-height:18px; }
  #hold { width:min(72vw, 300px); padding:16px; border-radius:16px; border:2px solid #3a3f49; background:#1e2127;
          color:var(--fg); font-size:18px; font-weight:600; touch-action:none; -webkit-touch-callout:none; }
  #hold.on { background:var(--live); border-color:var(--live); color:#06210f; }
  #hold[hidden] { display:none; }
</style>
</head>
<body>
  <h1>MUTINHO</h1>
  <button id="btn" aria-label="Botão do microfone">🎙️</button>
  <div id="label">Conectando…</div>
  <div id="hint"></div>
  <button id="hold" hidden>🎤 Segure para falar</button>
<script>
const token = new URLSearchParams(location.search).get('t') || '';
const cid = Math.random().toString(36).slice(2, 10);
const btn = document.getElementById('btn');
const label = document.getElementById('label');
const hint = document.getElementById('hint');
const hold = document.getElementById('hold');
let mode = 'toggle', muted = true, temp = false, talkMuted = true, holding = false, beatTimer = null, reqId = 0, counter = 0;

function stamp() { return Date.now() * 100 + (counter++ % 100); }

function render() {
  btn.classList.toggle('muted', muted);
  btn.textContent = muted ? '🔇' : '🎙️';
  if (mode === 'ptt') {
    label.textContent = muted ? 'Segure para falar' : 'FALANDO…';
    hint.textContent = muted ? 'Microfone fechado' : 'Solte para fechar';
  } else if (temp) {
    label.textContent = 'FALANDO…';
    hint.textContent = 'Solte para mutar de novo';
  } else {
    label.textContent = muted ? 'Microfone MUTADO' : 'Microfone LIGADO';
    hint.textContent = muted ? 'Toque para desmutar' : 'Toque para mutar';
  }
  hold.hidden = !(mode === 'toggle' && talkMuted && (muted || temp));
  hold.classList.toggle('on', temp);
}
function apply(d) {
  muted = d.muted; mode = d.mode || 'toggle'; temp = !!d.temp;
  talkMuted = d.talk_muted !== false; render();
}

async function call(path, method) {
  const sep = path.includes('?') ? '&' : '?';
  const r = await fetch(path + sep + 't=' + encodeURIComponent(token), { method });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
async function refresh() {
  if (holding) return;
  try { apply(await call('/api/state', 'GET')); }
  catch (e) { label.textContent = 'Sem conexão com o PC'; hint.textContent = 'Confira o Wi-Fi e escaneie o QR de novo'; }
}
async function talk(on) {
  const id = ++reqId;
  try {
    const d = await call('/api/talk?on=' + (on ? 1 : 0) + '&cid=' + cid + '&seq=' + stamp(), 'POST');
    if (id === reqId) apply(d);
  } catch (e) {}
}

function startHold(e) {
  if (holding) return;
  if (mode === 'toggle' && !(talkMuted && muted)) return;
  if (mode === 'toggle' && e.currentTarget === btn) return;
  e.preventDefault();
  holding = true;
  if (btn.setPointerCapture) { try { btn.setPointerCapture(e.pointerId); } catch (err) {} }
  if (navigator.vibrate) navigator.vibrate(25);
  talk(true);
  beatTimer = setInterval(() => talk(true), 400);
}
function endHold() {
  if (!holding) return;
  holding = false;
  clearInterval(beatTimer);
  beatTimer = null;
  talk(false);
}
for (const el of [btn, hold]) {
  el.addEventListener('pointerdown', startHold);
  el.addEventListener('pointerup', endHold);
  el.addEventListener('pointercancel', endHold);
  el.addEventListener('lostpointercapture', endHold);
  el.addEventListener('contextmenu', (e) => e.preventDefault());
}
window.addEventListener('blur', endHold);
document.addEventListener('visibilitychange', () => { if (document.hidden) endHold(); });

btn.addEventListener('click', async () => {
  if (mode === 'ptt') return;
  if (navigator.vibrate) navigator.vibrate(30);
  try { apply(await call('/api/toggle', 'POST')); } catch (e) { refresh(); }
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
            query = parse_qs(urlparse(self.path).query)
            try:
                if path == "/api/toggle":
                    muted = ctrl.toggle()
                elif path == "/api/mute":
                    muted = ctrl.set_muted(True)
                elif path == "/api/unmute":
                    muted = ctrl.set_muted(False)
                elif path == "/api/talk":
                    on = (query.get("on") or ["0"])[0] == "1"
                    client = (query.get("cid") or [None])[0]
                    if client is not None:
                        client = client[:40]
                    try:
                        seq = int((query.get("seq") or [""])[0])
                    except ValueError:
                        seq = None
                    muted = ctrl.remote_talk(on, client, seq)
                else:
                    muted = ctrl.is_muted()
                self._send(200, json.dumps({
                    "muted": muted, "mode": ctrl.get_mode(), "temp": ctrl.is_temp(),
                    "talk_muted": bool(ctrl.cfg.get("talk_muted", True))}))
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}))

        METHODS = {
            "/api/state": "GET",
            "/api/toggle": "POST",
            "/api/mute": "POST",
            "/api/unmute": "POST",
            "/api/talk": "POST",
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
# Redução de ruído
# --------------------------------------------------------------------------
class NoiseReducer:
    """Subtração espectral adaptativa (STFT de 20 ms, 50% de sobreposição).

    Estima o ruído de fundo continuamente (rastreia o piso de energia de cada
    frequência) e atenua o que está perto desse piso. `intensity` vai de 0 a 100:
    0 = sem alteração; 100 = máxima supressão (até -30 dB, com mais agressividade).
    """

    RATE = 48000
    HOP = 480      # 10 ms
    FRAME = 960    # 20 ms

    # parâmetros de ajuste
    PS_SMOOTH = 0.85   # suavização da energia por frequência (0..1, maior = mais lenta)
    NOISE_UP = 1.004   # velocidade com que o piso de ruído sobe por bloco
    NOISE_DOWN = 0.3   # velocidade com que o piso acompanha quedas
    BIAS = 2.5         # compensa o piso estimado ficar abaixo da média real do ruído
    PU_MIX = 0.4       # peso da energia instantânea (resto = suavizada)
    ATTACK = 0.5       # abertura do ganho (fala)
    RELEASE = 0.3      # fechamento do ganho
    BETA_MIN = 0.3     # agressividade com intensidade 0+
    BETA_RANGE = 1.0   # quanto a agressividade cresce até intensidade 100
    FREQ_SMOOTH = True  # suaviza a energia entre frequências vizinhas (menos "chiado picotado")

    def __init__(self):
        import numpy as np

        self.np = np
        n = self.FRAME
        self.win = np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n))
        self.freqs = np.fft.rfftfreq(n, 1.0 / self.RATE)
        self.kernel = np.array([0.25, 0.5, 0.25])
        self.intensity = 60
        self.reset()

    def reset(self):
        np = self.np
        bins = self.FRAME // 2 + 1
        self.prev = np.zeros(self.HOP)
        self.ola = np.zeros(self.HOP)
        self.noise = None
        self.ps = np.zeros(bins)
        self.gain = np.ones(bins)

    def process(self, x):
        """Processa exatamente HOP amostras (float, -1..1) e devolve HOP amostras."""
        np = self.np
        frame = np.concatenate((self.prev, x))
        self.prev = np.array(x, dtype=float)
        spec = np.fft.rfft(frame * self.win)
        p = spec.real ** 2 + spec.imag ** 2 + 1e-12
        if self.FREQ_SMOOTH:
            p = np.convolve(p, self.kernel, mode="same")  # média entre frequências vizinhas

        if self.noise is None:
            self.noise = p.copy()
            self.ps = p.copy()
        self.ps = self.PS_SMOOTH * self.ps + (1.0 - self.PS_SMOOTH) * p
        # piso de ruído: desce rápido quando a energia cai, sobe devagar
        self.noise = np.where(self.ps < self.noise,
                              (1.0 - self.NOISE_DOWN) * self.noise + self.NOISE_DOWN * self.ps,
                              self.noise * self.NOISE_UP)

        t = max(0.0, min(1.0, self.intensity / 100.0))
        if t <= 0.0:
            self.gain = np.ones_like(self.gain)
            g = self.gain
        else:
            beta = self.BETA_MIN + self.BETA_RANGE * t  # agressividade
            gmin = 10 ** ((-6.0 - 24.0 * t) / 20.0)     # atenuação máxima: -6 dB a -30 dB
            pu = self.PU_MIX * p + (1.0 - self.PU_MIX) * self.ps
            # relação sinal/ruído por frequência -> ganho suave (estilo Wiener)
            xi = np.maximum(pu / (self.BIAS * self.noise) - 1.0, 0.0)
            target = np.clip(xi / (xi + beta), gmin, 1.0)
            # abre rápido (início de fala) e fecha devagar (cauda das palavras)
            self.gain = np.where(target > self.gain,
                                 (1.0 - self.ATTACK) * self.gain + self.ATTACK * target,
                                 (1.0 - self.RELEASE) * self.gain + self.RELEASE * target)
            g = np.where(self.freqs < 75.0, np.minimum(self.gain, gmin), self.gain)

        y = np.fft.irfft(spec * g, n=self.FRAME) * self.win
        out = self.ola + y[: self.HOP]
        self.ola = y[self.HOP:]
        return out


class NoiseSuppressor:
    """Captura o microfone, limpa o áudio e envia para uma saída (microfone virtual)."""

    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.reducer = None
        self.stream = None
        self.running = False
        self.in_level = 0.0
        self.out_level = 0.0
        self._inbuf = None
        self._outbuf = None

    @staticmethod
    def available():
        try:
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
            return True, ""
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def devices():
        """Dispositivos da API de áudio padrão: ([(idx, nome)], [(idx, nome)])."""
        import sounddevice as sd

        host = sd.query_devices(kind="input")["hostapi"]
        ins, outs = [], []
        for i, d in enumerate(sd.query_devices()):
            if d["hostapi"] != host:
                continue
            if d["max_input_channels"] > 0:
                ins.append((i, d["name"]))
            if d["max_output_channels"] > 0:
                outs.append((i, d["name"]))
        return ins, outs

    @staticmethod
    def default_input_name():
        import sounddevice as sd

        return sd.query_devices(kind="input")["name"]

    @staticmethod
    def guess_virtual(outs):
        for _, name in outs:
            low = name.lower()
            if "cable input" in low or "vb-audio" in low or ("voicemeeter" in low and "input" in low):
                return name
        return None

    def set_intensity(self, value):
        if self.reducer is None:
            self.reducer = NoiseReducer()
        self.reducer.intensity = int(value)

    def start(self, in_name, out_name):
        """Devolve None se deu certo, ou uma mensagem de erro."""
        self.stop()
        ok, err = self.available()
        if not ok:
            return f"Faltam bibliotecas: {err}"
        import sounddevice as sd

        try:
            ins, outs = self.devices()
        except Exception as exc:
            return f"Não consegui listar os dispositivos de áudio: {exc}"
        in_idx = next((i for i, n in ins if n == in_name), None)
        out_idx = next((i for i, n in outs if n == out_name), None)
        if in_idx is None:
            return "Escolha o seu microfone."
        if out_idx is None:
            return "Escolha a saída (o microfone virtual, ex.: CABLE Input)."

        if self.reducer is None:
            self.reducer = NoiseReducer()
        self.reducer.reset()
        np = self.reducer.np
        self._inbuf = np.zeros(0)
        # pré-enche 10 ms: garante que a saída nunca fique sem amostras (evita estalos)
        self._outbuf = np.zeros(NoiseReducer.HOP)
        out_ch = max(1, min(2, int(sd.query_devices(out_idx)["max_output_channels"])))
        try:
            self.stream = sd.Stream(samplerate=NoiseReducer.RATE, blocksize=NoiseReducer.HOP,
                                    device=(in_idx, out_idx), channels=(1, out_ch),
                                    dtype="float32", latency="low", callback=self._callback)
            self.stream.start()
        except Exception as exc:
            self.stream = None
            return f"Não consegui abrir o áudio: {exc}"
        self.running = True
        return None

    def _callback(self, indata, outdata, frames, time_info, status):
        np = self.reducer.np
        hop = NoiseReducer.HOP
        try:
            x = np.asarray(indata[:, 0], dtype=float)
            self.in_level = 0.7 * self.in_level + 0.3 * float(np.sqrt(np.mean(x * x)))
            self._inbuf = np.concatenate((self._inbuf, x))
            while len(self._inbuf) >= hop:
                block = self._inbuf[:hop]
                self._inbuf = self._inbuf[hop:]
                self._outbuf = np.concatenate((self._outbuf, self.reducer.process(block)))
            y = np.zeros(frames)
            n = min(frames, len(self._outbuf))
            y[:n] = self._outbuf[:n]
            self._outbuf = self._outbuf[n:]
            if self.ctrl.last_muted:
                y[:] = 0.0
            y = np.clip(y, -1.0, 1.0)
            self.out_level = 0.7 * self.out_level + 0.3 * float(np.sqrt(np.mean(y * y)))
            outdata[:] = y.astype("float32")[:, None]
        except Exception:
            outdata[:] = 0

    def stop(self):
        self.running = False
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.in_level = 0.0
        self.out_level = 0.0


# --------------------------------------------------------------------------
# Atualização automática
# --------------------------------------------------------------------------
_UA = {"User-Agent": f"{APP_NAME}/{APP_VERSION}"}


def parse_version(text):
    return tuple(int(x) for x in re.findall(r"\d+", str(text))[:4])


def update_enabled():
    return bool(UPDATE_URL) and "SEU-SITE" not in UPDATE_URL


def site_url():
    return urljoin(UPDATE_URL, "./")


def _safe_url(url):
    p = urlparse(url)
    return p.scheme == "https" or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1"))


def check_for_update():
    """Devolve um dict com os dados da versão nova, ou None (sem versão nova ou sem internet)."""
    if not update_enabled():
        return None
    try:
        req = urllib.request.Request(UPDATE_URL, headers=_UA)
        with urllib.request.urlopen(req, timeout=8) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        if parse_version(info["version"]) <= parse_version(APP_VERSION):
            return None
        info["url"] = urljoin(UPDATE_URL, info.get("url") or "dist/Mutinho.exe")
        return info
    except Exception:
        return None


def can_self_update():
    """Só o .exe empacotado no Windows, em pasta onde o usuário pode gravar, troca a si mesmo."""
    if not (getattr(sys, "frozen", False) and SYSTEM == "Windows"):
        return False
    return os.access(os.path.dirname(sys.executable), os.W_OK)


def update_path():
    return os.path.join(os.path.dirname(sys.executable), "Mutinho.update")


def download_update(info, progress=None, dest=None):
    """Baixa o .exe novo e confere o SHA-256. Levanta exceção se algo estiver errado."""
    url = info.get("url", "")
    sha = str(info.get("sha256", "")).strip().lower()
    if not _safe_url(url):
        raise ValueError("endereço de download inseguro")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("a atualização não tem verificação de integridade")
    dest = dest or update_path()
    digest = hashlib.sha256()
    done = 0
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if digest.hexdigest() != sha:
            raise ValueError("o arquivo baixado não confere (verificação de integridade falhou)")
    except Exception:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    return dest


def apply_update(new_path):
    """Troca o .exe depois que o app fechar e abre de novo (feito por um script .bat)."""
    exe = sys.executable
    bat = os.path.join(tempfile.gettempdir(), "mutinho_update.bat")
    script = (
        "@echo off\r\n"
        "set TRIES=0\r\n"
        ":wait\r\n"
        "ping 127.0.0.1 -n 2 > nul\r\n"
        f'move /Y "{new_path}" "{exe}" > nul 2>&1\r\n'
        "if not errorlevel 1 goto done\r\n"
        "set /a TRIES+=1\r\n"
        "if %TRIES% LSS 40 goto wait\r\n"
        ":done\r\n"
        f'start "" "{exe}"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )
    with open(bat, "w", encoding="mbcs", errors="replace") as f:
        f.write(script)
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")  # o novo .exe começa do zero
    subprocess.Popen(["cmd", "/c", bat], env=env, close_fds=True,
                     creationflags=0x08000000 | 0x00000008)  # sem janela + desanexado


# --------------------------------------------------------------------------
# Ícone na bandeja do sistema (perto do relógio)
# --------------------------------------------------------------------------
class Tray:
    """Ícone na bandeja com menu. Se pystray/Pillow faltarem, start() devolve False."""

    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.icon = None
        self.muted = False

    @staticmethod
    def _image(muted):
        from PIL import Image, ImageDraw

        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((2, 2, size - 2, size - 2), fill="#ef4444" if muted else "#22c55e")
        d.rounded_rectangle((25, 13, 39, 36), radius=7, fill="white")        # corpo do microfone
        d.arc((19, 24, 45, 46), start=0, end=180, fill="white", width=3)      # haste
        d.line((32, 46, 32, 52), fill="white", width=3)
        d.line((25, 52, 39, 52), fill="white", width=3)
        if muted:
            d.line((14, 50, 50, 14), fill="white", width=5)                   # risco de mudo
        return img

    def start(self):
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem("Abrir Mutinho", lambda icon, item: self.ctrl.events.put(("show", None)),
                                 default=True),
                pystray.MenuItem("Tela cheia", lambda icon, item: self.ctrl.events.put(("fullscreen", None))),
                pystray.MenuItem(lambda item: "Desmutar" if self.muted else "Mutar",
                                 lambda icon, item: self.ctrl.safe_toggle()),
                pystray.MenuItem("Sair", lambda icon, item: self.ctrl.events.put(("quit", None))),
            )
            self.icon = pystray.Icon(APP_NAME, self._image(False), f"{APP_NAME} - microfone ligado", menu)
            self.icon.run_detached()
            return True
        except Exception:
            self.icon = None
            return False

    def update(self, muted):
        self.muted = muted
        if self.icon is not None:
            try:
                self.icon.icon = self._image(muted)
                self.icon.title = f"{APP_NAME} - " + ("MUTADO" if muted else "microfone ligado")
            except Exception:
                pass

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None


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

    # nome: (tamanho em pontos, negrito). Todas escalam juntas na tela cheia.
    FONT_SPECS = {
        "h8": (8, False), "h9": (9, False), "h9b": (9, True), "h10b": (10, True),
        "h11b": (11, True), "h13b": (13, True), "h15b": (15, True), "h54": (54, False),
    }

    def __init__(self, ctrl, server, hotkeys, ptt, start_hidden=False, start_fullscreen=False):
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.tkfont = tkfont
        self.ctrl, self.server, self.hotkeys, self.ptt = ctrl, server, hotkeys, ptt
        self.cfg = ctrl.cfg
        self.muted = False
        self.recording = False
        self.indicator = None
        self.tray = None
        self.update_info = None
        self.updating = False
        self.fullscreen = False
        self.scale = 1.0
        self.holding = False
        self.hold_t0 = 0.0
        self.maxed = False          # janela maximizada (botão quadrado): usa o layout grande
        self._maxed_size = None
        self._was_zoomed = False
        self._cfg_job = None
        self.rec_key = "hotkey"

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.configure(bg=self.BG)
        self.root.resizable(True, True)
        self.root.attributes("-topmost", bool(self.cfg["on_top"]))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.root.bind("<Escape>", lambda e: self._exit_fullscreen())
        self.root.bind("<Configure>", self._on_configure)

        self._make_fonts()
        self._build()
        self._set_normal_size(force=True)

        # A bandeja não é usada no macOS (conflita com o tkinter)
        if SYSTEM != "Darwin":
            tray = Tray(ctrl)
            if tray.start():
                self.tray = tray

        self.refresh()
        self.root.after(100, self._pump_events)
        self.root.after(1500, self._poll_state)
        self._start_update_check()
        if self.cfg["indicator"]:
            self._set_indicator(True)

        hidden = False
        if self.tray is not None:
            self._say("Fechar a janela mantém o Mutinho na bandeja. Para sair, use o ícone perto do relógio.")
            if start_hidden or self.cfg["start_hidden"]:
                self.root.withdraw()
                hidden = True
        else:
            self._say("Bandeja indisponível (instale pystray e pillow). Fechar a janela encerra o app.")
        if start_fullscreen and not hidden:
            self.root.after(300, self._enter_fullscreen)

    # ---- construção -------------------------------------------------------
    def _button(self, parent, text, command, bg=None):
        tk = self.tk
        bg = bg or self.ACCENT
        lbl = tk.Label(parent, text=text, bg=bg, fg="white", padx=14, pady=6,
                       font=self.fonts["h10b"], cursor="hand2")
        lbl.bind("<Button-1>", lambda e: command())
        return lbl

    def _card(self, parent):
        tk = self.tk
        frame = tk.Frame(parent, bg=self.CARD, padx=16, pady=14)
        frame.pack(fill="x", padx=18, pady=(0, 12))
        return frame

    def _build(self):
        tk = self.tk
        self.container = tk.Frame(self.root, bg=self.BG)
        self.container.place(relx=0.5, rely=0.5, anchor="center")
        self.panelA = tk.Frame(self.container, bg=self.BG)   # controle principal
        self.panelB = tk.Frame(self.container, bg=self.BG)   # opções e celular
        self.panelC = tk.Frame(self.container, bg=self.BG)   # redução de ruído
        self.msg = tk.Label(self.container, text="", bg=self.BG, fg=self.DIM,
                            font=self.fonts["h9"], wraplength=340)

        # ===== painel A: controle principal =====
        A = self.panelA
        self.top = tk.Frame(A, bg=self.BG)
        self.top.pack(fill="x", padx=18, pady=(16, 6))
        self.header = tk.Label(self.top, text=f"MUTINHO  v{APP_VERSION}", bg=self.BG, fg=self.DIM,
                               font=self.fonts["h11b"])
        self.header.pack(side="left")
        self.fs_btn = self._button(self.top, "Tela cheia (F11)", self._toggle_fullscreen, bg="#4b5563")
        self.fs_btn.pack(side="right", padx=(16, 0))

        # aviso de atualização (só aparece quando há versão nova)
        self.update_bar = tk.Frame(A, bg="#14301f", padx=12, pady=8)
        self.update_text = tk.Label(self.update_bar, text="", bg="#14301f", fg=self.FG,
                                    font=self.fonts["h9b"])
        self.update_text.pack(side="left", padx=(0, 10))
        self.update_btn = self._button(self.update_bar, "Atualizar agora", self._do_update,
                                       bg=self.LIVE)
        self.update_btn.pack(side="right")

        self.circle = tk.Canvas(A, width=160, height=160, bg=self.BG,
                                highlightthickness=0, cursor="hand2")
        self.circle.pack()
        self.circle_shape = self.circle.create_oval(10, 10, 150, 150, fill=self.LIVE, outline="")
        self.circle_icon = self.circle.create_text(80, 80, text="🎙", font=self.fonts["h54"], fill="white")
        self.circle.bind("<ButtonPress-1>", self._circle_press)
        self.circle.bind("<ButtonRelease-1>", self._circle_release)

        self.status = tk.Label(A, text="", bg=self.BG, fg=self.FG, font=self.fonts["h15b"])
        self.status.pack(pady=(8, 10))

        # seletor de modo
        seg = tk.Frame(A, bg=self.CARD, padx=3, pady=3)
        seg.pack(pady=(0, 12))
        self.mode_btns = {}
        for key, text in (("toggle", "Alternar"), ("ptt", "Apertar para falar")):
            lbl = tk.Label(seg, text=text, padx=14, pady=6, font=self.fonts["h10b"], cursor="hand2")
            lbl.bind("<Button-1>", lambda e, k=key: self._set_mode(k))
            lbl.pack(side="left")
            self.mode_btns[key] = lbl
        self._style_mode()

        # atalho (do modo atual)
        card = self._card(A)
        self.hotkey_title = tk.Label(card, text="", bg=self.CARD, fg=self.DIM, font=self.fonts["h9"])
        self.hotkey_title.pack(anchor="w")
        row = tk.Frame(card, bg=self.CARD)
        row.pack(fill="x", pady=(4, 0))
        self.hotkey_label = tk.Label(row, text="", bg=self.CARD, fg=self.FG, font=self.fonts["h13b"])
        self.hotkey_label.pack(side="left")
        self.record_btn = self._button(row, "Alterar", lambda: self._start_recording("main"))
        self.record_btn.pack(side="right")

        # segunda tecla: falar segurando enquanto estiver mutado (só no modo alternar)
        self.hold_box = tk.Frame(card, bg=self.CARD)
        self.hold_title = tk.Label(self.hold_box, text="Falar enquanto mutado (segure)", bg=self.CARD,
                                   fg=self.DIM, font=self.fonts["h9"])
        self.hold_title.pack(anchor="w", pady=(10, 0))
        hrow = tk.Frame(self.hold_box, bg=self.CARD)
        hrow.pack(fill="x", pady=(4, 0))
        self.hold_label = tk.Label(hrow, text="", bg=self.CARD, fg=self.FG, font=self.fonts["h13b"])
        self.hold_label.pack(side="left")
        self.hold_btn = self._button(hrow, "Alterar", lambda: self._start_recording("hold"))
        self.hold_btn.pack(side="right")
        self._update_hotkey_label()

        # ===== painel B: opções e celular =====
        B = self.panelB
        card = self._card(B)
        self.var_sound = tk.BooleanVar(value=bool(self.cfg["sound"]))
        self.var_top = tk.BooleanVar(value=bool(self.cfg["on_top"]))
        self.var_ind = tk.BooleanVar(value=bool(self.cfg["indicator"]))
        self.var_hidden = tk.BooleanVar(value=bool(self.cfg["start_hidden"]))
        self.var_talkm = tk.BooleanVar(value=bool(self.cfg.get("talk_muted", True)))
        for text, var, cmd in (
            ("Segurar para falar estando mutado", self.var_talkm, self._opt_talk_muted),
            ("Som ao mutar/desmutar", self.var_sound, self._opt_sound),
            ("Manter janela sempre no topo", self.var_top, self._opt_top),
            ("Indicador flutuante na tela", self.var_ind, self._opt_indicator),
            ("Iniciar escondido na bandeja", self.var_hidden, self._opt_hidden),
        ):
            tk.Checkbutton(card, text=text, variable=var, command=cmd, bg=self.CARD, fg=self.FG,
                           selectcolor=self.BG, activebackground=self.CARD, activeforeground=self.FG,
                           highlightthickness=0, bd=0, anchor="w").pack(fill="x")

        card = self._card(B)
        tk.Label(card, text="Controle pelo celular", bg=self.CARD, fg=self.DIM,
                 font=self.fonts["h9"]).pack(anchor="w")
        self.qr_canvas = tk.Canvas(card, bg="white", highlightthickness=0)
        self.qr_canvas.pack(pady=(8, 6))
        self.url_label = tk.Label(card, text="", bg=self.CARD, fg=self.DIM,
                                  font=self.fonts["h8"], wraplength=300)
        self.url_label.pack()
        tk.Label(card, text="Celular e PC precisam estar no mesmo Wi-Fi.", bg=self.CARD, fg=self.DIM,
                 font=self.fonts["h8"]).pack(pady=(2, 8))
        btns = tk.Frame(card, bg=self.CARD)
        btns.pack()
        self._button(btns, "Copiar link", self._copy_url).pack(side="left", padx=4)
        self._button(btns, "Gerar novo link", self._new_link, bg="#4b5563").pack(side="left", padx=4)
        self._draw_qr()

        # ===== painel C: redução de ruído =====
        self._build_noise(self.panelC)

        self._layout(False)

    # ---- QR ---------------------------------------------------------------
    def _draw_qr(self):
        import qrcode

        url = self.server.url()
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        cell = max(2, self.S(200) // len(matrix))
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

    # ---- botão do círculo, modo e atalho ----------------------------------
    def _circle_press(self, event):
        mode = self.ctrl.get_mode()
        if mode == "ptt" or (self.muted and self.cfg.get("talk_muted", True)):
            # segurar fala; no modo alternar, um toque rápido desmuta de vez
            self.holding = True
            self.hold_t0 = time.monotonic()
            self.ctrl.talk_async(True)
        else:
            self._click_toggle()

    def _circle_release(self, event):
        if not self.holding:
            return
        self.holding = False
        if self.ctrl.get_mode() != "ptt" and time.monotonic() - self.hold_t0 < TAP_SECONDS:
            self._click_toggle()  # toque rápido = desmutar (a marca de "só segurando" cai)
        else:
            self.ctrl.talk_async(False)

    def _style_mode(self):
        current = self.ctrl.get_mode()
        for key, lbl in self.mode_btns.items():
            on = key == current
            lbl.configure(bg=self.ACCENT if on else self.CARD, fg="white" if on else self.DIM)

    def _set_mode(self, mode):
        if mode == self.ctrl.get_mode():
            return

        def work():
            try:
                self.ctrl.set_mode(mode)
            except Exception as exc:
                self.ctrl.events.put(("msg", f"Erro ao trocar de modo: {exc}"))
            else:
                self.ctrl.events.put(("mode", None))

        threading.Thread(target=work, daemon=True).start()

    def _on_mode_changed(self):
        try:
            activate_hotkeys(self.cfg, self.hotkeys, self.ptt)
        except Exception as exc:
            self._say(f"Atalho indisponível: {exc}")
        else:
            if self.ctrl.get_mode() == "ptt":
                self._say(f"Segure {pretty_combo(self.cfg['ptt_hotkey'])} (ou o círculo) para falar.")
            else:
                self._say("Modo alternar: o atalho liga e desliga o microfone."
                          + (f" Mutado, segure {pretty_combo(self.cfg['ptt_hotkey'])} para falar."
                             if self.cfg.get("talk_muted", True) else ""))
        self._style_mode()
        self._update_hotkey_label()
        self.refresh()

    def _update_hotkey_label(self):
        ptt = self.cfg["mode"] == "ptt"
        self.hotkey_title.configure(text="Tecla para falar (segure)" if ptt else "Atalho global")
        self.hotkey_label.configure(text=pretty_combo(self.cfg["ptt_hotkey" if ptt else "hotkey"]))
        self.hold_label.configure(text=pretty_combo(self.cfg["ptt_hotkey"]))
        if not ptt and self.cfg.get("talk_muted", True):
            self.hold_box.pack(fill="x")
        else:
            self.hold_box.pack_forget()
        self._set_normal_size()

    # ---- fontes, escala e tela cheia ---------------------------------------
    def _make_fonts(self):
        tkfont = self.tkfont
        self.fonts = {}
        for name, (size, bold) in self.FONT_SPECS.items():
            self.fonts[name] = tkfont.Font(root=self.root, family="Helvetica", size=size,
                                           weight="bold" if bold else "normal")
        # fontes padrão do Tk (caixas de seleção, menus) também precisam escalar
        self.default_fonts = []
        for fname in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                f = tkfont.nametofont(fname)
                self.default_fonts.append((f, abs(int(f.cget("size"))) or 9))
            except Exception:
                pass

    def S(self, n):
        return int(round(n * self.scale))

    def _set_scale(self, scale):
        self.scale = scale
        for name, (size, _bold) in self.FONT_SPECS.items():
            self.fonts[name].configure(size=max(6, round(size * scale)))
        for f, base in self.default_fonts:
            f.configure(size=max(6, round(base * scale)))
        self._apply_sizes()

    def _apply_sizes(self):
        S = self.S
        size = S(160)
        self.circle.configure(width=size, height=size)
        self.circle.coords(self.circle_shape, S(10), S(10), size - S(10), size - S(10))
        self.circle.coords(self.circle_icon, size // 2, size // 2)
        self.url_label.configure(wraplength=S(300))
        self.msg.configure(wraplength=S(900) if self.fullscreen else 340)
        self._draw_qr()
        if hasattr(self, "ns_scale"):
            self.ns_scale.configure(length=S(290), sliderlength=S(22), width=S(14))
            self.ns_meter.configure(width=S(290), height=S(36))
            self.ns_status.configure(wraplength=S(290))
            self.ns_help.configure(wraplength=S(290))

    def _layout(self, full):
        S = self.S
        for w in (self.panelA, self.panelB, self.panelC, self.msg):
            w.grid_forget()
        for col in range(3):
            self.container.grid_columnconfigure(col, minsize=0)
        if full:
            # três colunas lado a lado, ocupando a tela
            self.panelA.grid(row=0, column=0, sticky="n", padx=S(8))
            self.panelB.grid(row=0, column=1, sticky="n", padx=S(8))
            self.panelC.grid(row=0, column=2, sticky="n", padx=S(8))
            self.msg.grid(row=1, column=0, columnspan=3, pady=(S(8), 0))
            for col in range(3):
                self.container.grid_columnconfigure(col, minsize=S(340))
        else:
            self.panelA.grid(row=0, column=0, sticky="n")
            self.panelB.grid(row=1, column=0, sticky="n")
            self.panelC.grid(row=0, column=1, rowspan=2, sticky="n", pady=(44, 0), padx=(0, 6))
            self.msg.grid(row=2, column=0, columnspan=2, pady=(0, 12))
            self.container.grid_columnconfigure(0, minsize=340)

    def _fit_to_area(self, aw, ah):
        """Escolhe o maior zoom (1x a 2,2x) em que as três colunas cabem na área dada."""
        root = self.root
        self._set_scale(1.0)
        root.update_idletasks()
        w = max(1, self.container.winfo_reqwidth())
        h = max(1, self.container.winfo_reqheight())
        scale = max(1.0, min(2.2, aw * 0.94 / w, ah * 0.94 / h))
        for _ in range(8):
            self._set_scale(scale)
            root.update_idletasks()
            fits = (self.container.winfo_reqwidth() <= aw * 0.97
                    and self.container.winfo_reqheight() <= ah * 0.97)
            if fits or scale <= 1.0:
                break
            scale = max(1.0, scale * 0.92)

    def _fit_to_screen(self):
        self._fit_to_area(self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    # ---- tamanho da janela e botão de maximizar -----------------------------
    def _set_normal_size(self, force=False):
        """Tamanho mínimo = o conteúdo. Com force, também volta ao tamanho natural."""
        if self.fullscreen or self.maxed:
            return
        self.root.update_idletasks()
        w = self.container.winfo_reqwidth() + 32
        h = self.container.winfo_reqheight() + 20
        self.root.minsize(w, h)
        if force:
            self.root.geometry(f"{w}x{h}")
        else:
            cw, ch = self.root.winfo_width(), self.root.winfo_height()
            if cw > 1 and (cw < w or ch < h):
                self.root.geometry(f"{max(cw, w)}x{max(ch, h)}")

    def _is_maximized(self):
        try:
            if self.root.state() == "zoomed":  # Windows e macOS
                return True
        except Exception:
            pass
        try:
            return bool(self.root.attributes("-zoomed"))  # Linux (X11)
        except Exception:
            return False

    def _on_configure(self, event):
        if event.widget is not self.root or self.fullscreen:
            return
        if self._cfg_job is not None:
            try:
                self.root.after_cancel(self._cfg_job)
            except Exception:
                pass
        self._cfg_job = self.root.after(120, self._sync_maximized)

    def _sync_maximized(self):
        """Maximizada: usa as três colunas e aumenta o zoom até ocupar a janela. Restaurada: volta ao normal."""
        self._cfg_job = None
        if self.fullscreen:
            return
        if self._is_maximized():
            size = (self.root.winfo_width(), self.root.winfo_height())
            if not self.maxed or size != self._maxed_size:
                self.maxed = True
                self._maxed_size = size
                self.root.minsize(1, 1)
                self._layout(True)
                self._fit_to_area(*size)
        elif self.maxed:
            self.maxed = False
            self._maxed_size = None
            self._set_scale(1.0)
            self._layout(False)
            self._set_normal_size(force=True)

    def _toggle_fullscreen(self):
        if self.fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        if self.fullscreen:
            return
        self._was_zoomed = self.maxed or self._is_maximized()
        self.fullscreen = True
        self.maxed = False
        self.root.deiconify()
        self.root.minsize(1, 1)
        self.root.attributes("-fullscreen", True)
        self._layout(True)
        self._fit_to_screen()
        self.fs_btn.configure(text="Sair da tela cheia (Esc)")
        self.root.lift()
        self.root.focus_force()

    def _exit_fullscreen(self):
        if not self.fullscreen:
            return
        self.fullscreen = False
        self.root.attributes("-fullscreen", False)
        self._set_scale(1.0)
        self._layout(False)
        self.fs_btn.configure(text="Tela cheia (F11)")
        if self._was_zoomed:
            self.root.after(250, self._sync_maximized)  # voltou maximizada: refaz o zoom
        else:
            self._set_normal_size(force=True)

    def refresh(self):
        try:
            self.muted = self.ctrl.is_muted()
        except Exception as exc:
            self._say(f"Não consegui ler o microfone: {exc}")
            return
        self.ctrl.last_muted = self.muted
        color = self.MUTE if self.muted else self.LIVE
        if self.ctrl.get_mode() == "ptt":
            text = "Segure para falar" if self.muted else "FALANDO…"
        else:
            text = "Microfone MUTADO" if self.muted else "Microfone LIGADO"
        self.circle.itemconfigure(self.circle_shape, fill=color)
        self.circle.itemconfigure(self.circle_icon, text="🔇" if self.muted else "🎙")
        self.status.configure(text=text, fg=color)
        if self.indicator is not None:
            self.indicator_label.configure(
                text="● MUTADO" if self.muted else "● MIC LIGADO", fg=color)
        if self.tray is not None:
            self.tray.update(self.muted)

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
                elif kind == "show":
                    self.show()
                elif kind == "quit":
                    self.quit()
                    return
                elif kind == "mode":
                    self._on_mode_changed()
                elif kind == "fullscreen":
                    self.show()
                    self._enter_fullscreen()
                elif kind == "update":
                    self._show_update(payload)
                elif kind == "update_progress":
                    done, total = payload
                    pct = f"{int(done * 100 / total)}%" if total else "…"
                    self.update_btn.configure(text=f"Baixando {pct}")
                elif kind == "update_error":
                    self.updating = False
                    self.update_btn.configure(text="Tentar de novo")
                    self._say(f"Não foi possível atualizar: {payload}")
                elif kind == "update_ready":
                    try:
                        apply_update(payload)
                    except Exception as exc:
                        self.updating = False
                        self.update_btn.configure(text="Tentar de novo")
                        self._say(f"Não foi possível aplicar a atualização: {exc}")
                    else:
                        self.quit()
                        return
        except queue.Empty:
            pass
        self.root.after(100, self._pump_events)

    # ---- atalho -----------------------------------------------------------
    def _start_recording(self, which="main"):
        if self.recording:
            return
        ptt = self.cfg["mode"] == "ptt"
        self.rec_key = "ptt_hotkey" if (ptt or which == "hold") else "hotkey"
        self.rec_which = which
        try:
            record_hotkey(lambda combo: self.ctrl.events.put(("hotkey", combo)))
        except Exception as exc:
            self._say(f"Não foi possível gravar o atalho: {exc}")
            return
        self.recording = True
        if which == "hold":
            self.hold_label.configure(text="Pressione a nova tecla…")
            self.hold_btn.configure(text="Gravando…")
        else:
            self.hotkey_label.configure(text="Pressione o novo atalho…")
            self.record_btn.configure(text="Gravando…")

    def _apply_hotkey(self, combo):
        self.recording = False
        self.record_btn.configure(text="Alterar")
        self.hold_btn.configure(text="Alterar")
        key = self.rec_key
        previous = self.cfg[key]
        self.cfg[key] = combo
        try:
            activate_hotkeys(self.cfg, self.hotkeys, self.ptt)
            self._say("Atalho atualizado.")
        except Exception as exc:
            self.cfg[key] = previous
            self._say(f"Atalho inválido ({exc}). Mantido o anterior.")
            try:
                activate_hotkeys(self.cfg, self.hotkeys, self.ptt)
            except Exception:
                pass
        self._update_hotkey_label()

    # ---- opções -----------------------------------------------------------
    def _opt_talk_muted(self):
        self.cfg["talk_muted"] = bool(self.var_talkm.get())
        if not self.cfg["talk_muted"]:
            try:
                self.ctrl.release_temp()  # não deixa o microfone aberto
            except Exception:
                pass
        try:
            activate_hotkeys(self.cfg, self.hotkeys, self.ptt)
        except Exception as exc:
            self._say(f"Atalho indisponível: {exc}")
        self._update_hotkey_label()
        self.refresh()
        self.cfg.save()

    def _opt_sound(self):
        self.cfg["sound"] = bool(self.var_sound.get())

    def _opt_top(self):
        self.cfg["on_top"] = bool(self.var_top.get())
        self.root.attributes("-topmost", bool(self.var_top.get()))

    def _opt_hidden(self):
        self.cfg["start_hidden"] = bool(self.var_hidden.get())

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
                                            font=self.fonts["h10b"])
            self.indicator_label.pack(expand=True, fill="both")
            self.indicator = win
            self.refresh()
        elif not on and self.indicator is not None:
            self.indicator.destroy()
            self.indicator = None

    # ---- ciclo de vida ----------------------------------------------------
    # ---- atualização automática -------------------------------------------
    def _start_update_check(self):
        if not update_enabled():
            return

        def work():
            info = check_for_update()
            if info:
                self.ctrl.events.put(("update", info))

        threading.Thread(target=work, daemon=True).start()

    def _show_update(self, info):
        self.update_info = info
        self.update_text.configure(text=f"Nova versão {info['version']} disponível")
        self.update_btn.configure(text="Atualizar agora" if can_self_update() else "Baixar no site")
        self.update_bar.pack(after=self.top, fill="x", padx=18, pady=(0, 6))
        notes = info.get("notes")
        if notes:
            self._say(f"Novidades: {notes}")

    def _do_update(self):
        if self.updating or not self.update_info:
            return
        if not can_self_update():
            webbrowser.open(site_url())
            return
        self.updating = True
        self.update_btn.configure(text="Baixando…")
        info = self.update_info

        def work():
            try:
                path = download_update(
                    info, lambda d, t: self.ctrl.events.put(("update_progress", (d, t))))
                self.ctrl.events.put(("update_ready", path))
            except Exception as exc:
                self.ctrl.events.put(("update_error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # ---- redução de ruído -------------------------------------------------
    def _style_menu(self, menu_button):
        menu_button.configure(bg=self.BG, fg=self.FG, activebackground=self.ACCENT,
                              activeforeground="white", highlightthickness=0, bd=0,
                              relief="flat", anchor="w", width=34)
        menu_button["menu"].configure(bg=self.CARD, fg=self.FG, activebackground=self.ACCENT,
                                      activeforeground="white", bd=0)

    def _build_noise(self, parent):
        tk = self.tk
        self.ns = NoiseSuppressor(self.ctrl)
        card = self._card(parent)
        tk.Label(card, text="Redução de ruído", bg=self.CARD, fg=self.DIM,
                 font=self.fonts["h9"]).pack(anchor="w")

        ok, _err = NoiseSuppressor.available()
        if not ok:
            tk.Label(card, text="Recurso opcional. Para ativar, instale as bibliotecas:\n\n"
                                "python -m pip install numpy sounddevice\n\n"
                                "e abra o Mutinho de novo.",
                     bg=self.CARD, fg=self.DIM, justify="left", wraplength=290,
                     font=self.fonts["h9"]).pack(anchor="w", pady=(8, 0))
            return

        self.var_ns = tk.BooleanVar(value=False)
        tk.Checkbutton(card, text="Ativar redução de ruído", variable=self.var_ns,
                       command=self._ns_toggle, bg=self.CARD, fg=self.FG, selectcolor=self.BG,
                       activebackground=self.CARD, activeforeground=self.FG,
                       highlightthickness=0, bd=0, anchor="w").pack(fill="x", pady=(6, 0))

        self.ns_label = tk.Label(card, text="", bg=self.CARD, fg=self.FG,
                                 font=self.fonts["h10b"])
        self.ns_label.pack(anchor="w", pady=(8, 0))
        self.ns_scale = tk.Scale(card, from_=0, to=100, orient="horizontal", showvalue=False,
                                 length=290, command=self._ns_intensity, bg=self.CARD, fg=self.FG,
                                 troughcolor=self.BG, highlightthickness=0, bd=0, sliderlength=22,
                                 width=14, activebackground=self.ACCENT)
        self.ns_scale.set(int(self.cfg["ns_intensity"]))
        self.ns_scale.bind("<ButtonRelease-1>", lambda e: self._ns_save_intensity())
        self.ns_scale.pack(fill="x")
        ends = tk.Frame(card, bg=self.CARD)
        ends.pack(fill="x")
        tk.Label(ends, text="Leve", bg=self.CARD, fg=self.DIM, font=self.fonts["h8"]).pack(side="left")
        tk.Label(ends, text="Forte", bg=self.CARD, fg=self.DIM, font=self.fonts["h8"]).pack(side="right")

        tk.Label(card, text="Seu microfone", bg=self.CARD, fg=self.DIM,
                 font=self.fonts["h9"]).pack(anchor="w", pady=(10, 2))
        self.ns_in = tk.StringVar()
        self.ns_in_menu = tk.OptionMenu(card, self.ns_in, "")
        self._style_menu(self.ns_in_menu)
        self.ns_in_menu.pack(fill="x")

        tk.Label(card, text="Enviar o áudio limpo para (microfone virtual)", bg=self.CARD, fg=self.DIM,
                 font=self.fonts["h9"]).pack(anchor="w", pady=(10, 2))
        self.ns_out = tk.StringVar()
        self.ns_out_menu = tk.OptionMenu(card, self.ns_out, "")
        self._style_menu(self.ns_out_menu)
        self.ns_out_menu.pack(fill="x")

        self.ns_meter = tk.Canvas(card, width=290, height=36, bg=self.CARD, highlightthickness=0)
        self.ns_meter.pack(pady=(12, 0))

        self.ns_status = tk.Label(card, text="", bg=self.CARD, fg=self.DIM, wraplength=290,
                                  justify="left", font=self.fonts["h9b"])
        self.ns_status.pack(anchor="w", pady=(6, 0))
        self.ns_help = tk.Label(card, text="", bg=self.CARD, fg=self.DIM, wraplength=290,
                                justify="left", font=self.fonts["h8"])
        self.ns_help.pack(anchor="w", pady=(4, 8))
        self._button(card, "Atualizar dispositivos", self._ns_refresh_devices,
                     bg="#4b5563").pack(anchor="w")

        self._ns_refresh_devices()
        if self.cfg["ns_enabled"]:
            self.var_ns.set(True)
            self._ns_toggle()
        self.root.after(200, self._ns_meter)

    def _fill_menu(self, menu_button, var, names, preferred, fallback_first):
        menu = menu_button["menu"]
        menu.delete(0, "end")
        for name in names:
            menu.add_command(label=name, command=lambda v=name, s=var: self._ns_pick(s, v))
        if preferred in names:
            var.set(preferred)
        elif fallback_first and names:
            var.set(names[0])
        else:
            var.set("(escolha)")

    def _ns_refresh_devices(self):
        try:
            ins, outs = NoiseSuppressor.devices()
            default_in = NoiseSuppressor.default_input_name()
        except Exception as exc:
            self._say(f"Não consegui listar os dispositivos de áudio: {exc}")
            return
        in_names = [n for _, n in ins]
        out_names = [n for _, n in outs]
        virtual = NoiseSuppressor.guess_virtual(outs)
        self._fill_menu(self.ns_in_menu, self.ns_in, in_names,
                        self.cfg["ns_in"] or default_in, True)
        self._fill_menu(self.ns_out_menu, self.ns_out, out_names,
                        self.cfg["ns_out"] or virtual, False)
        if virtual:
            self.ns_help.configure(
                text="Microfone virtual encontrado. Nos aplicativos (Meet, Discord, Zoom), "
                     "escolha \"CABLE Output\" como microfone. Use fones para evitar eco.")
        else:
            self.ns_help.configure(
                text="Para os outros programas ouvirem o áudio limpo, instale o VB-Cable "
                     "(vb-audio.com/Cable), reabra o Mutinho, escolha \"CABLE Input\" acima e, "
                     "nos aplicativos, use \"CABLE Output\" como microfone.")

    def _ns_pick(self, var, value):
        var.set(value)
        self.cfg["ns_in"] = self.ns_in.get()
        self.cfg["ns_out"] = self.ns_out.get()
        if self.var_ns.get():
            self._ns_toggle()  # reinicia com os dispositivos novos

    def _ns_toggle(self):
        if self.var_ns.get():
            err = self.ns.start(self.ns_in.get(), self.ns_out.get())
            if err:
                self.var_ns.set(False)
                self.ns_status.configure(text=err, fg=self.MUTE)
                return
            self.cfg["ns_enabled"] = True
            self.ns_status.configure(text="Ativo: seu áudio está sendo limpo.", fg=self.LIVE)
        else:
            self.ns.stop()
            self.cfg["ns_enabled"] = False
            self.ns_status.configure(text="", fg=self.DIM)

    def _ns_intensity(self, value):
        v = int(float(value))
        self.ns.set_intensity(v)
        nivel = "desligado" if v == 0 else "leve" if v < 35 else "médio" if v < 70 else "forte"
        self.ns_label.configure(text=f"Intensidade: {v}%  ({nivel})")

    def _ns_save_intensity(self):
        self.cfg["ns_intensity"] = int(self.ns_scale.get())

    def _ns_meter(self):
        import math

        S = self.S
        canvas = self.ns_meter
        canvas.delete("all")
        width = S(290)
        rows = (("Entrada", self.ns.in_level, "#6b7280"), ("Saída", self.ns.out_level, self.LIVE))
        for i, (label, level, color) in enumerate(rows):
            y = S(4) + i * S(16)
            frac = 0.0
            if self.ns.running:
                frac = min(1.0, max(0.0, (20 * math.log10(max(level, 1e-5)) + 60) / 60))
            canvas.create_text(0, y + S(5), text=label, anchor="w", fill=self.DIM, font=self.fonts["h8"])
            canvas.create_rectangle(S(52), y, width, y + S(10), fill=self.BG, outline="")
            canvas.create_rectangle(S(52), y, S(52) + int((width - S(52)) * frac), y + S(10),
                                    fill=color, outline="")
        self.root.after(100, self._ns_meter)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        """Botão X: esconde na bandeja (se houver) em vez de encerrar."""
        if self.fullscreen:
            self._exit_fullscreen()
        if self.tray is not None:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        if hasattr(self, "ns"):
            self.ns.stop()
        if self.tray is not None:
            self.tray.stop()
        self.hotkeys.stop()
        self.ptt.stop()
        self.server.stop()
        self.ctrl.shutdown()
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
    ctrl.apply_startup()

    server = RemoteServer(ctrl)
    server.start()

    hotkeys = HotkeyManager(ctrl.safe_toggle)
    ptt = PushToTalk(ctrl.talk_async)
    hotkey_error = None
    try:
        activate_hotkeys(cfg, hotkeys, ptt)
    except Exception as exc:
        hotkey_error = str(exc)

    if "--no-gui" in args:
        import qrcode

        is_ptt = cfg["mode"] == "ptt"
        print(f"{APP_NAME} rodando.")
        print(f"Modo: {'apertar para falar' if is_ptt else 'alternar'}")
        print(f"Atalho: {pretty_combo(cfg['ptt_hotkey' if is_ptt else 'hotkey'])}"
              + (f"  (indisponível: {hotkey_error})" if hotkey_error else ""))
        print(f"Link do celular: {server.url()}\n")
        qr = qrcode.QRCode(border=1)
        qr.add_data(server.url())
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        ctrl.shutdown()
        return

    gui = GUI(ctrl, server, hotkeys, ptt, start_hidden="--hidden" in args,
              start_fullscreen="--fullscreen" in args)
    if hotkey_error:
        gui._say(f"Atalho global indisponível: {hotkey_error}")
    gui.run()


if __name__ == "__main__":
    main()
