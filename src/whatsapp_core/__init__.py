"""whatsapp_core - the backend-agnostic engine behind the Alteryx WhatsApp connector.

This package knows *nothing* about Alteryx. It can be driven from
the command line (``python -m whatsapp_core.cli``), from tests, or from the two
Alteryx plugins in :mod:`ayx_plugins`. Keeping the engine free of SDK imports is
what makes it testable without Designer installed.

Layering, outermost first::

    ayx_plugins.whatsapp_input / whatsapp_output   Alteryx SDK glue (thin)
    whatsapp_core.runner / sender                  use-case orchestration
    whatsapp_core.store                            local SQLite archive
    whatsapp_core.client                           neonize (Go/whatsmeow) session
    whatsapp_core.profiles / config / jid          plumbing

Nothing above ``client`` imports ``neonize`` directly, so the heavy native
library is loaded only when a WhatsApp connection is actually required.
"""

from .version import __version__

__all__ = ["__version__"]
