#!/usr/bin/env python3
"""Compila o instalador do Mutinho (siteMutinho/dist/MutinhoSetup.exe) com o Inno Setup.

Antes:
  1. Gere o Mutinho.exe (PyInstaller, comando no LEIA-ME).
  2. Baixe o pacote do VB-CABLE em vb-audio.com/Cable, extraia o .zip e copie TODO o conteúdo
     para a pasta  vbcable\\  ao lado deste arquivo (deve existir vbcable\\VBCABLE_Setup_x64.exe).
  3. Instale o Inno Setup (jrsoftware.org/isinfo.php) - só você precisa dele, os usuários não.
Depois:
  python make_installer.py
"""
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent

source = (root / "mutinho.py").read_text(encoding="utf-8")
match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', source, re.M)
if not match:
    sys.exit("Não achei APP_VERSION no mutinho.py")
version = match.group(1)


def _from_registry():
    """Procura a pasta de instalação do Inno Setup na lista de programas instalados do Windows."""
    try:
        import winreg
    except ImportError:
        return None
    path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        name = winreg.EnumKey(key, i)
                        if not name.startswith("Inno Setup"):
                            continue
                        with winreg.OpenKey(key, name) as sub:
                            try:
                                location = winreg.QueryValueEx(sub, "InstallLocation")[0]
                            except OSError:
                                continue
                        candidate = os.path.join(location, "ISCC.exe")
                        if os.path.exists(candidate):
                            return candidate
            except OSError:
                continue
    return None


def find_iscc(explicit=None):
    # 1) caminho informado: --iscc "C:\...\ISCC.exe" ou variável de ambiente ISCC_PATH
    explicit = explicit or os.environ.get("ISCC_PATH")
    if explicit:
        explicit = explicit.strip('"')
        if os.path.isdir(explicit):
            explicit = os.path.join(explicit, "ISCC.exe")
        return explicit if os.path.exists(explicit) else None
    # 2) PATH, pastas comuns e registro do Windows
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    bases = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
             os.environ.get("ProgramW6432"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
    for base in filter(None, bases):
        hits = sorted(glob.glob(os.path.join(base, "Inno Setup*", "ISCC.exe")), reverse=True)
        if hits:
            return hits[0]
    return _from_registry()


def iscc_argument():
    if "--iscc" in sys.argv:
        i = sys.argv.index("--iscc")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


problems = []
exe = root / "siteMutinho" / "dist" / "Mutinho.exe"
vb = root / "vbcable" / "VBCABLE_Setup_x64.exe"
if not exe.exists():
    problems.append(f"Falta {exe}\n    -> gere o .exe com o PyInstaller (comando no LEIA-ME).")
if not vb.exists():
    problems.append(f"Falta {vb}\n    -> baixe o pacote em vb-audio.com/Cable e extraia TODO o conteúdo do .zip em {root / 'vbcable'}")
given = iscc_argument() or os.environ.get("ISCC_PATH")
iscc = find_iscc(given)
if not iscc:
    if given:
        problems.append(f"Não existe o arquivo informado: {given}")
    else:
        problems.append(
            "Não achei o Inno Setup (ISCC.exe)\n"
            "    -> se já instalou, ache o arquivo e informe o caminho:\n"
            '       python make_installer.py --iscc "C:\\caminho\\ISCC.exe"\n'
            "    -> se não instalou: jrsoftware.org/isinfo.php")
if problems:
    print("Não foi possível compilar o instalador:\n")
    for p in problems:
        print(" *", p)
    sys.exit(1)

print(f"Compilando o instalador da versão {version} ...")
result = subprocess.run([iscc, f"/DMyAppVersion={version}", str(root / "installer.iss")])
if result.returncode != 0:
    sys.exit("O Inno Setup falhou (veja a mensagem acima).")

out = root / "siteMutinho" / "dist" / "MutinhoSetup.exe"
print(f"\nPronto: {out} ({out.stat().st_size / 1e6:.1f} MB)")
print("Faça commit e push de MutinhoSetup.exe, Mutinho.exe e version.json juntos.")
