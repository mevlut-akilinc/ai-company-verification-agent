"""OpenSanctions için SQLite tarama indeksi (bir kerelik kurulur: kyb tarama-indeks).

480 MB'lık CSV'yi her vakada okumak yaklaşık bir dakika sürer. Bu modül dosyayı
bir kere okur, her kaydın adını ve alias'larını normalize edip yazar ve yanına
anahtar -> kayıt listesi ters indeksi kurar. Sorgular milisaniyeler sürer.

Tablolar
  kayit   (rid, os_id, tip, ad, liste, isimler JSON, dogum, ulkeler, programlar, datasets)
  anahtar (anahtar, df, kayitlar BLOB)   anahtar = "t:" kelime | "f:" fonetik kod
          kayitlar: rid'lerin uint32 dizisi (array('I')); df = dizi uzunluğu
  meta    (k, v)   kaynak dosya, boyut, değişim zamanı, sayaçlar

Aday daraltma: sorgudaki kelimelerin belge frekansına (df) bakılır. "MOHAMMED",
"HOLDINGS" gibi yaygın kelimeler on binlerce aday getirir; bu yüzden indekste var
olan en nadir NADIR_K kelime ve en nadir NADIR_K fonetik kod kullanılır, adaylar
birleşimle toplanır. Fonetik kodlar yazım ve harf çevirisi farklarını yakalar:
soyadı bozulmuşsa kelime indekste hiç yoktur ama fonetik kodu vardır.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from array import array
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

from kyb import tarama
from kyb.config import Paths
from kyb.data.readers import iter_csv

SURUM = 3  # normalizasyon, fonetik kod ya da liste türü değişince artır (indeks bayatlar)
NADIR_K = 2
# Bu kadar yaygın bir anahtar tek başına aday getirmez (daraltmaya yaramaz).
DF_TAVANI = 50_000
ADAY_TAVANI = 3_000

KISI_SEMALARI = {"Person"}
SIRKET_SEMALARI = {"Company", "Organization", "LegalEntity"}

# OpenSanctions simple CSV'de 'topics' yok; tür veri seti adından çıkarılır.
# 'sanctions' alanı yaptırım demek DEĞİL: yönetici yasakları (UK Companies House
# Disqualified Directors), ABD Medicaid/ihale yasakları, FINRA kararları, borsa men
# listeleri de bu alanı dolduruyor (305 bin "yaptırım"ın 234 bini bunlardı). Bunlar
# işlemi yasaklamaz: 'diger'. Yaptırım = yaptırım ya da terör listesinden gelen kayıt;
# 'sanctions' alanı yalnız veri seti bilgisi yoksa yedek olarak kullanılır.
_YAPTIRIM_IPUCLARI = ("SANCTION", "OFAC", "SDN", "OFSI", "FCDO", "CONSOLIDATED LIST",
                      "EMBARGO", "TERROR", "PROSCRIBED", "UN SECURITY COUNCIL", "ASSET FREEZE")
_PEP_IPUCLARI = ("PEP", "POLITICALLY EXPOSED", "POLITICIAN", "PARLIAMENT", "LEGISLAT",
                 "MINISTER", "DIPLOMAT", "PUBLIC OFFICIAL", "SENATE", "CONGRESS", "ASSEMBLY",
                 "MEMBERS OF", "DEPUTIES", "SABHA", "WORLD LEADERS", "HEADS OF MISSION",
                 "HOUSE OF LORDS", "HOUSE OF COMMONS", "ENTERPRISES LEADERSHIP",
                 "GOVERNMENT DIRECTORY", "WIKIDATA")


def varsayilan_yol() -> Path:
    return Paths().processed / "tarama_indeks.sqlite"


def _desen(ipuclari: tuple[str, ...]) -> re.Pattern:
    # Kelime başında eşleş: "PEP position annotations by OpenSanctions" yaptırım sayılmasın.
    return re.compile(r"\b(?:" + "|".join(map(re.escape, ipuclari)) + ")")


_YAPTIRIM_DESENI = _desen(_YAPTIRIM_IPUCLARI)
_PEP_DESENI = _desen(_PEP_IPUCLARI)


def liste_turu(sanctions: list[str], datasets: list[str]) -> str:
    adlar = [d.upper() for d in datasets]
    if any(_YAPTIRIM_DESENI.search(d) for d in adlar) or (sanctions and not adlar):
        return "yaptirim"
    if any(_PEP_DESENI.search(d) for d in adlar):
        return "pep"
    return "diger"


def _bol(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(";") if x.strip()]


def kaynak_dosyalar(paths: Paths | None = None) -> list[Path]:
    raw = (paths or Paths()).raw / "opensanctions"
    return sorted(raw.glob("*.csv"))


def _satirlar(dosyalar: Iterable[Path]) -> Iterable[dict]:
    for row in iter_csv(dosyalar):
        sema = row.get("schema")
        if sema in KISI_SEMALARI:
            tip = "kisi"
        elif sema in SIRKET_SEMALARI:
            tip = "sirket"
        else:  # Vessel, CryptoWallet, Security, Airplane ...
            continue
        if not row.get("name"):
            continue
        yield {"id": row["id"], "tip": tip, "ad": row["name"],
               "aliases": _bol(row.get("aliases")), "dogum": _bol(row.get("birth_date")),
               "ulkeler": _bol(row.get("countries")), "sanctions": _bol(row.get("sanctions")),
               "datasets": _bol(row.get("dataset"))}


def _anahtarlar(isimler: list[list[str]]) -> set[str]:
    a = set()
    for kelimeler in isimler:
        for k in kelimeler:
            a.add("t:" + k)
            if len(k) >= 3:
                a.add("f:" + tarama.fonetik(k))
    return a


def kur(dosyalar: list[Path], hedef: Path | None = None, *, ilerleme: bool = True) -> dict:
    """İndeksi sıfırdan kurar; geçici dosyaya yazıp sonunda yerine taşır."""
    from tqdm import tqdm

    hedef = Path(hedef or varsayilan_yol())
    hedef.parent.mkdir(parents=True, exist_ok=True)
    gecici = hedef.with_suffix(".tmp")
    gecici.unlink(missing_ok=True)
    t0 = time.time()

    db = sqlite3.connect(gecici)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE kayit (rid INTEGER PRIMARY KEY, os_id TEXT, tip TEXT, ad TEXT, liste TEXT,
                            isimler TEXT, dogum TEXT, ulkeler TEXT, programlar TEXT,
                            datasets TEXT);
        CREATE TABLE anahtar (anahtar TEXT PRIMARY KEY, df INTEGER, kayitlar BLOB) WITHOUT ROWID;
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    ters: dict[str, array] = defaultdict(lambda: array("I"))
    sayac: dict[str, int] = defaultdict(int)
    tampon = []
    rid = 0
    satirlar = _satirlar(dosyalar)
    if ilerleme:
        satirlar = tqdm(satirlar, desc="tarama-indeks", unit=" kayıt")
    for r in satirlar:
        isimler, gorulen = [], set()
        for ad in [r["ad"], *r["aliases"]]:
            t = tarama.tokens(ad, r["tip"])
            k = tarama.anahtar(t)
            if t and k not in gorulen:
                gorulen.add(k)
                isimler.append(t)
        if not isimler:  # yalnız Latin olmayan adlar
            sayac["latin_ad_yok"] += 1
            continue
        rid += 1
        liste = liste_turu(r["sanctions"], r["datasets"])
        sayac[f"{r['tip']}_{liste}"] += 1
        tampon.append((rid, r["id"], r["tip"], r["ad"], liste, json.dumps(isimler),
                       ";".join(r["dogum"]), ";".join(r["ulkeler"]),
                       json.dumps(r["sanctions"][:10], ensure_ascii=False),
                       ";".join(r["datasets"])))
        # Kişi ve şirket aynı kelimeyi paylaşabilir; tip ön eki karıştırmayı önler.
        for a in _anahtarlar(isimler):
            ters[f"{r['tip'][0]}{a}"].append(rid)
        if len(tampon) >= 20_000:
            db.executemany("INSERT INTO kayit VALUES (?,?,?,?,?,?,?,?,?,?)", tampon)
            tampon.clear()
    db.executemany("INSERT INTO kayit VALUES (?,?,?,?,?,?,?,?,?,?)", tampon)
    db.executemany("INSERT INTO anahtar VALUES (?,?,?)",
                   ((a, len(v), v.tobytes()) for a, v in ters.items()))
    meta = {
        "surum": SURUM,
        "kaynak": [str(d) for d in dosyalar],
        "kaynak_imza": [_imza(d) for d in dosyalar],
        "kurulum": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "saniye": round(time.time() - t0, 1),
        "kayit": rid,
        "anahtar": len(ters),
        "sayac": dict(sayac),
    }
    db.executemany("INSERT INTO meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in meta.items()])
    db.commit()
    db.close()
    gecici.replace(hedef)
    return meta


def _imza(d: Path) -> str:
    st = Path(d).stat()
    return f"{Path(d).name}:{st.st_size}:{int(st.st_mtime)}"


class IndeksYok(RuntimeError):
    pass


class TaramaIndeksi:
    def __init__(self, yol: Path | None = None):
        self.yol = Path(yol or varsayilan_yol())
        if not self.yol.exists():
            raise IndeksYok(f"Tarama indeksi yok: {self.yol}. Önce 'kyb tarama-indeks' çalıştırın.")
        self.db = sqlite3.connect(f"file:{self.yol.as_posix()}?mode=ro", uri=True,
                                  check_same_thread=False)
        self.meta = {k: json.loads(v) for k, v in self.db.execute("SELECT k, v FROM meta")}

    def bayat_mi(self) -> bool:
        """Kaynak dosya indeks kurulduktan sonra değiştiyse ya da indeks eski kodla
        kurulduysa (fonetik anahtarlar artık uyuşmaz) True."""
        if self.meta.get("surum") != SURUM:
            return True
        try:
            return [_imza(Path(d)) for d in self.meta["kaynak"]] != self.meta["kaynak_imza"]
        except OSError:
            return False  # kaynak silinmiş olabilir; indeks yine kullanılabilir

    def _df(self, anahtarlar: list[str]) -> dict[str, int]:
        if not anahtarlar:
            return {}
        soru = ",".join("?" * len(anahtarlar))
        return dict(self.db.execute(
            f"SELECT anahtar, df FROM anahtar WHERE anahtar IN ({soru})", anahtarlar))

    def aday_idleri(self, q_tokens: list[str], tip: str) -> list[int]:
        on = tip[0]
        kelime = [f"{on}t:{k}" for k in dict.fromkeys(q_tokens)]
        fon = [f"{on}f:{tarama.fonetik(k)}" for k in dict.fromkeys(q_tokens) if len(k) >= 3]
        df = self._df(kelime + fon)
        secilen = []
        for grup in (kelime, fon):
            var = sorted((a for a in dict.fromkeys(grup) if a in df), key=lambda a: df[a])
            nadir = [a for a in var if df[a] <= DF_TAVANI][:NADIR_K]
            secilen += nadir or var[:1]
        if not secilen:
            return []
        isabet: Counter[int] = Counter()
        for (blob,) in self.db.execute(
                f"SELECT kayitlar FROM anahtar WHERE anahtar IN ({','.join('?' * len(secilen))})",
                secilen):
            dizi = array("I")
            dizi.frombytes(blob)
            isabet.update(dizi)
        if len(isabet) <= ADAY_TAVANI:
            return list(isabet)
        # Çok aday: daha çok anahtarı paylaşanlar önce (JOHN SMITH'te SMITH tek başına yetmez).
        return [rid for rid, _ in isabet.most_common(ADAY_TAVANI)]

    def kayitlar(self, idler: list[int]) -> list[tarama.Kayit]:
        out = []
        for i in range(0, len(idler), 900):
            parca = idler[i:i + 900]
            for os_id, tip, ad, liste, isimler, dogum, ulkeler, programlar, datasets in self.db.execute(
                    "SELECT os_id, tip, ad, liste, isimler, dogum, ulkeler, programlar, datasets "
                    f"FROM kayit WHERE rid IN ({','.join('?' * len(parca))})", parca):
                out.append(tarama.Kayit(
                    id=os_id, tip=tip, ad=ad, liste=liste, isimler=json.loads(isimler),
                    dogum=_bol(dogum), ulkeler=_bol(ulkeler), programlar=json.loads(programlar),
                    datasets=_bol(datasets)))
        return out

    def kayit(self, os_id: str) -> tarama.Kayit | None:
        row = self.db.execute("SELECT rid FROM kayit WHERE os_id = ?", (os_id,)).fetchone()
        return self.kayitlar([row[0]])[0] if row else None

    def tara(self, sorgu: tarama.Sorgu, esik: float = tarama.ESIK) -> dict:
        q = tarama.tokens(sorgu.ad, sorgu.tip)
        if not q:
            return tarama.karar(sorgu, [], esik) | {"aday_sayisi": 0}
        adaylar = self.kayitlar(self.aday_idleri(q, sorgu.tip))
        skorlar = [tarama.skorla(sorgu, k, q) for k in adaylar]
        return tarama.karar(sorgu, skorlar, esik) | {"aday_sayisi": len(adaylar)}

    def close(self) -> None:
        self.db.close()
