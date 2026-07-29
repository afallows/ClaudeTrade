"""ClaudeTrade -- swing-trading research and decision-support application.

This package produces *research signals*. It is not financial advice, and it
does not place live orders unless the operator separately configures and
explicitly authorises a supported brokerage connection.
"""

from claudetrade.version import CODE_VERSION, __version__

__all__ = ["CODE_VERSION", "__version__"]
