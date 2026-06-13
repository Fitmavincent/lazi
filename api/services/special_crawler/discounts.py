"""
Shared discount classification for all retailers.

`discount_type` is a nullable product field that lets the frontend render
different discount tiers consistently across Coles and Woolworths:

  - "half_price"  : ~50% off (48-52%), or the retailer's explicit half-price flag
  - "beyond_half" : more than ~half off (> 52%)
  - "discount"    : a normal discount below ~half (< 48%)
  - None          : no measurable was/now discount

The bands carry a small tolerance around 50% because retailers brand items as
"½ Price" even when rounding makes the computed percentage 49% or 51%.
"""

HALF_PRICE_LOW = 0.48
HALF_PRICE_HIGH = 0.52


def discount_fraction(price: float, was_price: float) -> float | None:
    """Fraction off (0..1), or None when there's no genuine was/now discount."""
    if not was_price or not price or was_price <= price:
        return None
    return (was_price - price) / was_price


def classify_discount(price: float, was_price: float, is_half_price: bool = False) -> str | None:
    """Return the discount tier label, or None when there's no discount.

    `is_half_price` lets a retailer's explicit half-price flag (e.g. Woolworths'
    IsHalfPrice) take precedence over the computed band.
    """
    frac = discount_fraction(price, was_price)
    if frac is None:
        return "half_price" if is_half_price else None
    if is_half_price:
        return "half_price"
    if frac > HALF_PRICE_HIGH:
        return "beyond_half"
    if frac >= HALF_PRICE_LOW:
        return "half_price"
    return "discount"
