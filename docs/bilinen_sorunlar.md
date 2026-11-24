# Belge Agent'ı — v0 Durumu ve Bilinen Sorunlar

**Durum:** v0 olarak donduruldu (2026-11-24). Uçtan uca akış kurulduktan sonra
en zayıf halka hangisiyse o iyileştirilecek.

## v0 Ölçümü

- Model: qwen3:4b-instruct (Ollama, yerel), seed 42, tekrarlanabilir
- Test seti: 6 sentetik TSG ilanı (`tests/fixtures/`), elle yazılmış gold
- Alan doğruluğu: 62/72 (%86)
- Şirket uyarısı eşleşmesi: 6/6, şema hatası: 0
- Grounding: gerçek hataların 10/10'unu yakalıyor (recall); ancak 27 `kaldi`
  işaretinin yaklaşık 15'i yanlış alarm (precision ≈ %40)
- Süre: belge başına 4–30 sn, toplam ~127 sn (RTX 3050 4 GB)

## Bilinen Sorunlar

### S1 — Model belgede yazmayan yüzdeleri hesaplıyor

- **Belirti:** 01, 04, 05, 06'da `pay_orani` alanına tutardan hesaplanmış
  değerler yazılıyor (ör. 35, 65). 9 yanlış alan.
- **Kök neden:** Anahtar zorunlu olunca model alanı doldurmaya eğilimli;
  prompt'taki "hesaplama yapma" kuralı 4B modelde yetmiyor.
- **Not:** `oranlar.py` bunu yakalayamaz; uydurma kendi içinde tutarlı olduğu
  için ORAN_TUTARSIZ çıkmıyor. Sadece grounding yakalıyor.
- **Çözüm fikri:** `pay_orani` alanını şemadan çıkar; yüzdeyi kod, tutar
  kanıtından `%` regex'i ile çıkarsın.

### S2 — Tek kanıt varsayımı (yanlış alarm kaynağı)

- **Belirti:** Doğru `pay_tutari` değerleri `kaldi` alıyor (~15 yanlış alarm).
- **Kök neden:** Şema ortak başına tek kanıt istiyor; isim kuruluş maddesinde,
  tutar sermaye maddesinde. Ollama anahtarları alfabetik ürettiği için `kanit`
  pay alanlarından önce yazılıyor; model ismin ilk geçtiği cümleyi seçiyor.
- **Çözüm fikri:** Kanıtı `ad_kaniti` ve `tutar_kaniti` olarak böl. `tutar_kaniti`
  alfabede `pay_tutari`'dan sonra geldiği için model tutarı önce yazar.
  Alfabetik sıra belgelenmiş bir davranış değil; testle sabitlenmeli.

### S3 — Referansla bağlanan ortaklar (belge 06)

- **Belirti:** "Kurucu (1): … 400.000.-TL" cümlesinde isim yok.
- **Kök neden:** Ortak–tutar bağı "Kurucu (1)" referansıyla kuruluyor;
  grounding bu bağı doğrulayamaz. Kanıt bölünse bile çapraz atama yakalanmaz.
- **Çözüm fikri:** Tutar kanıtında ortağın adı yoksa sonuç en fazla `kismi`
  sayılsın. Kalıcı çözüm Entity Resolution aşamasında.

### S4 — Sicil numarası karışıklığı

- **Belirti:** 03'te MERSİS numarası `sicil_no` alanına yazıldı (grounding yakaladı).
- **Çözüm fikri:** Description'a "MERSİS veya oda sicil numarası değildir" eklendi;
  tekrar ölçülecek.

### S5 — Kanıt bağımlılığı (grounding)

- Kanıt belgede yoksa, kanıtın içinde aranan ad ve pay kontrolleri yanıltıcı
  biçimde `gecti` dönebilir. Durum: [DÜZELTİLDİ / AÇIK — kontrol et]

## Bilinçli Sınırlar

- Tutarlar tamsayı TL. Kuruşlu sermaye nadir; görülürse grounding işaretler.
- Yazıyla yazılmış tutarlar ("Bir Milyon Türk Lirası") okunmuyor.
- İngilizce ondalık biçim (`40.0`) sayı sayılmıyor; belgeler Türkçe.
- A.Ş.'lerde pay devirleri TSG'de ilan edilmez; ilan sadece kurucuları gösterir.
  Güncel ortaklık için pay defteri gerekir.
- 4096 bağlam sınırı: şema + talimat ~750 token; belgeye ~2900 token kalıyor.
  Uzun belgeler Adım 4'te ele alınacak.
