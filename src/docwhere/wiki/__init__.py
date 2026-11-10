"""Search a wiki through its own search, and read only the pages it returns.

Nothing is mirrored: each question asks the wiki's search, fetches the few pages it returns,
and cuts them into sections the way local files are, so permissions and freshness stay the
wiki's. These commands are docwhere's only network access, and they talk only to the wiki
URLs the user has added; a redirect elsewhere is refused.

Modules: ``core`` (the shared types), ``net`` (HTTP), ``registry`` (the wikis added and their
clients), ``hits`` (ranking), ``confluence`` (one wiki kind), ``view`` (the reader page) and
``cli`` (the commands). A wiki kind is a class with ``search``, ``page``, ``space_list`` and
``check``; add a module, and its name in ``registry.KINDS`` and ``registry.client``, to
support another.
"""

from docwhere.wiki.core import Page, WikiError, WikiHit, clean, origin, safe_url

__all__ = ["Page", "WikiError", "WikiHit", "clean", "origin", "safe_url"]
