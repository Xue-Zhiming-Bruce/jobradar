"""Where a job board becomes the postings this app stores: one module per source.

Each adapter is a module that exports `fetch(slug, *, client=None)`, and this
package holds the one mapping from a stored name to that callable. A company row
names its source, `POST /watchlist` probes through it and the tick fetches
through it, both through `SOURCES`; `base.py` holds the vocabulary the two
adapters share. Adding a third source is a module, an entry here and a value the
`company.source` check accepts -- no base class, no subclass per source, no flag.
"""

from app.sources import greenhouse, lever
from app.sources.base import Source

# Every source a `company.source` row may name, mapped to its adapter's own
# `fetch`. The keys are the vocabulary `app.models.Company`'s check constraint
# declares; a name that is not here cannot be stored, and one that is stored is
# resolved to the callable that knows its payload.
SOURCES: dict[str, Source] = {"greenhouse": greenhouse.fetch, "lever": lever.fetch}


def source_named(name: str) -> Source:
    """The adapter a stored source `name` names, or `KeyError` for a name no row may hold."""
    return SOURCES[name]
