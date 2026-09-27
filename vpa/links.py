"""Links from a stored car to its advert, for display and export only.

carsales writes its own links as a bare path:
    /cars/details/2016-ford-falcon-g6e-turbo-fg-x-auto/SSE-AD-20616552/
and that path is what the database holds, unchanged. Put in a page as it
stands, the browser resolves it against the app itself (127.0.0.1) and the
"open" link goes nowhere. So wherever a link leaves the app, on a page or in
the workbook, the site's address goes in front here. The stored value is
never touched.

The archive link is the Internet Archive's list of copies of that same
address. A car that has sold is usually taken down by carsales, and the
archive is the only place an old copy might survive. It may hold none; the
link then says so on the archive's own page.
"""
from __future__ import annotations

import math

# the site each source's bare paths belong to
SITES = {"carsales": "https://www.carsales.com.au"}


def ad_url(url, source=None) -> str | None:
    """The full address of the advert, or None when there is none.

    A full address is returned as it is. A bare path is completed only when
    the source names a known site - a path from anywhere else is not guessed.
    """
    if url is None or (isinstance(url, float) and math.isnan(url)):
        return None
    url = str(url).strip()
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/") and source is not None:
        site = SITES.get(str(source))
        if site:
            return site + url
    return None


def archive_url(url) -> str | None:
    """The Internet Archive's list of saved copies of a full address."""
    return "https://web.archive.org/web/*/" + url if url else None
