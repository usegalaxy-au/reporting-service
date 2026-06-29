"""End-to-end probe for workflow-invocation collection.

Exercises reports.workflows against the live Galaxy database, printing
the InfluxDB line protocol output that would be produced for a given
encoded workflow ID.

Usage:

    # Set TEST_ENCODED_WORKFLOW_ID to a real workflow ID from nginx logs
    #   grep 'workflows/.*/invocations' /var/log/nginx/access.log \
    #       | tail -1
    # and copy the hex string from the URL.

    cd /home/ubuntu/reporting-service
    ./venv/bin/python -m reports.test_workflow_id_mapping
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine

load_dotenv(Path(__file__).parent.parent / '.env')

import os  # noqa: E402

from common.influx import format_line_protocol  # noqa: E402
from reports.workflows import (  # noqa: E402
    WORKFLOW_QUERY,
    REPORT,
    decode_galaxy_id,
    resolve_canonical_id,
)

# =====================================================================
# Set this to a real encoded workflow ID from nginx logs, e.g.
#   grep 'workflows/.*/invocations' /var/log/nginx/access.log \
#       | tail -1
# and copy the hex string from the URL.

TEST_ENCODED_WORKFLOW_ID = '1346aa3b2ee8c0a3'

# =====================================================================

SAMPLE_DOMAIN = 'genome.usegalaxy.org.au'


def main():
    print("=" * 60)
    print("Workflow invocation reporting - end-to-end test")
    print("=" * 60)

    print(f"\n[1] Decoding encoded ID: {TEST_ENCODED_WORKFLOW_ID}")
    try:
        workflow_id = decode_galaxy_id(TEST_ENCODED_WORKFLOW_ID)
    except (ValueError, TypeError) as e:
        print(f"  FAIL: Could not decode ID: {e}")
        sys.exit(1)
    print(f"  Decoded database ID: {workflow_id}")

    print(f"\n[2] Querying Galaxy database for StoredWorkflow {workflow_id}")
    engine = create_engine(os.environ['GALAXY_DATABASE_URL'])
    with engine.connect() as conn:
        result = conn.execute(
            WORKFLOW_QUERY,
            {'id': workflow_id},
        ).fetchone()

    if not result:
        print(f"  FAIL: StoredWorkflow {workflow_id} not found in database")
        sys.exit(1)

    _, name, user_id, uuid, source_metadata, email = result
    print(f"  Workflow name:   {name}")
    print(f"  User ID:         {user_id}")
    print(f"  UUID:            {uuid}")
    print(f"  Email:           {email}")
    print(f"  source_metadata: {json.dumps(source_metadata, indent=4)}")

    print("\n[3] Resolving canonical identity")
    canonical_id, trs_server, trs_version_id = (
        resolve_canonical_id(source_metadata)
    )
    print(f"  canonical_id:   {canonical_id or '(none - local workflow)'}")
    print(f"  trs_server:     {trs_server or '(none)'}")
    print(f"  trs_version_id: {trs_version_id or '(none)'}")

    output = format_line_protocol(
        measurement=REPORT.measurement,
        tags={
            'domain': SAMPLE_DOMAIN,
            'workflow_name': name,
            'canonical_id': canonical_id or name,
            'trs_server': trs_server,
        },
        fields={
            'count': 1.0,
            'workflow_id': workflow_id,
            'user_id': user_id,
            'trs_version_id': trs_version_id,
        },
        timestamp=datetime.now(timezone.utc),
    )

    print("\n[4] InfluxDB line protocol output:")
    print(output)

    print("\n" + "=" * 60)
    print("PASS: All steps completed successfully")
    print("=" * 60)


if __name__ == '__main__':
    main()
