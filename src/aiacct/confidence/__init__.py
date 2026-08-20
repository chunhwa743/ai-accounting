"""Scoring how sure we are, and routing accordingly."""

from .scorer import Routing, Score, Signals, compute_confidence, is_opaque, route_allocation

__all__ = [
    "Routing",
    "Score",
    "Signals",
    "compute_confidence",
    "is_opaque",
    "route_allocation",
]
