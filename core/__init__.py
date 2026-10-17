"""Couple Chat — core (non-Django) library package.

Everything in this package is framework-agnostic: the tunables (settings.py),
the TTL state store (store.py) and the Web Push helper (push.py).  The Django
views in the ``chat`` app are thin wrappers over it, which keeps the privacy
contract testable without a web layer.
"""
