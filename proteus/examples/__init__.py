"""Shipped contributor templates and runnable Proteus examples.

Living inside the ``proteus`` package keeps the wheel's top level clean — installing
proteus-evolve must never claim a generic name like ``examples`` in site-packages — while
``python -m proteus.scaffold`` still finds the same templates after a PyPI install as in
a Git checkout.
"""
