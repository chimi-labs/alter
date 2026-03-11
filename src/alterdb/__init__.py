"""Compatibility shim: ``import alterdb`` → ``import alter``.

The PyPI distribution is named *alterdb* but the installed Python package has
always been *alter*.  This shim lets users write either::

    import alterdb          # works
    from alterdb import X   # works
    import alter            # still works – no regression

Both names import from the same underlying ``alter`` package; they share the
same module objects at runtime.
"""
from alter import *  # noqa: F401, F403
