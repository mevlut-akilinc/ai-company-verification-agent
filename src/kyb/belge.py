"""Vaka belgelerinden düz metin okuma.

Belge sözlüğü iki biçimde gelebilir:
  {"text": "..."}             : metin doğrudan verilmiş (test, API)
  {"path": "belge.pdf"}       : dosyadan okunur (.pdf -> pypdf, diğerleri UTF-8 metin)
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


def pdf_metni(yol: str | Path) -> str:
    return "\n".join(sayfa.extract_text() or "" for sayfa in PdfReader(yol).pages)


def belge_adi(belge: dict) -> str:
    if belge.get("name"):
        return str(belge["name"])
    if belge.get("path"):
        return Path(belge["path"]).name
    return "metin"


def belge_metni(belge: dict) -> str:
    if belge.get("text") is not None:
        return belge["text"]
    yol = belge.get("path")
    if not yol:
        raise ValueError("Belgede 'text' ya da 'path' olmalı.")
    yol = Path(yol)
    if yol.suffix.lower() == ".pdf":
        return pdf_metni(yol)
    return yol.read_text(encoding="utf-8")
