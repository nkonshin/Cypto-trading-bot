from copytrade.model import Box, Pos
from copytrade.parse import (find_cards, parse_balance, parse_card, parse_form, parse_number, parse_tab,
                             parse_tpsl, valid_tpsl)


def B(x, y, text, conf=0.99):
    return Box(x, y, 40, 9, text, conf)


def test_parse_number_strict():
    assert parse_number("59,571.7") == 59571.7
    assert parse_number("0.2000") == 0.2
    assert parse_number("245.5788 ®") == 245.5788
    assert parse_number("+97.1200(40.75%)") == 97.12
    assert parse_number("54,3210") is None          # потерянная точка — не угадываем
    assert parse_number("04000") is None            # реальный сбой OCR: «0.4000» → «04000» с conf 0.99
    assert parse_number("5,9571.7") is None
    assert parse_number("abc") is None
    assert parse_number("") is None


def test_parse_tpsl():
    assert parse_tpsl("54,321.0($1,050.14)/59,750.0($35.66)>") == (54321.0, 59750.0)
    assert parse_tpsl("54,321.0($1,050.14)59,750.0($35.66)>") == (54321.0, 59750.0)   # без слеша
    assert parse_tpsl("54,321.0 （$1,050.14) / 59,750.0 ($35.66)") == (54321.0, 59750.0)
    assert parse_tpsl("--/--") == (None, None)
    assert parse_tpsl("54,321.0($1,050.14)/--") == (54321.0, None)
    assert parse_tpsl("54,3210($1.050.14)/59") is None
    assert parse_tpsl("All Positions") is None


def test_parse_tpsl_ocr_glitches_resolved_by_market_price():
    # реальный сбой OCR: «)/» → «V», запятая тысяч → точка
    glitch = "54,321.0($2,009.6V59.600.0($102)"
    assert parse_tpsl(glitch) is None                         # без цены рынка не угадываем
    assert parse_tpsl(glitch, mark=58981.0) == (54321.0, 59600.0)
    assert parse_tpsl("54.321.0($2.009.6)/59.600.0($102)", mark=58981.0) == (54321.0, 59600.0)
    assert parse_tpsl("54,321.0($2,009.6V59.6000($102))", mark=58934.1) == (54321.0, 59600.0)  # сбой на сервере
    assert parse_tpsl("5.432.10($2,009.6)/59,600.0($102)", mark=58981.0) == (54321.0, 59600.0)
    assert parse_tpsl("9($2,009.6)/59,600.0($102)", mark=58981.0) is None


def test_valid_tpsl_respects_side_and_mark():
    assert valid_tpsl("short", 59086.1, 54321.0, 59750.0)
    assert not valid_tpsl("short", 59086.1, 59750.0, 54321.0)     # перепутаны местами
    assert valid_tpsl("long", 59086.1, 62000.0, 57000.0)
    assert not valid_tpsl("long", 59086.1, 5432.0, 57000.0)       # вне ±50% от рынка
    assert not valid_tpsl("short", None, 54321.0, 59750.0)


def test_parse_tab_variants():
    assert parse_tab([B(6, 4, "Posiltion (1)Open Orders（1)Order History")]) == 1
    assert parse_tab([B(6, 4, "Position(0)Open Orders(0)Order History")]) == 0
    assert parse_tab([B(6, 4, "Pesition (2)")]) == 2
    assert parse_tab([B(6, 60, "Position (1)")]) is None              # не в строке вкладок
    assert parse_tab([B(6, 4, "Order History")]) is None


CARD = [
    B(7, 53, "BTCUSDTPerpetual>"),
    B(7, 83, "Pesions (BTC)", 0.91), B(119, 84, "Margin (USDT)"), B(299, 84, "Margin Ratio"),
    B(6, 94, "0.2000", 1.0), B(119, 94, "245.5788", 0.99), B(312, 94, "13.91%"),
    B(6, 110, "Average PositionPrice"), B(118, 110, "MarkPrice"),
    B(6, 121, "59,571.7"), B(118, 120, "59,086.1", 0.98), B(307, 121, "60,524.8"),
]


def test_parse_card_ok():
    (sym_box, sym), = find_cards(CARD)
    p, why = parse_card(CARD, sym_box, sym)
    assert why == ""
    assert (p.symbol, p.size, p.avg, p.margin, p.mark) == ("BTCUSDT", 0.2, 59571.7, 245.5788, 59086.1)
    assert p.leverage == 50


