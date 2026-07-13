"""Adapter interface all pipelines implement."""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import date

from ..http import EbbClient
from ..models import CapacityRecord, FlowRecord, NoticeRecord, RateDoc, TariffRate


@dataclass
class FetchResult:
    capacity: list[CapacityRecord] = field(default_factory=list)
    flows: list[FlowRecord] = field(default_factory=list)
    notices: list[NoticeRecord] = field(default_factory=list)
    rate_docs: list[RateDoc] = field(default_factory=list)
    rate_values: list[TariffRate] = field(default_factory=list)
    raw_paths: list[str] = field(default_factory=list)


class PipelineAdapter(ABC):
    """One adapter per pipeline EBB.

    DATASETS maps dataset name -> method name. Each method has signature
    (client, gas_day: date, *, start: date | None, end: date | None) -> FetchResult.
    Date-range support (backfill) is per-dataset; methods that don't support
    ranges ignore start/end.
    """

    name: str = ""
    DATASETS: dict[str, str] = {}
    # datasets that download multi-MB documents; pull-all skips them unless
    # --include-heavy (schedule them weekly — tariff rates change rarely)
    HEAVY_DATASETS: tuple[str, ...] = ()
    # datasets that need headless-browser emulation; pull-all skips them
    # unless --include-browser
    BROWSER_DATASETS: frozenset[str] = frozenset()

    def datasets(self) -> list[str]:
        return list(self.DATASETS)

    def fetch(self, client: EbbClient, dataset: str, gas_day: date,
              start: date | None = None, end: date | None = None) -> FetchResult:
        try:
            method = getattr(self, self.DATASETS[dataset])
        except KeyError:
            raise ValueError(f"{self.name}: unknown dataset {dataset!r}; have {self.datasets()}")
        return method(client, gas_day, start=start, end=end)

    def fetch_notice_bodies(self, client: EbbClient, notices: list[dict]) -> dict[str, str]:
        """Fetch full body text for notices that were stored without one.

        `notices` rows have keys notice_id, category, url, extra. Return
        {notice_id: body_text}. Default: nothing to enrich (adapters whose
        notice feed already carries the body don't override this).
        """
        return {}
