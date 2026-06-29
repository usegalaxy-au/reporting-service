"""Report registry for the unified report.py entrypoint."""

from reports import tools, workflows

REGISTRY = {
    r.name: r for r in [tools.REPORT, workflows.REPORT]
}
