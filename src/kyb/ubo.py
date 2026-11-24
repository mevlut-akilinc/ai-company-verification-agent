"""Gerçek faydalanıcı (UBO) hesabı: sahiplik grafı üzerinde saf fonksiyonlar.

LLM yok. Girdi: konu şirketten yukarı doğru aktif CONTROLS kenarları (store'dan).

Yöntem
------
* Kenar payı = pay ve oy aralıklarının büyüğü (KYB'de eşik "pay VEYA oy").
  PSC kesin yüzde vermez, bant verir (ör. >25-50): her değer [min, max] aralığıdır.
* Etkin pay (çarpımsal): yol boyunca aralıklar çarpılır, aynı kişiye giden yollar
  toplanır (max 100'de kesilir).
* Kontrol zinciri: kişiden konu şirketin bir üst halkasına kadar her ara kenar
  çoğunluk (>%50) ise kişi o halkanın konu şirketteki payının TAMAMINI kontrol eder.
  Çarpımsal hesapta %25 altında kalan ama fiilen kontrol eden kişiyi yakalar.
* Yüzdesiz kenar (sadece yönetici atama / önemli etki) yolu "kontrol yoluyla" yapar;
  yüzde hesabına girmez ama kişi listeden atılmaz.

Sınıflar (eşik: UBO_THRESHOLD_PCT, varsayılan 25):
  kesin      : etkin min > eşik ya da kontrol zinciriyle min > eşik
  olasi      : aralık eşiği kapsıyor (max > eşik >= min); bant belirsizliği
  kontrol    : yüzdesiz kontrol hakkı (yönetici atama / önemli etki)

Müşterek tutma: bir şirkete gelen payların alt sınırları toplamı %100'ü aşıyorsa
aynı pay birden fazla kişi adına kayıtlıdır (ör. müşterek hak sahipleri, tröst
yöneticileri; PSC kuralı her birini payın tamamını tutuyor sayar). Kişiler yine
faydalanıcıdır ama yüzdeleri TOPLANAMAZ; rapor bunu açıkça işaretler.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

MAX_DERINLIK = 20
MAX_YOL = 5000          # kişiye/uca varan yol sayısı sınırı
MAX_GENISLETME = 50_000  # ara şirket genişletme sınırı (elmas yapılarda yol patlaması)


@dataclass(frozen=True)
class Kenar:
    owner_label: str          # Person | Company | LegalEntity | ProtectedPSC
    owner_key: str
    owner: dict               # düğüm özellikleri (name, nationality, ...)
    asset: str                # company_number
    lo: float | None          # kenar payı alt sınırı (%)
    hi: float | None
    props: dict = field(default_factory=dict, compare=False, hash=False)


def kenar_payi(r: dict) -> tuple[float | None, float | None]:
    """Pay ve oy aralıklarının elemanca büyüğü; ikisi de yoksa (None, None)."""
    los = [v for v in (r.get("share_min"), r.get("vote_min")) if v is not None]
    his = [v for v in (r.get("share_max"), r.get("vote_max")) if v is not None]
    return (max(los) if los else None, max(his) if his else None)


def kisi_anahtari(owner: dict, key: str) -> str:
    """Aynı kişinin farklı şirketlerdeki PSC kayıtlarını birleştirir.

    PSC kayıtları tekilleştirilmemiş (her şirkette ayrı psc_key). Ad + doğum ay/yılı
    birlikte güçlü bir anahtar; doğum bilgisi yoksa kayıt kendi başına kalır.
    """
    by, bm = owner.get("birth_year"), owner.get("birth_month")
    if by and bm:
        return f"{(owner.get('name') or '').strip().upper()}|{by}-{bm:02d}"
    return f"psc:{key}"


def _dugum(label: str, key: str) -> str:
    return f"{label}:{key}"


def hesapla(konu: str, kenarlar: list[Kenar], esik: float,
            cozumlenen: dict[str, str] | None = None) -> dict:
    """konu: company_number. cozumlenen: LegalEntity key -> company_number (ER çıktısı);
    bu tüzel sahipler grafta o şirket gibi yürünür."""
    cozumlenen = cozumlenen or {}
    sahipler: dict[str, list[Kenar]] = defaultdict(list)  # asset -> gelen kenarlar
    for k in kenarlar:
        sahipler[k.asset].append(k)
    musterek = {a for a, ks in sahipler.items()
                if sum(k.lo for k in ks if k.lo is not None) > 100 + 1e-9}

    kisiler: dict[str, dict] = {}
    dongu = False
    kesildi = False
    en_derin = 0
    yabanci: dict[str, dict] = {}
    korunan: set[str] = set()
    yol_sayisi = 0
    genisletme = 0

    # DFS: yığın = (şirket, yol üzerindeki şirketler, yol kenarları)
    yigin: list[tuple[str, tuple[str, ...], tuple[Kenar, ...]]] = [(konu, (konu,), ())]
    while yigin:
        sirket, ziyaret, yol = yigin.pop()
        for k in sahipler.get(sirket, []):
            yeni_yol = (k,) + yol  # sahipten konuya doğru sıralı
            hedef_sirket = None
            if k.owner_label == "Company":
                hedef_sirket = k.owner_key
            elif k.owner_label == "LegalEntity" and k.owner_key in cozumlenen:
                hedef_sirket = cozumlenen[k.owner_key]

            if hedef_sirket is not None:
                if hedef_sirket in ziyaret:
                    dongu = True  # döngüsel sahiplik: bu yol kapanır
                    continue
                genisletme += 1
                if (len(yeni_yol) >= MAX_DERINLIK or yol_sayisi >= MAX_YOL
                        or genisletme > MAX_GENISLETME):
                    kesildi = True
                    continue
                en_derin = max(en_derin, len(yeni_yol))
                yigin.append((hedef_sirket, ziyaret + (hedef_sirket,), yeni_yol))
                continue

            yol_sayisi += 1
            if yol_sayisi > MAX_YOL:
                kesildi = True
                break
            en_derin = max(en_derin, len(yeni_yol) - 1)
            if k.owner_label == "LegalEntity":
                yabanci.setdefault(k.owner_key, {"key": k.owner_key, **_ozet(k.owner)})
            elif k.owner_label == "ProtectedPSC":
                korunan.add(k.owner_key)
            elif k.owner_label == "Person":
                _kisiye_ekle(kisiler, k, yeni_yol, musterek)

    faydalanicilar = []
    for kisi in kisiler.values():
        kisi["effective_min"] = min(100.0, kisi["effective_min"])
        kisi["effective_max"] = min(100.0, kisi["effective_max"])
        kisi["control_min"] = min(100.0, kisi["control_min"])
        kisi["control_max"] = min(100.0, kisi["control_max"])
        kisi["sinif"] = _sinifla(kisi, esik)
        if kisi["sinif"]:
            faydalanicilar.append(kisi)
    sira = {"kesin": 0, "olasi": 1, "kontrol": 2}
    faydalanicilar.sort(key=lambda b: (sira[b["sinif"]], -b["effective_max"], b["name"]))

    # Zincirin tepesindeki şirketler: sahibi olan ama kendi sahibi grafta olmayan şirketler.
    # Sık neden: borsada işlem gören şirket (PSC beyanından muaf) ya da PSC'si bildirilmemiş.
    tepe = {}
    for k in kenarlar:
        if k.owner_label == "Company" and k.owner_key not in sahipler:
            tepe[k.owner_key] = {"company_number": k.owner_key, "name": k.owner.get("name"),
                                 "psc_statements": k.owner.get("psc_statements") or []}

    return {
        "threshold_pct": esik,
        "top_companies": sorted(tepe.values(), key=lambda x: x["company_number"]),
        "joint_holding_companies": sorted(musterek),
        "layers": en_derin,
        "has_cycle": dongu,
        "truncated": kesildi,
        "path_count": yol_sayisi,
        "unresolved_foreign_entities": len(yabanci),
        "foreign_entities": sorted(yabanci.values(), key=lambda x: x["name"] or ""),
        "protected_pscs": len(korunan),
        "persons_seen": len(kisiler),
        "beneficial_owners": faydalanicilar,
    }


def _ozet(owner: dict) -> dict:
    return {k: owner.get(k) for k in ("name", "country_registered", "kind") if owner.get(k)}


def _kisiye_ekle(kisiler: dict, k: Kenar, yol: tuple[Kenar, ...], musterek: set[str]) -> None:
    anahtar = kisi_anahtari(k.owner, k.owner_key)
    kisi = kisiler.setdefault(anahtar, {
        "name": k.owner.get("name"), "nationality": k.owner.get("nationality"),
        "birth": (f"{k.owner['birth_year']}-{k.owner['birth_month']:02d}"
                  if k.owner.get("birth_year") and k.owner.get("birth_month") else None),
        "psc_keys": [], "effective_min": 0.0, "effective_max": 0.0,
        "control_min": 0.0, "control_max": 0.0,
        "path_count": 0, "control_only_paths": 0, "paths": [], "joint": False,
    })
    if any(e.asset in musterek for e in yol):
        kisi["joint"] = True
    if k.owner_key not in kisi["psc_keys"]:
        kisi["psc_keys"].append(k.owner_key)
    kisi["path_count"] += 1
    if len(kisi["paths"]) < 5:
        kisi["paths"].append([{"asset": e.asset, "lo": e.lo, "hi": e.hi} for e in yol])

    if any(e.lo is None for e in yol):
        kisi["control_only_paths"] += 1
        return
    lo = hi = 1.0
    for e in yol:
        lo *= e.lo / 100
        hi *= e.hi / 100
    kisi["effective_min"] += lo * 100
    kisi["effective_max"] += hi * 100
    # Kontrol zinciri: son kenar hariç hepsi çoğunluk (bantlar "more than X": lo >= 50 => >50)
    if all(e.lo >= 50 for e in yol[:-1]):
        kisi["control_min"] += yol[-1].lo
        kisi["control_max"] += yol[-1].hi


def _sinifla(kisi: dict, esik: float) -> str | None:
    lo = max(kisi["effective_min"], kisi["control_min"])
    hi = max(kisi["effective_max"], kisi["control_max"])
    # PSC bantları "more than X": alt sınır eşiğe eşitse de eşik aşılmıştır (>25).
    if lo >= esik and hi > esik:
        return "kesin"
    if hi > esik:
        return "olasi"
    if kisi["control_only_paths"]:
        return "kontrol"
    return None


# --------------------------------------------------------------------------- graftan çekme
def zinciri_cek(store, konu: str, cozumlenen: dict[str, str] | None = None) -> list[Kenar]:
    """Konu şirketten yukarı, katman katman (BFS) aktif kontrol kenarlarını çeker.

    Her katmanda tek sorgu; değişken uzunluklu yol sorgusu yerine bu yol seçildi,
    çünkü elmas yapılı gruplarda yol sayısı üstel büyür, kenar sayısı büyümez.
    """
    cozumlenen = cozumlenen or {}
    kenarlar: list[Kenar] = []
    goruldu = {konu}
    sinir = [konu]
    for _ in range(MAX_DERINLIK):
        if not sinir:
            break
        sonraki: list[str] = []
        for r in store.controllers(sinir):
            label = r["label"] if r["label"] != "Unknown" else "ProtectedPSC"
            owner = r["owner"]
            key = owner.get({"Person": "psc_key", "Company": "company_number",
                             "LegalEntity": "entity_key"}.get(label, "psc_key"))
            lo, hi = kenar_payi(r["edge"])
            kenarlar.append(Kenar(label, key, owner, r["asset"], lo, hi, r["edge"]))
            ust = key if label == "Company" else cozumlenen.get(key) if label == "LegalEntity" else None
            if ust and ust not in goruldu:
                goruldu.add(ust)
                sonraki.append(ust)
        sinir = sonraki
    return kenarlar
