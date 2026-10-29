"""AdProduct -- Amazon Ads advertised-product report automation.

Playwright drives the Amazon Ads "Sponsored ads reports" page, builds the
Sponsored Products / Sponsored Brands / Sponsored Display advertised-product
reports for Today or Yesterday, downloads them, and loads them into the MySQL
``Advertised_product`` table with pandas.  No Amazon Ads API.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
