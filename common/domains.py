"""Email-domain -> institution map, refreshed weekly from upstream."""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DOMAINS_URL = (
    'https://raw.githubusercontent.com/usegalaxy-au/galaxy-media-site/'
    'refs/heads/dev/webapp/utils/data/domains.json'
)
DOMAINS_CACHE_TTL = timedelta(days=7)


def load_domain_map(cache_file: Path) -> dict:
    """Load email-domain -> institution map, refreshing weekly from GitHub."""
    needs_refresh = True
    if cache_file.exists():
        mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
        if datetime.now() - mtime < DOMAINS_CACHE_TTL:
            needs_refresh = False

    if needs_refresh:
        try:
            with urllib.request.urlopen(DOMAINS_URL) as resp:
                data = resp.read()
            json.loads(data)  # validate
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(data)
            logger.info("Refreshed domains.json cache")
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            if cache_file.exists():
                logger.warning(
                    "Failed to refresh domains.json, using stale cache: %s",
                    e)
            else:
                logger.error("Failed to fetch domains.json: %s", e)
                return {}

    with cache_file.open() as f:
        return json.load(f)


def lookup_institution(email: str, domain_map: dict) -> str:
    """Resolve an institution name from an email address using domain_map.

    domain_map keys are either '@full.domain' (exact) or '*.parent.tld'
    (suffix wildcard). Exact matches take precedence over wildcards.
    """
    if not email or '@' not in email:
        return ''
    domain = email.rsplit('@', 1)[1].lower()

    exact = domain_map.get('@' + domain)
    if exact:
        return exact

    best_match = ''
    best_len = 0
    for pattern, name in domain_map.items():
        if pattern.startswith('*.'):
            suffix = pattern[1:]
            if domain.endswith(suffix) and len(suffix) > best_len:
                best_match = name
                best_len = len(suffix)
    return best_match
