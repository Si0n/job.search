from __future__ import annotations

from jobsearch.adapters.base import Adapter
from jobsearch.adapters.djinni import DjinniAdapter
from jobsearch.adapters.dou import DouAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    DjinniAdapter.name: DjinniAdapter,
    DouAdapter.name: DouAdapter,
}


def get(name: str) -> Adapter:
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise LookupError(
            f"no adapter for source '{name}' — browser sources enter via `jobsearch ingest`"
        ) from None
