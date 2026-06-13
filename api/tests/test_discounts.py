from services.special_crawler.discounts import classify_discount, discount_fraction


def test_no_discount_returns_none():
    assert classify_discount(5.0, 0.0) is None
    assert classify_discount(5.0, 5.0) is None      # was == now
    assert classify_discount(6.0, 5.0) is None       # was < now (bad data)
    assert classify_discount(0.0, 0.0) is None


def test_half_price_band():
    assert classify_discount(4.0, 8.0) == "half_price"   # exactly 50%
    assert classify_discount(5.1, 10.0) == "half_price"  # 49%
    assert classify_discount(4.8, 10.0) == "half_price"  # 52%


def test_beyond_half():
    assert classify_discount(3.0, 8.0) == "beyond_half"   # 62.5%
    assert classify_discount(0.1, 10.0) == "beyond_half"  # 99%
    assert classify_discount(4.0, 10.0) == "beyond_half"  # 60%


def test_normal_discount():
    assert classify_discount(8.0, 10.0) == "discount"    # 20%
    assert classify_discount(7.0, 10.0) == "discount"    # 30%
    assert classify_discount(6.0, 10.0) == "discount"    # 40%


def test_explicit_half_price_flag_overrides():
    # Woolworths IsHalfPrice flag wins even if rounding lands outside the band
    assert classify_discount(4.6, 10.0, is_half_price=True) == "half_price"   # 54%
    assert classify_discount(5.5, 10.0, is_half_price=True) == "half_price"   # 45%
    # but a flagged item with no was-price is still labelled half_price
    assert classify_discount(5.0, 0.0, is_half_price=True) == "half_price"


def test_discount_fraction():
    assert discount_fraction(5.0, 10.0) == 0.5
    assert discount_fraction(5.0, 5.0) is None
    assert discount_fraction(5.0, 0.0) is None
