"""Shared catalogue of request patterns used to classify nginx traffic.

Every regex here is RE2-compatible (no lookaround/backreferences) so the
same definition runs in DuckDB (``regexp_matches``) and in Python
(``re``). Patterns are matched against the request *target*
(path + query string) unless noted otherwise.
"""

import re

# Paths which can never exist on a Galaxy server. Matched on the target.
SCANNER_PATH_PATTERNS = {
    'php': r'(?i)\.php\d?($|[/?&])',
    'wordpress': (
        r'(?i)/(wp-(admin|content|includes|login|json|config|cron)'
        r'|wordpress|xmlrpc)'),
    'dotfile': (
        r'(?i)/\.(git|env|aws|svn|hg|ssh|docker|htaccess|htpasswd|ds_store'
        r'|npmrc|bash_history|vscode|idea)'),
    'admin_panel': (
        r'(?i)/(phpmyadmin|pma|adminer|cgi-bin|boaform|hnap1|actuator'
        r'|vendor/phpunit|solr|jenkins|manager/html|server-status|telescope'
        r'|owa/auth|ecp/|remote/login|evox|nmaplowercheck)'),
    'server_script': r'(?i)\.(aspx?|jsp|jspx|cgi|cfm|do|action)($|[/?&])',
    'traversal': r'(?i)(\.\./|%2e%2e|%252e|\.\.%2f)',
    'injection': (
        r'(?i)(jndi:|%24%7bjndi|<script|%3cscript|union(%20|\+| )select'
        r'|/etc/passwd|base64_decode|%00)'),
    'encoded_dot_file': (
        r'(?i)%2e(env|php|ya?ml|json|bak|txt|ts|ini|sql|conf|cfg'
        r'|properties|old|xml)($|[?&])'),
    'secret_file': (
        r'(?i)/(id_(rsa|dsa|ecdsa|ed25519)|[^/?]*\.(pem|key|p12|pfx|tfstate)'
        r'|[^/?]*(credentials|service[-_]?account[^/?]*|serviceaccountkey'
        r'|firebase[^/?]*|secrets?)\.(json|ya?ml)|rclone\.conf|\.boto'
        r'|\.s3cfg|docker-compose\.ya?ml|dockerfile)($|\?)'),
    'misc_probe': r'(?i)^/(containers/json|ipfs/|ip($|\?))',
    'archive_probe': (
        r'(?i)^/[^/?]*\.(sql|bak|old|zip|tar|tar\.gz|tgz|rar|7z)($|\?)'),
}
# Generic web-app/framework routes that scanners probe for. Lower
# confidence than SCANNER_PATH_PATTERNS (a future Galaxy release could
# add one), so they only count when the response is 4xx.
# /user/login is deliberately absent: Galaxy users hit it.
SCANNER_4XX_PATTERNS = {
    'app_probe': (
        r'(?i)^/(graphql|v\d+/graphql|api/graphql'
        r'|(dist/|build/)?\.vite/manifest\.json|(dist|build)/manifest\.json'
        r'|runtime-config\.js|config\.json|dashboard|portal|workspace|app'
        r'|signin|account|profile|settings|my|manage|console|admin/login'
        r'|auth/login)/?($|\?)'),
}

# One-line explanation per pattern, rendered into the fail2ban filters.
SCANNER_DESCRIPTIONS = {
    'php': 'PHP scripts (e.g. /wp-content/x.php, /1.php); Galaxy has none',
    'wordpress': 'WordPress paths (/wp-admin, /wp-content, /xmlrpc)',
    'dotfile': 'hidden config/VCS files (/.git/config, /.env, /.aws)',
    'admin_panel': (
        'admin panels and known exploit paths (/phpmyadmin, /cgi-bin, '
        '/actuator, /vendor/phpunit)'),
    'server_script': 'other server-side scripts (.asp, .aspx, .jsp, .cgi)',
    'traversal': 'path traversal (../, %2e%2e, double-encoded variants)',
    'injection': (
        'injection payloads (jndi:, <script, UNION SELECT, /etc/passwd)'),
    'encoded_dot_file': (
        'config files requested with an encoded dot '
        '(/api/smtp/smtp%2ejson, keys%2eenv)'),
    'secret_file': (
        'keys and credentials (id_rsa, *.pem, terraform.tfstate, '
        'service-account JSON, docker-compose.yaml)'),
    'misc_probe': 'misc recon (/containers/json, /ipfs/, /ip)',
    'archive_probe': 'backup/archive in the web root (/backup.zip, /db.sql)',
    'app_probe': (
        'generic web-app routes (/graphql, /dashboard, /.vite/manifest.json,'
        ' /admin/login); matched only when the response is 4xx'),
}

BOGUS_API_NAME = 'bogus_api'
MALFORMED_NAME = 'malformed'

