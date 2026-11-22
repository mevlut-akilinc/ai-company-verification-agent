"""Belge çıkarım şeması.

Ticaret sicil gazetesi gibi şirket belgelerinden LLM'in çıkaracağı bilgilerin
şekli. Bu şema iki iş görür:
  1. model_json_schema() ile Ollama'nın `format` parametresine verilir,
     modelin üretimi bu şekle zorlanır.
  2. Gelen JSON, model_validate_json() ile doğrulanır.

Uydurmaya karşı kurallar:
  - Belgede her zaman bulunmayan alanlar opsiyoneldir (None = "belgede yok").
  - Her ortak için `kanit`: belgeden birebir kopyalanmış metin. Sonradan
    kodla belgede geçip geçmediği kontrol edilir.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Ortak(BaseModel):
    """Şirketin bir ortağı (gerçek ya da tüzel kişi)."""

    model_config = ConfigDict(extra="forbid")

    ad: str = Field(
        min_length=1,
        max_length=200,
        description="Ortağın belgede yazdığı haliyle tam adı veya unvanı.",
    )
    tur: Literal["gercek", "tuzel"] = Field(
        description="'gercek': kişi. 'tuzel': şirket, vakıf vb. kurum.",
    )
    pay_orani: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Yüzde olarak pay oranı (0-100). Belgede açıkça yazmıyorsa null.",
    )
    kanit: str = Field(
        min_length=1,
        max_length=400,
        description="Bu ortağın bilgisinin geçtiği cümle, belgeden birebir kopyalanmış haliyle.",
    )


class SirketBilgisi(BaseModel):
    """Bir şirket belgesinden çıkarılan bilgiler."""

    model_config = ConfigDict(extra="forbid")

    unvan: str = Field(
        min_length=1,
        max_length=300,
        description="Şirketin belgede yazan tam ticaret unvanı.",
    )
    # pattern: grammar'a da geçer; 15. rakamdan sonra sadece kapanış tırnağı
    # üretilebilir. Döngüyü kesmez ama sınırlar; asıl doğrulama 2.5'te.
    sicil_no: str | None = Field(
        default=None,
        pattern=r"^[0-9]{1,15}$",
        description="Ticaret sicil numarası, sadece rakamlar. Belgede yoksa null.",
    )
    # Varsayılan yok: model "ortak yok" diyecekse bunu [] yazarak açıkça söylemeli,
    # anahtarı atlayamaz.
    ortaklar: list[Ortak] = Field(
        description="Belgede adı geçen ortaklar. Hiç yoksa boş liste.",
    )


if __name__ == "__main__":
    # Modelin göreceği JSON şemasını yazdır.
    print(json.dumps(SirketBilgisi.model_json_schema(), indent=2, ensure_ascii=False))
