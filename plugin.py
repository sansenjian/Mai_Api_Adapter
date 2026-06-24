from __future__ import annotations

from .adapter.core import HttpApiAdapterPlugin


def create_plugin() -> HttpApiAdapterPlugin:
    return HttpApiAdapterPlugin()
