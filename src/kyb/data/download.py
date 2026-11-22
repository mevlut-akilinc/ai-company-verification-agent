"""Download the raw bulk files.

Sources (all public):
  ch_psc        Companies House PSC snapshot (JSON lines, ~2.5 GB zipped, single file or 30+ parts)
  ch_basic      Companies House Free Company Data Product (CSV, ~470 MB zipped)
  gleif_lei     GLEIF Golden Copy Level 1, LEI-CDF CSV (~490 MB zipped)
  gleif_rr      GLEIF Golden Copy Level 2, relationship records CSV (~25 MB zipped)
  opensanctions OpenSanctions targets.simple.csv (CC BY-NC 4.0, non-commercial use)

Download URLs carry the publication date, so they are discovered from the
index pages / API at run time instead of being hard-coded.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from tqdm import tqdm

CH_BASE = "https://download.companieshouse.gov.uk/"
CH_PSC_PAGE = CH_BASE + "en_pscdata.html"
CH_BASIC_PAGE = CH_BASE + "en_output.html"
GLEIF_LATEST_API = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
OPENSANCTIONS_URL = "https://data.opensanctions.org/datasets/latest/{collection}/targets.simple.csv"

ALL_SOURCES = ("ch_psc", "ch_basic", "gleif_lei", "gleif_rr", "opensanctions")

_HEADERS = {"User-Agent": "kyb-analyst/0.1 (student research project)"}


def _zip_links(page_url: str) -> list[str]:
    html = requests.get(page_url, headers=_HEADERS, timeout=60).text
    links = re.findall(r'href="([^"]+\.zip)"', html, flags=re.I)
    return sorted({urljoin(page_url, l) for l in links})


def ch_psc_urls(parts: list[int] | None = None) -> list[str]:
    """Single snapshot file, or selected numbered parts (e.g. [1, 2, 3])."""
    links = _zip_links(CH_PSC_PAGE)
    if parts is None:
        single = [l for l in links if "persons-with-significant-control-snapshot" in l]
        if not single:
            raise RuntimeError(f"No single PSC snapshot link found on {CH_PSC_PAGE}")
        return single[:1]
    chosen = []
    for l in links:
        m = re.search(r"_(\d+)of(\d+)\.zip$", l)
        if m and int(m.group(1)) in parts:
            chosen.append(l)
    if not chosen:
        raise RuntimeError(f"No PSC part links matched {parts} on {CH_PSC_PAGE}")
    return chosen


def ch_basic_urls() -> list[str]:
    links = [l for l in _zip_links(CH_BASIC_PAGE) if "BasicCompanyDataAsOneFile" in l]
    if not links:
        raise RuntimeError(f"No BasicCompanyDataAsOneFile link found on {CH_BASIC_PAGE}")
    return links[:1]


def gleif_urls() -> dict[str, str]:
    data = requests.get(GLEIF_LATEST_API, headers=_HEADERS, timeout=60).json()["data"]
    return {
        "gleif_lei": data["lei2"]["full_file"]["csv"]["url"],
        "gleif_rr": data["rr"]["full_file"]["csv"]["url"],
    }


def download(url: str, dest_dir: Path, filename: str | None = None) -> Path:
    """Stream to disk with resume support (.part file). Skips if the file already exists."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = filename or url.rstrip("/").split("/")[-1]
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  = {dest.name} already present, skipping")
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    pos = part.stat().st_size if part.exists() else 0
    headers = dict(_HEADERS)
    if pos:
        headers["Range"] = f"bytes={pos}-"
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        if pos and r.status_code != 206:  # server ignored Range -> restart
            pos = 0
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0)) + pos
        mode = "ab" if pos else "wb"
        with open(part, mode) as fh, tqdm(
            total=total or None, initial=pos, unit="B", unit_scale=True, desc=name
        ) as bar:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                bar.update(len(chunk))
    part.rename(dest)
    return dest


def run(raw_dir: Path, sources: list[str], psc_parts: list[int] | None = None,
        os_collection: str = "default") -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    if "ch_psc" in sources:
        print("Companies House PSC snapshot")
        out["ch_psc"] = [download(u, raw_dir / "ch_psc") for u in ch_psc_urls(psc_parts)]
    if "ch_basic" in sources:
        print("Companies House basic company data")
        out["ch_basic"] = [download(u, raw_dir / "ch_basic") for u in ch_basic_urls()]
    if {"gleif_lei", "gleif_rr"} & set(sources):
        urls = gleif_urls()
        for key in ("gleif_lei", "gleif_rr"):
            if key in sources:
                print(f"GLEIF {key}")
                out[key] = [download(urls[key], raw_dir / key)]
    if "opensanctions" in sources:
        print(f"OpenSanctions ({os_collection})")
        url = OPENSANCTIONS_URL.format(collection=os_collection)
        out["opensanctions"] = [
            download(url, raw_dir / "opensanctions", f"{os_collection}_targets.simple.csv")
        ]
    return out


def parse_parts(spec: str | None) -> list[int] | None:
    """'1-5,8' -> [1,2,3,4,5,8]"""
    if not spec:
        return None
    nums: set[int] = set()
    for chunk in spec.split(","):
        if "-" in chunk:
            a, b = chunk.split("-")
            nums.update(range(int(a), int(b) + 1))
        elif chunk.strip():
            nums.add(int(chunk))
    return sorted(nums)
