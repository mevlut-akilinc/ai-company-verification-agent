"""Belge çıkarım şeması.

Ticaret sicil gazetesi gibi şirket belgelerinden LLM'in çıkaracağı bilgilerin
şekli. Bu şema iki iş görür:
  1. model_json_schema() ile Ollama'nın `format` parametresine verilir,
     modelin üretimi bu şekle zorlanır.
  2. Gelen JSON, model_validate_json() ile doğrulanır.

Uydurmaya karşı kurallar:
  - Belgede her zaman bulunmayan alanların DEĞERİ null olabilir (None = "belgede
    yok"), ama ANAHTARI zorunludur (default yok). Ollama'nın grammar'ı opsiyonel
    anahtarları atlayıp nesneyi kapatmaya izin veriyor; ilk ölçümde model
    sermaye_tutari ve pay_orani'yı hiçbir belgede yazmadı.
  - Her ortak için `kanit`: belgeden birebir kopyalanmış metin. Sonradan
    kodla belgede geçip geçmediği kontrol edilir.

Para alanları tamsayı TL (int):
  - JSON şemasında integer ondalık nokta ve üs kabul etmez. float'ta model
    rakam döngüsünü üsse kaçırıyordu (50000.0000e-2000000 -> 0.0).
  - Toplamlar kesin; float hassasiyet sorunu ve Decimal ihtiyacı yok.
  - Bilinçli sınır: kuruşlu tutar (12.500,50) tamsayıya sığmaz; model 12500
    yazar, grounding belgedeki 12500,5 ile eşleştiremez ve işaretler.
  - Üst sınır: int sınırsızdır; rakam döngüsü (5000000000...) gt=0'dan geçerdi.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 10 trilyon TL: gerçek bir şirket sermayesinin çok üstünde, rakam döngüsünü keser.
TUTAR_UST_SINIRI = 10**13


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
    pay_tutari: int | None = Field(
        gt=0,
        lt=TUTAR_UST_SINIRI,
        description="Ortağın sermaye payı, TL, belgede yazdığı gibi. Yoksa null.",
    )
    # gt=0: float döngüsü (33.33e-2000 -> 0.0) ge=0'dan sessizce geçerdi;
    # sıfır paylı ortak da olmaz.
    pay_orani: float | None = Field(
        gt=0,
        le=100,
        description="Yüzde pay oranı, sadece belgede açıkça yazıyorsa. Hesaplama yapma. Yoksa null.",
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
        pattern=r"^[0-9]{1,15}$",
        description="Ticaret sicil numarası, sadece rakamlar. Belgede yoksa null.",
    )
    sermaye_tutari: int | None = Field(
        gt=0,
        lt=TUTAR_UST_SINIRI,
        description="Şirketin toplam sermayesi, TL. Belgede yoksa null.",
    )
    # Varsayılan yok: model "ortak yok" diyecekse bunu [] yazarak açıkça söylemeli,
    # anahtarı atlayamaz.
    ortaklar: list[Ortak] = Field(
        description="Belgede adı geçen ortaklar. Hiç yoksa boş liste.",
    )


if __name__ == "__main__":
    # Modelin göreceği JSON şemasını yazdır.
    print(json.dumps(SirketBilgisi.model_json_schema(), indent=2, ensure_ascii=False))
