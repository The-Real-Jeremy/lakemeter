"""PostgreSQL NUMERIC semantics in Python.

The pricing functions in ``bootstrap/sql/functions.sql`` are PL/pgSQL. The
SQLite backend runs a Python port of them (``pricing_engine.py``) and must
return the same numbers to the cent, so the port reproduces how PostgreSQL
does arithmetic rather than how Python floats do:

* ``float8 -> numeric`` keeps 15 significant digits (``float8_numeric``).
* ``+``, ``-`` and ``*`` on numerics are exact.
* ``/`` rounds to the scale chosen by ``select_div_scale`` (at least 16
  significant digits), half away from zero.
* Assigning to a ``DECIMAL(p, s)`` variable rounds to ``s`` places, half away
  from zero. Function return types do NOT round (PostgreSQL ignores the typmod
  of a declared return type), so only variables and explicit casts do.
* SQL three-valued logic: NULL propagates through arithmetic, ``NOT``, ``AND``,
  and an ``IF`` on NULL takes the ELSE branch.

``None`` stands for SQL NULL throughout.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP, Context
from fractions import Fraction
from typing import Any, Optional

_CTX = Context(prec=4000, rounding=ROUND_HALF_UP)

NUMERIC_MIN_SIG_DIGITS = 16
DEC_DIGITS = 4
NUMERIC_MAX_DISPLAY_SCALE = 1000


class PgError(Exception):
    """An error PostgreSQL would raise at the same point."""


def _no_positive_exponent(d: Decimal) -> Decimal:
    if d.as_tuple().exponent > 0:
        return d.quantize(Decimal(1), context=_CTX)
    return d


def num(value: Any) -> Optional[Decimal]:
    """A value as a PostgreSQL numeric (float8 keeps 15 significant digits)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise PgError("cannot cast type boolean to numeric")
    if isinstance(value, Decimal):
        return _no_positive_exponent(value)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return _no_positive_exponent(Decimal(format(value, ".15g")))
    if isinstance(value, str):
        try:
            return _no_positive_exponent(Decimal(value.strip()))
        except Exception as exc:
            raise PgError(
                f'invalid input syntax for type numeric: "{value}"'
            ) from exc
    raise PgError(f"cannot cast {type(value).__name__} to numeric")


def dscale(d: Decimal) -> int:
    return max(0, -d.as_tuple().exponent)


def typmod(value: Optional[Decimal], scale: int) -> Optional[Decimal]:
    """Assignment to DECIMAL(p, scale): round half away from zero."""
    if value is None:
        return None
    return num(value).quantize(Decimal(1).scaleb(-scale), context=_CTX)


def coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def add(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
    if a is None or b is None:
        return None
    return _CTX.add(num(a), num(b))


def mul(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
    if a is None or b is None:
        return None
    return _CTX.multiply(num(a), num(b))


def _weight_and_first_digit(d: Decimal) -> tuple[int, int]:
    """Weight and leading digit of ``d`` in PostgreSQL's base-10000 form."""
    d = abs(d)
    if d == 0:
        return 0, 0
    weight = d.adjusted() // DEC_DIGITS
    first = int(_CTX.divide(d, Decimal(10000) ** weight))
    return weight, first


def select_div_scale(a: Decimal, b: Decimal) -> int:
    """``select_div_scale`` from PostgreSQL's numeric.c."""
    weight1, first1 = _weight_and_first_digit(a)
    weight2, first2 = _weight_and_first_digit(b)
    qweight = weight1 - weight2
    if first1 <= first2:
        qweight -= 1
    rscale = NUMERIC_MIN_SIG_DIGITS - qweight * DEC_DIGITS
    rscale = max(rscale, dscale(a), dscale(b), 0)
    return min(rscale, NUMERIC_MAX_DISPLAY_SCALE)


def _round_half_away(fraction: Fraction) -> int:
    sign = -1 if fraction < 0 else 1
    magnitude = abs(fraction)
    whole, remainder = divmod(magnitude.numerator, magnitude.denominator)
    if 2 * remainder >= magnitude.denominator:
        whole += 1
    return sign * whole


def div(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
    """Numeric division, exactly as PostgreSQL rounds it."""
    if a is None or b is None:
        return None
    a, b = num(a), num(b)
    if b == 0:
        raise PgError("division by zero")
    rscale = select_div_scale(a, b)
    scaled = Fraction(a) / Fraction(b) * (10 ** rscale)
    return Decimal(_round_half_away(scaled)).scaleb(-rscale)


def ceiling(value: Optional[Decimal]) -> Optional[Decimal]:
    if value is None:
        return None
    return num(value).to_integral_value(rounding=ROUND_CEILING)


# --- three-valued logic -----------------------------------------------------

def sql_not(value: Optional[bool]) -> Optional[bool]:
    return None if value is None else not value


def sql_and(a: Optional[bool], b: Optional[bool]) -> Optional[bool]:
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def upper(value: Optional[str]) -> Optional[str]:
    return None if value is None else value.upper()


def lower(value: Optional[str]) -> Optional[str]:
    return None if value is None else value.lower()


# --- CAST(:param AS type) as psycopg 3 sends Python values -----------------

_TRUE = {"t", "true", "y", "yes", "on", "1"}
_FALSE = {"f", "false", "n", "no", "off", "0"}


def cast_varchar(value: Any) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def cast_bool(value: Any) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise PgError(f'invalid input syntax for type boolean: "{value}"')


def cast_int(value: Any, bits: int = 32) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        result = int(value)
    elif isinstance(value, int):
        result = value
    elif isinstance(value, float):
        result = round(value)  # float8 -> int uses rint(): half to even
    elif isinstance(value, Decimal):
        result = int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    elif isinstance(value, str):
        text = value.strip()
        body = text[1:] if text[:1] in "+-" else text
        if not body.isdigit():
            raise PgError(f'invalid input syntax for type integer: "{value}"')
        result = int(text)
    else:
        raise PgError(f"cannot cast {type(value).__name__} to integer")
    limit = 2 ** (bits - 1)
    if not -limit <= result < limit:
        raise PgError("integer out of range")
    return result


def cast_numeric(value: Any) -> Optional[Decimal]:
    return num(value)
