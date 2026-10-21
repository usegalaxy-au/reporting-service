"""S3 client for reading and managing Galaxy log objects.

Objects are stored under a date-partitioned path:
    <S3_PREFIX>YYYY/MM/DD/HHMMSS-<uuid>.log.gz
Each object is a gzipped newline-delimited JSON file.
"""

import gzip
import io
import json
import logging
import os
from datetime import date, timedelta

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

DATE_PATH_LENGTH = 10  # length of YYYY/MM/DD
# Bulk deletes of 1000 keys time out on the Nectar object store.
DELETE_BATCH_SIZE = 200
DELETE_PROGRESS_EVERY = 50  # batches
# Give up rather than hammer an object store that is down.
DELETE_MAX_CONSECUTIVE_FAILURES = 10
CLIENT_READ_TIMEOUT = 300  # seconds
CLIENT_MAX_ATTEMPTS = 5


class S3Storage:
    def __init__(self, prefix: str = None):
        self._client = None
        self.prefix = prefix

    @property
    def endpoint_url(self):
        return os.environ['S3_ENDPOINT_URL']

    @property
    def access_key(self):
        return os.environ['S3_ACCESS_KEY']

    @property
    def secret_key(self):
        return os.environ['S3_SECRET_KEY']

    @property
    def region(self):
        return os.environ['S3_REGION']

    @property
    def bucket(self):
        return os.environ['S3_BUCKET']

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client(
                's3',
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
                config=Config(
                    signature_version='s3v4',
                    read_timeout=CLIENT_READ_TIMEOUT,
                    retries={
                        'max_attempts': CLIENT_MAX_ATTEMPTS,
                        'mode': 'standard',
                    },
                ),
            )
        return self._client

    def list_all_objects(self) -> list[dict]:
        """Return all objects in the bucket."""
        client = self._get_client()
        paginator = client.get_paginator('list_objects_v2')
        objects = []
        for page in paginator.paginate(Bucket=self.bucket):
            objects.extend(page.get('Contents', []))
        return objects

    def delete_objects(
        self,
        keys: list[str],
        batch_size: int = DELETE_BATCH_SIZE,
    ) -> tuple[int, int]:
        """Delete S3 objects in batches; return (deleted, failed) counts.

        A failing batch is logged and skipped so one timeout doesn't
        abort the whole run. After DELETE_MAX_CONSECUTIVE_FAILURES failed
        batches in a row the remaining keys are counted as failed.
        """
        client = self._get_client()
        starts = range(0, len(keys), batch_size)
        deleted = failed = consecutive_failures = 0

        for n, start in enumerate(starts, 1):
            batch = keys[start:start + batch_size]
            try:
                response = client.delete_objects(
                    Bucket=self.bucket,
                    Delete={
                        'Objects': [{'Key': k} for k in batch],
                        'Quiet': True,
                    },
                )
            except (BotoCoreError, ClientError) as e:
                logger.error(
                    "Batch %d/%d failed (%d keys from %s): %s",
                    n, len(starts), len(batch), batch[0], e)
                failed += len(batch)
                consecutive_failures += 1
                if consecutive_failures >= DELETE_MAX_CONSECUTIVE_FAILURES:
                    remaining = len(keys) - start - len(batch)
                    logger.error(
                        "Giving up after %d consecutive failed batches; "
                        "%d object(s) not attempted.",
                        consecutive_failures, remaining)
                    failed += remaining
                    break
                continue

            consecutive_failures = 0
            errors = response.get('Errors', [])
            for error in errors:
                logger.error(
                    "Failed to delete %s: %s %s", error.get('Key'),
                    error.get('Code'), error.get('Message'))
            failed += len(errors)
            deleted += len(batch) - len(errors)
            if n % DELETE_PROGRESS_EVERY == 0 or n == len(starts):
                logger.info(
                    "Deleted %d/%d object(s), %d failed",
                    deleted, len(keys), failed)

        return deleted, failed

    def date_from_key(self, key: str) -> date:
        """Extract the log date from an S3 object key.

        Keys are of the form <S3_PREFIX>YYYY/MM/DD/HHMMSS-<uuid>.log.gz.
        """
        if not key.startswith(self.prefix):
            raise ValueError(
                f"Key does not start with S3_PREFIX '{self.prefix}': {key}")
        date_str = key[len(self.prefix):len(self.prefix) + DATE_PATH_LENGTH]
        return date.fromisoformat(date_str.replace('/', '-'))

    def iter_keys(self, start_date: date, end_date: date):
        """Yield S3 object keys for log files in the date range (inclusive)."""
        client = self._get_client()
        current = start_date
        while current <= end_date:
            date_prefix = self.prefix + current.strftime('%Y/%m/%d/')
            paginator = client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=self.bucket, Prefix=date_prefix)
            for page in pages:
                for obj in page.get('Contents', []):
                    yield obj['Key']
            current += timedelta(days=1)

    def read_object(self, key: str) -> bytes:
        """Return the raw (still gzipped) bytes of a single S3 object."""
        client = self._get_client()
        logger.info("Downloading s3://%s/%s", self.bucket, key)
        response = client.get_object(Bucket=self.bucket, Key=key)
        return response['Body'].read()

    def read_records(self, key: str):
        """Yield parsed JSON records from a single S3 log object."""
        body = self.read_object(key)
        with gzip.open(io.BytesIO(body)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "Skipping malformed JSON line in %s: %s", key, e,
                    )
