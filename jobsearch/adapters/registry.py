from __future__ import annotations

from jobsearch.adapters.base import Adapter
from jobsearch.adapters.djinni import DjinniAdapter
from jobsearch.adapters.dou import DouAdapter
from jobsearch.adapters.euremotejobs import EuRemoteJobsAdapter
from jobsearch.adapters.jobicy import JobicyAdapter
from jobsearch.adapters.jobspresso import JobspressoAdapter
from jobsearch.adapters.landingjobs import LandingJobsAdapter
from jobsearch.adapters.larajobs import LaraJobsAdapter
from jobsearch.adapters.nofluffjobs import NoFluffJobsAdapter
from jobsearch.adapters.remotive import RemotiveAdapter
from jobsearch.adapters.remoteok import RemoteOkAdapter
from jobsearch.adapters.weworkremotely import WeWorkRemotelyAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    DjinniAdapter.name: DjinniAdapter,
    DouAdapter.name: DouAdapter,
    RemoteOkAdapter.name: RemoteOkAdapter,
    WeWorkRemotelyAdapter.name: WeWorkRemotelyAdapter,
    LaraJobsAdapter.name: LaraJobsAdapter,
    JobicyAdapter.name: JobicyAdapter,
    NoFluffJobsAdapter.name: NoFluffJobsAdapter,
    RemotiveAdapter.name: RemotiveAdapter,
    EuRemoteJobsAdapter.name: EuRemoteJobsAdapter,
    JobspressoAdapter.name: JobspressoAdapter,
    LandingJobsAdapter.name: LandingJobsAdapter,
}


def get(name: str) -> Adapter:
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise LookupError(
            f"no adapter for source '{name}' — browser sources enter via `jobsearch ingest`"
        ) from None
