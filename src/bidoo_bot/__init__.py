"""bidoo-bot: manually triggered Gmail -> Bidoo free-bid redeemer.

Layering (imports only ever point downwards)::

    adapters/  gmail, bidoo, telegram      <- talks to the outside world
    container                              <- wires adapters into the core
    application/                           <- the use case, ports only
    parsing/, models/, security, config    <- pure, dependency-free
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
