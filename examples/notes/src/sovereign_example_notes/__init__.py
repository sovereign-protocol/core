"""The smallest application that satisfies Sovereign Core's contract.

This exists so Core's own test suite always has a real, installable
application to run against, rather than only the stubs its unit tests build.
S-Agreement used to serve that purpose; it became a product and moved to its
own repository, so this took over - deliberately small enough to read in one
sitting.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
