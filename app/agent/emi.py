"""Indicative EMI ranges. Never a loan offer: the rate range and disclaimer come from
business_rules.yaml, and the agent must always show the disclaimer."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.client_config import EmiRules


def monthly_emi(principal: float, annual_rate_pct: float, months: int) -> float:
    """Standard reducing-balance EMI."""
    if principal <= 0:
        return 0.0
    if months <= 0:
        raise ValueError("months must be positive")
    r = annual_rate_pct / 12 / 100
    if r == 0:
        return principal / months
    factor = (1 + r) ** months
    return principal * r * factor / (factor - 1)


def _round_100(x: float) -> int:
    return int(round(x / 100.0)) * 100


@dataclass(frozen=True)
class EmiOption:
    tenure_months: int
    emi_min: int  # at the lowest rate
    emi_max: int  # at the highest rate


@dataclass(frozen=True)
class EmiEstimate:
    price: int
    down_payment: int
    loan_amount: int
    rate_min_pct: float
    rate_max_pct: float
    options: list[EmiOption]


def estimate(price: int, rules: EmiRules, *, down_payment: int | None = None,
             tenure_months: int | None = None) -> EmiEstimate:
    """EMI range for each tenure (or just the one asked for), rounded to ₹100."""
    if price <= 0:
        raise ValueError("price must be positive")
    if down_payment is None:
        down_payment = _round_100(price * rules.default_down_payment_pct / 100)
    down_payment = max(0, min(down_payment, price))
    loan = price - down_payment
    tenures = [tenure_months] if tenure_months else list(rules.tenure_months_options)
    options = [
        EmiOption(t, _round_100(monthly_emi(loan, rules.annual_rate_min_pct, t)),
                  _round_100(monthly_emi(loan, rules.annual_rate_max_pct, t)))
        for t in tenures
    ]
    return EmiEstimate(price, down_payment, loan, rules.annual_rate_min_pct,
                       rules.annual_rate_max_pct, options)
