import pytest
from inpro_copilot.normalize import parse_amount, find_amounts, parse_date, detect_currency


@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", 1234.56), ("1.234,56", 1234.56), ("4.904,94", 4904.94), ("2.321,00", 2321.0),
    ("34,73", 34.73), ("29.99", 29.99), ("€ 4,24", 4.24), ("$4.11", 4.11), ("Rs 1939", 1939.0),
    ("-9,32", -9.32), ("1,939", 1939.0), ("127.50", 127.5), ("0,75 €", 0.75), ("abc", None),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_find_amounts_skips_percent_and_splits_columns():
    assert find_amounts("BTW 21% € 124,61") == [124.61]
    assert find_amounts("Total facture 24.99 5.00 29.99") == [24.99, 5.0, 29.99]
    assert find_amounts("Totaal € 717,97 ''iDEAL'' op 21 april 2015 € 717,97")[0] == 717.97


@pytest.mark.parametrize("raw,mdy,expected", [
    ("2022-11-28", False, "2022-11-28"), ("28/11/2022", False, "2022-11-28"),
    ("03/20/2023", False, "2023-03-20"), ("31/12/2017", False, "2017-12-31"),
    ("20-10-2015", False, "2015-10-20"), ("7. Mai 2014", False, "2014-05-07"),
    ("19 april 2014", False, "2014-04-19"), ("02 Juillet 2015", False, "2015-07-02"),
    ("Jan 1, 2022", False, "2022-01-01"), ("August 3 , 2014", False, "2014-08-03"),
    ("09/06/2012", True, "2012-09-06"), ("09/06/2012", False, "2012-06-09"),
    ("8-9-2022", False, "2022-09-08"), ("no date here", False, None),
])
def test_parse_date(raw, mdy, expected):
    assert parse_date(raw, prefer_mdy=mdy) == expected


def test_currency():
    assert detect_currency("Totaal € 717,97 € 9,32") == "EUR"
    assert detect_currency("Rs 1939 x 1 Night Rs 1939") == "INR"
    assert detect_currency("$4.11 All charges are in US Dollars") == "USD"


def test_thousands_with_a_space_next_to_a_currency_sign():
    from inpro_copilot.extractor_rules import find_money
    assert find_money("Total $ 5 640,17 $ 564,02 $ 6 204,19") == [5640.17, 564.02, 6204.19]
    assert find_money("Montant 1 234,56 €") == [1234.56]
    assert find_money("Total 1 278.61 40.39 319.00") == [278.61, 40.39, 319.0]      # quantity 1, then a price
