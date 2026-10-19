import re

import pytest

from analysis import classify, filters

HOST_PLACEHOLDER = '<HOST>'


@pytest.mark.parametrize('target', [
    '/wp-login.php',
    '/x/shell.php?cmd=id',
    '/wp-content/plugins/a/b.php',
    '/.git/config',
    '/foo/.env',
    '/api/smtp/smtp%2ejson',
    '/cgi-bin/',
    '/login.action',
    '/backup.zip',
    '/containers/json',
    '/id_rsa',
    '/ssl/localhost.key',
    '/terraform.tfstate',
    '/serviceAccountKey.json',
    '/secrets.yml',
    '/docker-compose.yaml',
    '/key.json',
    '/gcp-key.json',
    '/sa.json',
    '/config.env',
    '/appsettings.Development.json',
    '/_environment',
    '/phpinfo',
    '/phpinfo.php.save',
    '/index.php~',
    '/icons/%252e%252e/%252e%252e/etc',
    '/datasets/abc/display/?p=1%27%20UNION%20SELECT%20NULL',
])
def test_scanner_targets_match(target):
    assert classify.scanner_match(target)


@pytest.mark.parametrize('target', [
    '/',
    '/history/current_history_json?since=2026-08-01T04:06:22',
    '/api/histories?limit=10',
    '/datasets/abc/display/plugins/RegexSequenceSearch/css/main.css',
    '/.well-known/passkey-endpoints',
    '/static/dist/libs.bundled.js?v=1',
    '/ipython',
    '/apple-touch-icon.png',
    '/admin/users',
    '/visa.json',
    '/login/token.php',
    '/login/token.php?service=moodle_mobile_app',
])
def test_genuine_targets_do_not_match(target):
    assert classify.scanner_match(target) is None


@pytest.mark.parametrize('target,expected', [
    ('/api/histories', 'legit_api'),
    ('/api/unprivileged_tools', 'legit_api'),
    ('/api/trs_search?query=x', 'legit_api'),
    ('/api/tools/fetch', 'legit_api'),
    ('/api/php/config', 'bogus_api'),
    ('/api/.env', 'bogus_api'),
    ('/api/x/y.php', 'bogus_api'),
    ('/api/api/workflows/abc', 'bogus_api'),
    ('/history/current_history_json', 'none'),
    ('/', 'none'),
])
def test_api_kind(target, expected):
    path = target.partition('?')[0]
    assert classify.api_kind(path, target) == expected


def test_legit_api_with_scanner_payload_is_not_legit():
    target = '/api/histories?q=<script>alert(1)</script>'
    assert classify.api_kind('/api/histories', target) == 'bogus_api'


def test_tool_and_crawler_patterns():
    assert re.search(classify.TOOL_UA_PATTERNS, 'Mozilla/5.0 zgrab/0.x')
    assert re.search(classify.TOOL_UA_PATTERNS, 'ivre-masscan/1.3')
    assert not re.search(classify.TOOL_UA_PATTERNS, 'python-httpx/0.28.1')
    assert re.search(classify.CRAWLER_UA_PATTERN, 'Mozilla/5.0 Bytespider')
    assert re.search(classify.MONITOR_UA_PATTERN,
                     'CloudWatchSynthetics/arn:aws:synthetics')


def _log_line(target: str, status: int = 404, ua: str = '-') -> str:
    return (f'198.51.100.7 - - [03/Aug/2026:23:00:03 +0000] '
            f'"GET {target} HTTP/1.1" {status} 169 "-" "{ua}"')


def _matches(regexes, line):
    return any(
        re.search(r.replace(HOST_PLACEHOLDER, r'(\S+)'), line)
        for r in regexes)


@pytest.mark.parametrize('target', [
    '/x.php', '/a/wp-login.php?x=1', '/.env', '/api/smtp/smtp%2ejson',
    '/backup.zip', '/containers/json',
])
def test_generated_scan_regex_matches_scanner_lines(target):
    assert _matches(filters.scan_failregex(), _log_line(target))


