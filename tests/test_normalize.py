import pytest

from kyb.normalize import looks_uk_registered, normalize_company_number, parse_natures


@pytest.mark.parametrize("raw,expected", [
    ("1234567", "01234567"),
    ("01234567", "01234567"),
    ("SC 12345", "SC012345"),
    ("sc012345", "SC012345"),
    ("OC301234", "OC301234"),
    ("Company No. 09876543", "09876543"),
    ("Registered number: 3", "00000003"),
    ("", None),
    (None, None),
    ("0", None),
    ("HRB 12345 B", None),
    ("123456789", None),
])
def test_company_number(raw, expected):
    assert normalize_company_number(raw) == expected


@pytest.mark.parametrize("ident,expected", [
    ({"country_registered": "England", "legal_authority": "Companies Act 2006"}, True),
    ({"place_registered": "Companies House, Cardiff"}, True),
    ({"country_registered": "United Kingdom"}, True),
    ({"country_registered": "Netherlands", "legal_authority": "Dutch Civil Code"}, False),
    ({"country_registered": "Luxembourg"}, False),  # must not match the 'uk' token inside a word
    ({}, False),
    (None, False),
])
def test_uk_hint(ident, expected):
    assert looks_uk_registered(ident) is expected


def test_natures_bands_and_flags():
    c = parse_natures([
        "ownership-of-shares-25-to-50-percent-as-trust",
        "voting-rights-75-to-100-percent",
        "right-to-appoint-and-remove-directors",
    ])
    assert (c.share_min, c.share_max) == (25, 50)
    assert (c.vote_min, c.vote_max) == (75, 100)
    assert c.appoint_directors and c.via_trust and not c.via_firm
    assert c.unknown == []


def test_natures_overseas_and_llp():
    c = parse_natures([
        "ownership-of-shares-more-than-25-percent-registered-overseas-entity",
        "right-to-share-surplus-assets-50-to-75-percent-limited-liability-partnership",
        "something-new",
    ])
    assert (c.share_min, c.share_max) == (25, 100)  # widened across both
    assert c.unknown == ["something-new"]
def test_natures_with_missing_items():
    c = parse_natures([None, "", "ownership-of-shares-25-to-50-percent"])
    assert (c.share_min, c.share_max) == (25, 50)
    assert c.unknown == ["<bos ifade>", "<bos ifade>"]
