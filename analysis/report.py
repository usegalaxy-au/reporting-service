"""Assemble findings.md and a flood chart from the analysis CSVs."""

import csv
import logging
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

logger = logging.getLogger(__name__)

TOP_N = 12
MONTHS_LIMIT = 36
CHART_FILENAME = 'requests_per_minute.png'
REPORT_FILENAME = 'findings.md'


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline='') as fh:
        return list(csv.DictReader(fh))


def _table(rows: list[dict], columns: list[str], limit: int = TOP_N) -> str:
    if not rows:
        return '_(no data)_\n'
    lines = ['| ' + ' | '.join(columns) + ' |',
             '|' + '---|' * len(columns)]
    for row in rows[:limit]:
        lines.append(
            '| ' + ' | '.join(str(row.get(c, '')) for c in columns) + ' |')
    return '\n'.join(lines) + '\n'


def _chart(out_dir: Path) -> Path | None:
    rows = _rows(out_dir / 'minute_stats.csv')
    if not rows:
        return None
    minutes = [r['minute'] for r in rows]
    fig, ax = plt.subplots(figsize=(11, 3.5))
    ax.plot(minutes, [int(r['malicious']) for r in rows], lw=0.8,
            color='tab:red', label='malicious requests / min')
    ax.plot(minutes, [int(r['status_502']) for r in rows], lw=0.8,
            color='tab:blue', label='502 / min')
    ax.set_xticks(minutes[::max(1, len(minutes) // 8)])
    ax.tick_params(axis='x', labelrotation=30, labelsize=7)
    ax.set_ylabel('requests per minute')
    ax.legend(loc='upper right')
    fig.tight_layout()
    path = out_dir / CHART_FILENAME
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def write(out_dir: Path) -> Path:
    chart = _chart(out_dir)
    spam = _rows(out_dir / 'spam_ranges.csv')
    mixed = [r for r in _rows(out_dir / 'mixed_ranges.csv')
             if r['level'] == 'subnet24']
    parts = [
        f'# Nginx spam analysis: {out_dir.name}\n',
        '## Malicious patterns (Q2)\n',
        _table(_rows(out_dir / 'patterns.csv'),
               ['category', 'requests', 'ips', 'pct_404', 'pct_2xx',
                'pct_3xx', 'baseline_ips', 'example_target']),
        '## Per-minute error rate vs. genuine users\n',
        _table(_rows(out_dir / 'error_rate_thresholds.csv'),
               ['win', 'threshold', 'ips_over', 'baseline_ips_over',
                'ips_with_scanner_hits'], limit=20),
        '## Spam-only ranges (Q1)\n',
        '### /24\n',
        _table([r for r in spam if r['level'] == 'subnet24'],
               ['range', 'malicious', 'ips', 'days_active',
                'malicious_fraction']),
        '### /16\n',
        _table([r for r in spam if r['level'] == 'subnet16'],
               ['range', 'malicious', 'ips', 'days_active',
                'malicious_fraction']),
        '## Mixed /24 ranges, do not block by range (Q3)\n',
        _table(mixed, ['range', 'malicious', 'good', 'ips',
                       'malicious_ips', 'baseline_ips']),
        '## Allowlist candidates (Q3)\n',
        _table(_rows(out_dir / 'allowlist_candidates.csv'),
               ['ip', 'requests', 'days', 'reason']),
        '## Backtest of the generated fail2ban jails\n',
        _table(_rows(out_dir / 'backtest_summary.csv'),
               ['jail', 'bans', 'banned_ips', 'blocked_malicious',
                'blocked_legit_api', 'malicious_total']),
        '### By month (all jails combined)\n',
        _table([r for r in _rows(out_dir / 'backtest_by_month.csv')
                if r['jail'] == 'ALL (combined)'],
               ['month', 'bans', 'banned_ips', 'baseline_ips_banned',
                'blocked_malicious', 'malicious_total',
                'pct_malicious_blocked', 'blocked_legit_api'],
               limit=MONTHS_LIMIT),
        '### Repeat offenders (banned in 2+ weeks: recidive candidates)\n',
        f'{len(_rows(out_dir / "backtest_repeat_offenders.csv"))} IPs. '
        'Top:\n\n',
        _table(_rows(out_dir / 'backtest_repeat_offenders.csv'),
               ['ip', 'subnet24', 'bans', 'weeks_banned', 'jails']),
    ]
    if chart:
        parts.insert(1, f'![requests per minute]({chart.name})\n')
    path = out_dir / REPORT_FILENAME
    path.write_text('\n'.join(parts))
    logger.info('Wrote %s', path)
    return path