# First path segment(s) after /api/ that real Galaxy serves. Used only to
# label traffic as genuine API use in reports: it goes stale with Galaxy
# releases, so nothing malicious or ban-worthy may depend on it.
GALAXY_API_PREFIXES = (
    'authenticate', 'cloud', 'configuration', 'container_resolvers',
    'dataset_collection_element', 'dataset_collections', 'datasets',
    'datatypes', 'display_applications',
    'dynamic_tools', 'entry_points', 'folders', 'ftp_files', 'genomes',
    'groups', 'histories', 'object_stores', 'unprivileged_tools',
    'invocations', 'job_lock', 'jobs', 'libraries',
    'licenses', 'metrics', 'notifications', 'object_store', 'pages',
    'plugins', 'quotas', 'remote_files', 'roles', 'sessions',
    'short_term_storage', 'storage', 'tags', 'tool_data', 'tool_panels',
    'tool_shed_repositories', 'tool_shed', 'tools', 'tours', 'upload',
    'chunk_upload', 'users', 'version', 'visualizations', 'webhooks',
    'whoami', 'workflows', 'workflow_landings', 'tool_landings',
    'item_tags', 'cloud_authz', 'help', 'forms', 'ga4gh', 'toolshed',
    'drs_download', 'oidc', 'citations', 'history_contents', 'lint',
    'tasks', 'proxy', 'file_source_templates', 'file_source_instances',
    'object_store_templates', 'trs_consume', 'trs_search', 'exports', 'docs',
    'events',
)
GALAXY_API_RE = r'^/api/(' + '|'.join(GALAXY_API_PREFIXES) + r')($|[/?])'
API_PREFIX_RE = r'^/api(/|$)'

# Endpoints the Galaxy UI polls continuously while a tab is open.
UI_POLL_PATHS = (
    '/history/current_history_json',
    '/api/notifications/status',
    '/api/entry_points',
)

# Non-/api routes where real users legitimately collect bursts of 4xx
# (e.g. JBrowse plugin assets under a dataset display). Rate-based 4xx
# rules must ignore these.
FLOOD_EXEMPT_PATH_RE = (
    r'^/(datasets/|display_application/|history/|dataset/|context($|[/?])'
    r'|apple-touch-icon|favicon|\.well-known/)')

# User agents of well-known attack tooling (unambiguous).
TOOL_UA_PATTERNS = (
    r'(?i)(zgrab|masscan|nmap|nuclei|sqlmap|nikto|dirbuster|gobuster|wpscan'
    r'|fuzz faster|acunetix|netsparker|havij|jaeles)'
)
# Libraries that are fine on /api but suspicious when scanning the UI.
GENERIC_LIB_UA_PATTERNS = (
    r'(?i)^(go-http-client|python-requests|python-urllib|curl|wget'
    r'|libwww-perl|java/|okhttp|axios|node-fetch|scrapy|python-httpx)'
)

# Crawlers are tagged and excluded from the analysis for now.
CRAWLER_UA_PATTERN = (
    r'(?i)(bot\b|bot/|spider|crawl|slurp|bytespider|baiduspider|applebot'
    r'|googlebot|bingbot|gptbot|claudebot|petalbot|semrush|ahrefs|mj12'
    r'|dotbot|yandex|facebookexternalhit|duckduckgo|amazonbot|ccbot'
    r'|headlesschrome)')
# Our own uptime monitoring.
MONITOR_UA_PATTERN = (
    r'(?i)(CloudWatchSynthetics|UptimeRobot|Pingdom|ELB-Health)')

# Loopback: interactive-tool traffic proxied locally loses its real IP.
LOOPBACK_RE = r'^127\.'
DEFAULT_SELF_HOST = r'usegalaxy\.org\.au'


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def scanner_case_sql(
    target_col: str = 'target',
    status_col: str | None = None,
) -> str:
    """SQL CASE expression returning the first matching scanner name.

    ``SCANNER_4XX_PATTERNS`` are only included when ``status_col`` is
    given, and then only match 4xx responses.
    """
    whens = [
        f'WHEN regexp_matches({target_col}, {_sql_quote(pattern)}) '
        f'THEN {_sql_quote(name)}'
        for name, pattern in SCANNER_PATH_PATTERNS.items()
    ]
    if status_col:
        whens.extend(
            f'WHEN {status_col} BETWEEN 400 AND 499 '
            f'AND regexp_matches({target_col}, {_sql_quote(pattern)}) '
            f'THEN {_sql_quote(name)}'
            for name, pattern in SCANNER_4XX_PATTERNS.items()
        )
    joined = '\n        '.join(whens)
    return f'CASE\n        {joined}\n        ELSE NULL END'


def sql_in(values: tuple[str, ...]) -> str:
    return ', '.join(_sql_quote(v) for v in values)


def scanner_match(target: str, status: int | None = None) -> str | None:
    """Python equivalent of ``scanner_case_sql`` (used by tests)."""
    for name, pattern in SCANNER_PATH_PATTERNS.items():
        if re.search(pattern, target):
            return name
    if status is not None and 400 <= status <= 499:
        for name, pattern in SCANNER_4XX_PATTERNS.items():
            if re.search(pattern, target):
                return name
    return None


def api_kind(path: str, target: str) -> str:
    """Classify a request as 'none', 'legit_api' or 'bogus_api'."""
    if not re.search(API_PREFIX_RE, path):
        return 'none'
    if re.search(GALAXY_API_RE, path) and not scanner_match(target):
        return 'legit_api'
    return BOGUS_API_NAME
