from inpro_copilot.taxid import validate_gstin, validate_tax_id, find_tax_ids, gstin_checksum_char


def test_real_gstin_from_oyo_invoice_is_valid():
    assert validate_gstin("06AABCO6063D1ZQ").valid          # printed on the real OYO document


def test_one_wrong_character_is_caught():
    r = validate_gstin("06AABCO6063D1ZP")
    assert not r.valid and "check character" in r.reason


def test_invalid_state_code():
    assert not validate_gstin("99AABCO6063D1ZQ".replace("99", "50")).valid


def test_checksum_roundtrip():
    g14 = "27AAPFU0939F1Z"
    assert validate_gstin(g14 + gstin_checksum_char(g14)).valid


def test_eu_and_uae():
    assert validate_tax_id("DE 232 446 240").valid
    assert validate_tax_id("NL810433941B01").valid
    assert validate_tax_id("100123456789012").valid
    assert not validate_tax_id("12345").valid


def test_finder_does_not_overcapture_and_ignores_buyer_number():
    txt = "KvK Rotterdam 24330087\nBTW NL810433941B01\nFactuurnummer: 993548900"
    assert [r.value for r in find_tax_ids(txt)] == ["NL810433941B01"]
    assert find_tax_ids("Uw BTW nummer: NL00333599698") == []
    assert [r.value for r in find_tax_ids("UStId DE 232 446 240 \nSteuer-Nr. 044")] == ["DE232446240"]
