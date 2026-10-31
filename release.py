#!/usr/bin/env python3
"""Gera siteMutinho/version.json a partir do mutinho.py e do .exe já compilado.

Uso (na pasta do projeto, depois de gerar o .exe):
    python release.py
    python release.py "O que mudou nesta versão"
"""
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
source = (root / "mutinho.py").read_text(encoding="utf-8")
match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', source, re.M)
if not match:
    sys.exit("Não achei APP_VERSION no mutinho.py")
version = match.group(1)

exe = root / "siteMutinho" / "dist" / "Mutinho.exe"
if not exe.exists():
    sys.exit(f"Não achei {exe}. Gere o .exe antes (PyInstaller).")

data = {
    "version": version,
    "url": "dist/Mutinho.exe",
    "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
    "notes": sys.argv[1] if len(sys.argv) > 1 else "",
}
out = root / "siteMutinho" / "version.json"
out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Versão {version} | {exe.stat().st_size / 1e6:.1f} MB")
print(f"Gravado: {out}")
print("Agora faça commit e push do .exe e do version.json juntos.")
