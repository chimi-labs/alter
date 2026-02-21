"""ORM code generators.

Use ``get_generator(orm)`` to obtain the correct backend.
"""

from alter.generators.base import BaseGenerator, get_generator

__all__ = ["BaseGenerator", "get_generator"]
