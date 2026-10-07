"""Site adapters.

Adapters layer site knowledge on top of the generic automation core:
 - richer SiteConfig (auth cookies, login/verification patterns, probes)
 - recipes: named, parameterised scraping jobs built on the generic
   ``scrape.urls`` / ``scrape.search`` job types

The generic core never requires an adapter - unknown sites work through
runtime-registered site configs and the generic jobs. Adapters exist for
sites we scrape repeatedly and know well.
"""
from typing import Dict


class Adapter:
    name = "base"
    domains = []

    def site_config(self) -> dict:
        return {
            "name": self.name,
            "domains": self.domains,
            "auth_cookies": [],
            "login_path_patterns": ["*login*", "*signin*"],
            "verification_path_patterns": ["*captcha*", "*verify*"],
        }

    def recipes(self) -> Dict[str, dict]:
        """{recipe_name: {description, job_type, params_builder(query)}}"""
        return {}


REGISTRY: Dict[str, Adapter] = {}


def register(adapter_cls):
    inst = adapter_cls()
    REGISTRY[inst.name] = inst
    return adapter_cls


def all_adapters() -> dict:
    out = {}
    for name, adapter in REGISTRY.items():
        out[name] = {
            "domains": adapter.domains,
            "site_config": adapter.site_config(),
            "recipes": {k: {"description": v.get("description", ""),
                            "example_params": v.get("example_params", {})}
                        for k, v in adapter.recipes().items()},
        }
    return out


def find_adapter_for_domain(domain: str) -> Adapter:
    d = (domain or "").lstrip(".").lower()
    for adapter in REGISTRY.values():
        for dom in adapter.domains:
            if d == dom or d.endswith("." + dom):
                return adapter
    return None


def build_recipe_job(adapter_name: str, recipe: str, query: dict) -> dict:
    """Returns {job_type, params} for an adapter recipe invocation."""
    adapter = REGISTRY.get(adapter_name)
    if not adapter:
        raise ValueError(f"unknown adapter {adapter_name}")
    recipes = adapter.recipes()
    if recipe not in recipes:
        raise ValueError(f"unknown recipe {recipe} on {adapter_name}")
    spec = recipes[recipe]
    params = spec["params_builder"](query or {})
    return {"job_type": spec["job_type"], "params": params}
