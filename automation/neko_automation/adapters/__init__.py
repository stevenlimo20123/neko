"""Adapter registry loading."""
from .base import REGISTRY, all_adapters, build_recipe_job, find_adapter_for_domain  # noqa
from . import pinduoduo  # noqa - registers itself

__all__ = ["REGISTRY", "all_adapters", "build_recipe_job",
           "find_adapter_for_domain"]