def test_parse_card_garbled_unit_label_still_ok():
    for label in ("Poions 0BTC)", "Posiions (BTC3", "Posions (BTC)"):
        boxes = [b if "BTC)" not in b.text else b._replace(text=label, conf=0.72) for b in CARD]
        (sym_box, sym), = find_cards(boxes)
        p, why = parse_card(boxes, sym_box, sym)
        assert why == "" and p.size == 0.2, label


def test_parse_card_rejects_size_inconsistent_with_margin():
    boxes = [b if b.text != "0.2000" else b._replace(text="04000") for b in CARD]
    (sym_box, sym), = find_cards(boxes)
    assert parse_card(boxes, sym_box, sym)[0] is None
    boxes = [b if b.text != "0.2000" else b._replace(text="2.0000") for b in CARD]   # 10× — плечо 485x
    (sym_box, sym), = find_cards(boxes)
    p, why = parse_card(boxes, sym_box, sym)
    assert p is None and "не сходятся" in why


def test_parse_card_requires_margin():
    boxes = [b for b in CARD if b.text != "245.5788"]
    (sym_box, sym), = find_cards(boxes)
    p, why = parse_card(boxes, sym_box, sym)
    assert p is None and "маржа" in why


def test_parse_card_rejects_low_confidence_size():
    boxes = [b if b.text != "0.2000" else b._replace(conf=0.7) for b in CARD]
    (sym_box, sym), = find_cards(boxes)
    p, why = parse_card(boxes, sym_box, sym)
    assert p is None and "объём" in why


def test_parse_card_rejects_contract_units_and_bad_symbol():
    boxes = [b if "BTC)" not in b.text else b._replace(text="Positions (Cont)") for b in CARD]   # контракты
    (sym_box, sym), = find_cards(boxes)
    assert parse_card(boxes, sym_box, sym)[0] is None
    boxes = [b if "Perpetual" not in b.text else b._replace(text="BTCU5DTPerpetual>") for b in CARD]
    (sym_box, sym), = find_cards(boxes)
    p, why = parse_card(boxes, sym_box, sym)
    assert p is None and "монета" in why


def test_usdt_suffix_ocr_glitch_fixed():
    for text in ("BTCUSOTPerpetual>", "BTCUSD7Perpetual", "ETHUS0TPerpetual >"):
        (_, sym), = find_cards([B(7, 53, text)])
        assert sym.endswith("USDT"), text


def test_leverage_without_margin_is_unknown():
    assert Pos("BTCUSDT", "short", 0.2, 59571.7).leverage is None
    assert Pos("BTCUSDT", "short", 0.2, 59571.7, margin=1191.4).leverage == 10


def test_parse_balance():
    assert parse_balance([B(2, 6, "AssetsVahue", 0.93), B(2, 26, "748.7413USDT-", 0.95), B(90, 26, "$748.74")]) == 748.7413
    assert parse_balance([B(2, 26, "748.7413USDT-", 0.6)]) is None



def test_form_leverage_catches_digit_substitution():
    """0.4 → 0.6 даёт 73x — близко к стандартным 75x, но плечо в форме 50x: чтение отбрасывается."""
    card = [B(7, 53, "BTCUSDTPerpetual>"), B(7, 83, "Posons (BTC)", 0.74), B(6, 94, "0.6000", 0.95),
            B(120, 94, "486.1696"), B(7, 110, "Average PositionPrice"), B(7, 121, "58,967.2"), B(120, 121, "58,666.1")]
    (sb, sym), = find_cards(card)
    assert parse_card(card, sb, sym)[0] is not None                     # без плеча формы прошло бы как 75x
    p, why = parse_card(card, sb, sym, form_lev=50)
    assert p is None and "50x" in why
    ok = [b if b.text != "0.6000" else b._replace(text="0.4000") for b in card]
    p, _ = parse_card(ok, sb, sym, form_lev=50)
    assert p.size == 0.4 and p.leverage == 50


def test_two_size_reads_must_agree():
    (sb, sym), = find_cards(CARD)
    p, why = parse_card(CARD, sb, sym, size_x4=0.3)
    assert p is None and "по-разному" in why
    assert parse_card(CARD, sb, sym, size_x4=0.2)[0].size == 0.2


def test_parse_form():
    assert parse_form([B(0, 0, "BTCUSDTPerpetual")], [B(0, 0, "lsolated", 0.96), B(90, 0, "50X", 0.92)]) == ("BTCUSDT", 50)
    assert parse_form([B(0, 0, "ETHUSOT Perpetual")], [B(90, 0, "20X", 0.6)]) == ("ETHUSDT", None)