@pytest.mark.parametrize('line', [
    _log_line('/api/histories', 200),
    _log_line('/.well-known/passkey-endpoints'),
    _log_line('/ipython'),
    _log_line('/ip'),
    _log_line('/static/a.js', 200, 'Mozilla/5.0 x.php'),
    '198.51.100.7 - - [03/Aug/2026:23:00:03 +0000] "GET /static/a.js '
    'HTTP/1.1" 200 0 "http://x/a.php" "-"',
])
def test_generated_scan_regex_ignores_genuine_lines(line):
    assert not _matches(filters.scan_failregex(), line)


@pytest.mark.parametrize('target', [
    '/graphql', '/v1/graphql', '/dist/.vite/manifest.json', '/dashboard',
    '/admin/login', '/config.json?x=1', '/panel', '/info', '/auth',
    '/chat/config.json',
])
def test_app_probes_match_only_on_4xx(target):
    assert classify.scanner_match(target, 404) == 'app_probe'
    assert classify.scanner_match(target, 200) is None
    assert classify.scanner_match(target) is None
    assert _matches(filters.scan_failregex(), _log_line(target, 404))
    assert not _matches(filters.scan_failregex(), _log_line(target, 200))


@pytest.mark.parametrize('target', [
    '/user/login', '/api/histories', '/datasets/abc/display/x.css',
    '/dashboards', '/static/config.json', '/apple-touch-icon.png',
    '/authnz/oidc/callback',
])
def test_app_probe_ignores_other_4xx(target):
    assert classify.scanner_match(target, 404) is None
    assert not _matches(filters.scan_failregex(), _log_line(target, 404))


def test_malformed_regex_matches_tls_probe():
    line = ('198.51.100.7 - - [03/Aug/2026:23:00:09 +0000] '
            '"\\x16\\x03\\x01\\x00\\xBA" 400 150 "-" "-"')
    assert _matches([filters.MALFORMED_REQUEST], line)
    assert not _matches(
        [filters.MALFORMED_REQUEST], _log_line('/', 200))


def test_checked_in_filter_files_are_up_to_date():
    assert filters.stale_files() == []


def test_every_scanner_pattern_has_a_description():
    names = {*classify.SCANNER_PATH_PATTERNS, *classify.SCANNER_4XX_PATTERNS,
             *classify.SCANNER_EXEMPT_PATTERNS}
    assert names == set(classify.SCANNER_DESCRIPTIONS)


def test_scan_filter_comment_lists_patterns_in_regex_order():
    text = filters.render_filter(
        filters.scan_failregex(), [], filters.scan_description())
    comment, _, body = text.partition('[Definition]')
    names = [*classify.SCANNER_PATH_PATTERNS, *classify.SCANNER_4XX_PATTERNS]
    positions = [comment.index(f' {n} ') for n in names]
    assert positions == sorted(positions)
    assert len(names) == body.count('<HOST>')


def test_rendered_filters_load_with_fail2ban_interpolation(tmp_path):
    # fail2ban treats % as interpolation; the URL-encoded patterns must be
    # escaped as %% and decode back to the exact regexes.
    for name, (fail, ignore) in filters.definitions().items():
        path = tmp_path / f'{name}.conf'
        path.write_text(filters.render_filter(fail, ignore))
        assert filters.load_filter(path) == (fail, ignore), name
    assert any('%' in r for r in filters.scan_failregex())


def test_scan_ignoreregex_exempts_moodle_token_only():
    fail, ignore = filters.scan_failregex(), filters.scan_ignoreregex()
    exempt = _log_line('/login/token.php', 404)
    # It looks like a PHP probe, but the ignoreregex lets it through.
    assert _matches(fail, exempt) and _matches(ignore, exempt)
    for target in ('/login/token.php?x=1',):
        assert _matches(ignore, _log_line(target))
    for target in ('/x/login/token.php', '/wp-login.php', '/token.php'):
        assert not _matches(ignore, _log_line(target)), target
