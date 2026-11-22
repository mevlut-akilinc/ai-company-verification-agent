"""Generate tiny synthetic raw files in the real upstream formats.

Used by the tests and for trying the pipeline without downloading ~3.5 GB.
All names are invented. Usage:  python scripts/make_fixtures.py <raw_dir>
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path


def _zip(path: Path, member: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, text)


def _csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _ind(cn, name, natures, idx, ceased=None, nat="Turkish"):
    d = {"kind": "individual-person-with-significant-control", "name": name,
         "natures_of_control": natures, "nationality": nat, "country_of_residence": "England",
         "date_of_birth": {"month": 3, "year": 1980}, "notified_on": "2020-01-01",
         "address": {"premises": "1", "address_line_1": "Fake Street"},
         "links": {"self": f"/company/{cn}/persons-with-significant-control/individual/IND{idx}"}}
    if ceased:
        d["ceased_on"] = ceased
    return {"company_number": cn, "data": d}


def _corp(cn, name, natures, idx, reg, country="England", authority="Companies Act 2006"):
    return {"company_number": cn, "data": {
        "kind": "corporate-entity-person-with-significant-control", "name": name,
        "natures_of_control": natures, "notified_on": "2019-05-01",
        "identification": {"registration_number": reg, "country_registered": country,
                           "legal_authority": authority, "legal_form": "Limited"},
        "links": {"self": f"/company/{cn}/persons-with-significant-control/corporate-entity/CORP{idx}"}}}


def psc_records() -> list[dict]:
    r = [
        # chain: 03 -> 02 -> 01
        _corp("00000001", "DEMO MIDCO LIMITED", ["ownership-of-shares-75-to-100-percent"], 1, "2"),
        _corp("00000002", "DEMO TOPCO LIMITED", ["ownership-of-shares-50-to-75-percent",
                                                 "voting-rights-50-to-75-percent"], 2, "Company No. 0000003"),
        _ind("00000002", "Mehmet Kaya", ["ownership-of-shares-25-to-50-percent"], 3),
        # foreign owner whose number would collide with UK 00012345 if not checked
        _corp("00000003", "ORANJE BEHEER B.V.", ["ownership-of-shares-50-to-75-percent"], 4,
              "12345", country="Netherlands", authority="Dutch Civil Code"),
        _ind("00000003", "Ayse Yilmaz", ["ownership-of-shares-25-to-50-percent-as-trust",
                                         "right-to-appoint-and-remove-directors"], 5),
        _ind("00000003", "Old Owner", ["ownership-of-shares-25-to-50-percent"], 6, ceased="2021-06-30"),
        {"company_number": "00000002", "data": {
            "kind": "persons-with-significant-control-statement",
            "statement": "psc-details-not-confirmed", "notified_on": "2022-01-01",
            "links": {"self": "/company/00000002/persons-with-significant-control-statements/S1"}}},
        # circular ownership: 10 <-> 11
        _corp("00000010", "LOOP B LIMITED", ["ownership-of-shares-75-to-100-percent"], 7, "11"),
        _corp("00000011", "LOOP A LIMITED", ["ownership-of-shares-75-to-100-percent"], 8, "00000010"),
        _ind("00000011", "Can Demir", ["significant-influence-or-control"], 9),
        # super-secure PSC
        {"company_number": "00000021", "data": {
            "kind": "super-secure-person-with-significant-control",
            "description": "super-secure-persons-with-significant-control",
            "links": {"self": "/company/00000021/persons-with-significant-control/super-secure/SS1"}}},
    ]
    # simple individually owned companies 20..30 + 00012345
    for i, cn in enumerate([f"000000{n}" for n in range(20, 31)] + ["00012345"]):
        r.append(_ind(cn, f"Owner {i}", ["ownership-of-shares-75-to-100-percent",
                                         "voting-rights-75-to-100-percent"], 100 + i, nat="British"))
    r.append({"data": {"kind": "totals#persons-of-significant-control-snapshot",
                       "persons_of_significant_control_count": len(r)}})
    return r


def basic_rows() -> list[list]:
    names = {"00000001": "DEMO OPCO LIMITED", "00000002": "DEMO MIDCO LIMITED",
             "00000003": "DEMO TOPCO LIMITED", "00000010": "LOOP A LIMITED",
             "00000011": "LOOP B LIMITED", "00012345": "UNRELATED TRADING LIMITED"}
    names |= {f"000000{n}": f"SIMPLE {n} LIMITED" for n in range(20, 31)}
    return [[name, cn, "Private Limited Company", "Active", "United Kingdom", "01/01/2015",
             "LS1 1AA", "70100 - Activities of head offices", "None Supplied"]
            for cn, name in names.items()]


BASIC_HEADER = ["CompanyName", " CompanyNumber", "CompanyCategory", "CompanyStatus",
                "RegAddress.Country", "IncorporationDate", "RegAddress.PostCode",
                " SICCode.SicText_1", "SICCode.SicText_2"]

LEI_HEADER = ["LEI", "Entity.LegalName", "Entity.LegalAddress.Country", "Entity.LegalJurisdiction",
              "Entity.EntityStatus", "Registration.RegistrationStatus",
              "Entity.RegistrationAuthority.RegistrationAuthorityID",
              "Entity.RegistrationAuthority.RegistrationAuthorityEntityID"]
LEI_ROWS = [
    ["LEITOPCO000000000003", "Demo Topco Ltd", "GB", "GB", "ACTIVE", "ISSUED", "RA000585", "3"],
    ["LEISIMPLE00000000021", "Simple Twenty-One Ltd", "GB", "GB", "ACTIVE", "ISSUED", "RA000585", "00000021"],
    ["LEIPARENTNL000000001", "Oranje Beheer B.V.", "NL", "NL", "ACTIVE", "ISSUED", "RA000463", "12345"],
    ["LEIDISTRACTOR0000001", "Demo Topco Holdings Plc", "GB", "GB", "ACTIVE", "ISSUED", "RA000585", "99999999"],
    ["LEIDISTRACTOR0000002", "Loop Advisory LLP", "GB", "GB", "ACTIVE", "LAPSED", "RA000585", "OC999999"],
    ["LEIFOREIGN0000000009", "Irrelevant GmbH", "DE", "DE", "ACTIVE", "ISSUED", "RA000242", "HRB 1"],
]
RR_HEADER = ["Relationship.StartNode.NodeID", "Relationship.StartNode.NodeIDType",
             "Relationship.EndNode.NodeID", "Relationship.EndNode.NodeIDType",
             "Relationship.RelationshipType", "Relationship.RelationshipStatus"]
RR_ROWS = [
    ["LEITOPCO000000000003", "LEI", "LEIPARENTNL000000001", "LEI", "IS_DIRECTLY_CONSOLIDATED_BY", "ACTIVE"],
    ["LEITOPCO000000000003", "LEI", "LEIPARENTNL000000001", "LEI", "IS_ULTIMATELY_CONSOLIDATED_BY", "ACTIVE"],
    ["LEIFOREIGN0000000009", "LEI", "LEIFOREIGN0000000009", "LEI", "IS_DIRECTLY_CONSOLIDATED_BY", "ACTIVE"],
]
OS_HEADER = ["id", "schema", "name", "aliases", "birth_date", "countries", "addresses", "identifiers",
             "sanctions", "phones", "emails", "program_ids", "dataset", "first_seen", "last_seen", "last_change"]
OS_ROWS = [
    ["os-demo-1", "Person", "Ayse Yilmazer", "Ayşe Yılmazer;A. Yilmazer", "1979", "tr", "", "",
     "Demo sanction", "", "", "", "Demo List", "2024-01-01", "2026-11-01", "2026-01-01"],
    ["os-demo-2", "Company", "Shadow Trading LLC", "", "", "ae", "", "", "", "", "", "", "Demo List",
     "2024-01-01", "2026-11-01", "2026-01-01"],
    ["os-demo-3", "Vessel", "MV Example", "", "", "", "", "", "", "", "", "", "Demo List",
     "2024-01-01", "2026-11-01", "2026-01-01"],
]


def write_fixtures(raw: Path) -> None:
    raw = Path(raw)
    _zip(raw / "ch_psc" / "persons-with-significant-control-snapshot-2026-01-01.zip",
         "psc.txt", "\n".join(json.dumps(r) for r in psc_records()) + "\n")
    _zip(raw / "ch_basic" / "BasicCompanyDataAsOneFile-2026-01-01.zip", "basic.csv",
         _csv(BASIC_HEADER, basic_rows()))
    _zip(raw / "gleif_lei" / "lei2.csv.zip", "lei2.csv", _csv(LEI_HEADER, LEI_ROWS))
    _zip(raw / "gleif_rr" / "rr.csv.zip", "rr.csv", _csv(RR_HEADER, RR_ROWS))
    (raw / "opensanctions").mkdir(parents=True, exist_ok=True)
    (raw / "opensanctions" / "default_targets.simple.csv").write_text(_csv(OS_HEADER, OS_ROWS), encoding="utf-8")


if __name__ == "__main__":
    write_fixtures(Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw"))
    print("fixtures written")
