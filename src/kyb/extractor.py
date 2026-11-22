"""LLM ile belgeden şirket bilgisi çıkarımı (Ollama + yapılandırılmış çıktı)."""

from __future__ import annotations

import json
import os

import ollama

from kyb.extraction import SirketBilgisi

MODEL = os.environ.get("KYB_LLM_MODEL", "qwen3:4b-instruct")

SYSTEM_PROMPT = """Sen bir KYB (Know Your Business) belge analistisin.
Görevin: verilen şirket belgesinden bilgileri çıkarıp JSON olarak döndürmek.

Kurallar:
- Sadece belgede açıkça yazan bilgileri kullan. Tahmin etme, hesaplama yapma.
- Belgede olmayan bir bilgi için null yaz.
- Ad ve unvanları belgede yazdığı gibi aynen yaz.
- "kanit" alanına, bilginin geçtiği cümleyi belgeden kelimesi kelimesine kopyala.

Çıktı bu JSON şemasına uymalı:
{schema}"""


class ExtractionError(Exception):
    """Model çıktısı kullanılamaz durumda (kesilmiş ya da şemaya uymuyor).

    Teşhis için modelin ham çıktısını da taşır.
    """

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def extract_company(document_text: str, *, num_predict: int = 1024) -> tuple[SirketBilgisi, dict]:
    """Belge metninden SirketBilgisi çıkarır. (sonuç, ölçümler) döndürür."""
    schema = SirketBilgisi.model_json_schema()

    response = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system",
             "content": SYSTEM_PROMPT.format(schema=json.dumps(schema, ensure_ascii=False))},
            {"role": "user", "content": f"BELGE:\n{document_text}"},
        ],
        format=schema,
        options={
            # Qwen3-Instruct-2507 için önerilen örnekleme ayarları.
            # temperature=0 (greedy) bu modelde tekrar döngüsüne yol açıyordu.
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0,
            # Tekrarı cezalandırır; yüksek değer birebir kopyalamayı (kanit) bozabilir.
            "presence_penalty": 1.0,
            # Determinizmi sıcaklık yerine sabit seed sağlar.
            "seed": 42,
            "num_ctx": 4096,
            "num_predict": num_predict,
        },
    )

    meta = {
        "prompt_tokens": response.prompt_eval_count,
        "output_tokens": response.eval_count,
        "done_reason": response.done_reason,
        "raw": response.message.content,
    }

    # Çıktı sınıra takılıp kesildiyse JSON yarımdır, doğrulamaya hiç girmeyelim.
    if response.done_reason == "length":
        raise ExtractionError(f"Çıktı num_predict={num_predict} sınırında kesildi.",
                              raw=response.message.content)

    return SirketBilgisi.model_validate_json(response.message.content), meta
