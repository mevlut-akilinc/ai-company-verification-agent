"""İlk çıkarım denemesi: sentetik bir ticaret sicili ilanı üzerinde."""

import sys

from pydantic import ValidationError

from kyb.extractor import ExtractionError, extract_company
from kyb.grounding import ground

# Sentetik belge: kişiler ve şirket hayalidir.
BELGE = """TÜRKİYE TİCARET SİCİLİ GAZETESİ
İstanbul Ticaret Sicili Müdürlüğü
Sicil No: 845123

MAVİ DENİZ LOJİSTİK ANONİM ŞİRKETİ

Şirketin 25.10.2026 tarihli genel kurul toplantısında alınan karar uyarınca
şirket sermayesi 10.000.000 TL olarak tespit edilmiştir.
Ortaklık yapısı aşağıdaki gibidir:
Kuzey Yatırım Holding A.Ş. sermayenin %60'ına sahiptir.
Elif Karaca sermayenin %40'ına sahiptir.
Yönetim kurulu başkanlığına Elif Karaca seçilmiştir.
"""


def main() -> int:
    try:
        sonuc, meta = extract_company(BELGE)
    except ExtractionError as e:
        print(f"Çıkarım hatası: {e}")
        raw = e.raw
        # !r -> repr(): boşluk, \n ve \t karakterlerini görünür yapar.
        print(f"\nHam çıktı uzunluğu: {len(raw)} karakter")
        print(f"Boşluk olmayan karakter sayısı: {len(raw.strip())}")
        print(f"\nİlk 600 karakter:\n{raw[:600]!r}")
        print(f"\nSon 300 karakter:\n{raw[-300:]!r}")
        return 1
    except ValidationError as e:
        print(f"Şema doğrulaması başarısız:\n{e}")
        return 1

    print(f"Token: {meta['prompt_tokens']} giriş, {meta['output_tokens']} çıkış, "
          f"bitiş: {meta['done_reason']}")
    print("\nDoğrulanmış nesne:")
    print(sonuc.model_dump_json(indent=2))

    rapor = ground(sonuc, BELGE)
    print("\nGrounding raporu:")
    for k in rapor.kontroller:
        not_ = f"  ({k.not_})" if k.not_ else ""
        print(f"  [{k.durum:7}] {k.alan}: {k.deger!r}{not_}")
    sonuc_metni = "DOĞRULANDI" if rapor.dogrulandi else "ANALİST İNCELEMESİ GEREKLİ"
    print(f"\nSonuç: {sonuc_metni} "
          f"({rapor.kalan_sayisi} kaldı, {rapor.kismi_sayisi} kısmi)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
