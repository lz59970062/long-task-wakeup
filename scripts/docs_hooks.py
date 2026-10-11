"""Use the hosting platform's base URL for canonical and language links."""

import os

from mkdocs.plugins import event_priority


@event_priority(100)
def on_config(config):
    # Run before i18n adds the language prefix. Each locale build starts here.
    for name in (
        "LTC_DOCS_SITE_URL",
        "READTHEDOCS_CANONICAL_URL",
        "CF_PAGES_URL",
        "CI_PAGES_URL",
    ):
        value = os.environ.get(name)
        if value:
            config.site_url = value.rstrip("/") + "/"
            break
    return config
