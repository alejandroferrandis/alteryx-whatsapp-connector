"""Single source of truth for the connector version.

The build script reads this to name the .yxi and to stamp every Config.xml, so
the version never has to be edited in more than one place.
"""

__version__ = "1.0.0"

#: Bumped independently of ``__version__`` whenever the on-disk archive schema
#: changes in a way that requires a migration. See ``store.Store._migrate``.
SCHEMA_VERSION = 1
