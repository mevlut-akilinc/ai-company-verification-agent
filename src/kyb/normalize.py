"""Deterministic normalisation helpers (no LLM involved).

* UK company numbers -> canonical 8-character form
* PSC ``natures_of_control`` strings -> numeric share/vote ranges + control flags
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Company numbers
# ---------------------------------------------------------------------------

_PREFIXED = re.compile(r"^([A-Z]{2})(\d{1,6})$")
_NUMERIC = re.compile(r"^\d{1,8}$")
_NOISE = re.compile(r"(COMPANY|REGISTRATION|REGISTERED|NUMBER|NO\b|NO\.|NR|#|:)")

_UK_HINTS = (
    "england", "wales", "scotland", "northern ireland", "united kingdom", "great britain",
    "companies house", "uk", "gb", "britain", "cardiff", "edinburgh", "belfast",
)


def normalize_company_number(raw: str | None) -> str | None:
    """Return the canonical 8-char UK company number, or None if not plausible.

    >>> normalize_company_number("1234567")
    '01234567'
    >>> normalize_company_number("SC 12345")
    'SC012345'
    >>> normalize_company_number("Company No. 09876543")
    '09876543'
    """
    if not raw:
        return None
    s = str(raw).upper()
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^A-Z0-9]", "", s)
    if not s:
        return None
    m = _PREFIXED.match(s)
    if m:
        return m.group(1) + m.group(2).zfill(6)
    if _NUMERIC.match(s):
        if int(s) == 0:
            return None
        return s.zfill(8)
    return None


def looks_uk_registered(identification: dict | None) -> bool:
    """Heuristic: does a corporate PSC's identification block point to a UK register?

    Needed because a foreign number (e.g. German HRB 12345) would otherwise
    normalise to '00012345' and silently collide with an unrelated UK company.
    """
    if not identification:
        return False
    text = " ".join(
        str(identification.get(k, "") or "")
        for k in ("country_registered", "place_registered", "legal_authority")
    ).lower()
    if not text.strip():
        return False
    tokens = set(re.findall(r"[a-z]+", text))
    for hint in _UK_HINTS:
        if " " in hint:
            if hint in text:
                return True
        elif hint in tokens:
            return True
    return False


# ---------------------------------------------------------------------------
# PSC natures of control
# ---------------------------------------------------------------------------

_RANGE = re.compile(
    r"^(?P<kind>ownership-of-shares|voting-rights|right-to-share-surplus-assets)-"
    r"(?:(?P<lo>\d+)-to-(?P<hi>\d+)|more-than-(?P<more>\d+))-percent"
)


@dataclass
class ControlSummary:
    """Numeric reading of PSC natures of control.

    PSC data only gives bands (e.g. 'more than 25% up to 50%'), never exact
    percentages, so ownership is carried as an interval [min, max].
    """

    share_min: float | None = None
    share_max: float | None = None
    vote_min: float | None = None
    vote_max: float | None = None
    appoint_directors: bool = False
    significant_influence: bool = False
    via_trust: bool = False
    via_firm: bool = False
    unknown: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "share_min": self.share_min,
            "share_max": self.share_max,
            "vote_min": self.vote_min,
            "vote_max": self.vote_max,
            "appoint_directors": self.appoint_directors,
            "significant_influence": self.significant_influence,
            "via_trust": self.via_trust,
            "via_firm": self.via_firm,
            "unknown_natures": self.unknown,
        }


def _widen(cur_lo, cur_hi, lo, hi):
    if cur_lo is None:
        return lo, hi
    return min(cur_lo, lo), max(cur_hi, hi)


def parse_natures(natures: list[str] | None) -> ControlSummary:
    out = ControlSummary()
    for raw in natures or []:
        if not isinstance(raw,str) or not raw.strip():
            out.unknown.append("<bos ifade>")
            continue
        n = raw.strip().lower()
        if n.endswith("-as-trust") or "-as-trust-" in n:
            out.via_trust = True
        if n.endswith("-as-firm") or "-as-firm-" in n:
            out.via_firm = True
        m = _RANGE.match(n)
        if m:
            if m.group("more"):
                lo, hi = float(m.group("more")), 100.0
            else:
                lo, hi = float(m.group("lo")), float(m.group("hi"))
            if m.group("kind") == "voting-rights":
                out.vote_min, out.vote_max = _widen(out.vote_min, out.vote_max, lo, hi)
            else:
                out.share_min, out.share_max = _widen(out.share_min, out.share_max, lo, hi)
            continue
        if n.startswith("right-to-appoint-and-remove"):
            out.appoint_directors = True
            continue
        if n.startswith("significant-influence-or-control"):
            out.significant_influence = True
            continue
        out.unknown.append(raw)
    return out
