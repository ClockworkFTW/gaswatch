"""Pipeline adapter registry."""
from __future__ import annotations

from importlib import import_module

from .base import PipelineAdapter

# name -> (module, class). Imported lazily so an optional dependency (e.g.
# playwright for ruby) doesn't break the rest of the CLI.
_REGISTRY = {
    "gtn": ("gaswatch.pipelines.gtn", "GtnAdapter"),
    "ngtl": ("gaswatch.pipelines.ngtl", "NgtlAdapter"),
    "foothills": ("gaswatch.pipelines.foothills", "FoothillsAdapter"),
    "cgt": ("gaswatch.pipelines.cgt", "CgtAdapter"),
    "epng": ("gaswatch.pipelines.epng", "EpngAdapter"),
    "ruby": ("gaswatch.pipelines.ruby", "RubyAdapter"),
    "transwestern": ("gaswatch.pipelines.transwestern", "TranswesternAdapter"),
    "kernriver": ("gaswatch.pipelines.kernriver", "KernRiverAdapter"),
    "nwp": ("gaswatch.pipelines.nwp", "NwpAdapter"),
    "socal": ("gaswatch.pipelines.socal", "SocalAdapter"),
}


def pipeline_names() -> list[str]:
    return list(_REGISTRY)


def get_adapter(name: str) -> PipelineAdapter:
    try:
        module_name, cls_name = _REGISTRY[name.lower()]
    except KeyError:
        raise ValueError(f"unknown pipeline {name!r}; have {pipeline_names()}")
    module = import_module(module_name)
    return getattr(module, cls_name)()
