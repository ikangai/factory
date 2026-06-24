"""Executive-summary reporting for the harness factory.

A small, read-only layer that turns the blackboard + filesystem state of an
(autonomous) run into a short, plain-language presentation for the human at the
09:00 update: DISCOVERIES, DECISIONS, PROPOSED NEXT STEPS.

It NEVER writes to the store, NEVER promotes, and falls back to a deterministic
templated summary if the LLM call fails — so it can never crash a loop.
"""
from .summary import generate_executive_summary

__all__ = ["generate_executive_summary"]
