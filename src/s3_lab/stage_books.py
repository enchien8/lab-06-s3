#!/usr/bin/env python3
"""Download Project Gutenberg books and publish them in an S3 bucket."""

import argparse
import json
import logging
import os
import sys
import time
from urllib.parse import urlparse

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError

AWS_REGION = "us-east-1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)

# Use the Learner Lab credentials configured by the AWS CLI provider chain.
s3 = boto3.client("s3", region_name=AWS_REGION)


def parse_args() -> tuple[str, str, str]:
    """Parse the book list, index page, and destination bucket arguments."""
    parser = argparse.ArgumentParser(
        description="Download Project Gutenberg books and stage them in S3."
    )
    parser.add_argument("list_file", help="text file containing one book URL per line")
    parser.add_argument("index_path", help="local HTML index page to upload")
    parser.add_argument("bucket", help="name of the public S3 bucket")
    arguments = parser.parse_args()
    LOGGER.info("Preparing to stage books from %s in bucket %s", arguments.list_file, arguments.bucket)
    return arguments.list_file, arguments.index_path, arguments.bucket


def download_books(list_file: str, dest_dir: str) -> list[str]:
    """Download URLs from a list, reusing existing files and returning local paths."""
    try:
        with open(list_file, encoding="utf-8") as url_file:
            urls = [line.strip() for line in url_file if line.strip()]
    except OSError as error:
        LOGGER.error("Could not read book list %s: %s", list_file, error)
        return []

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as error:
        LOGGER.error("Could not create download directory %s: %s", dest_dir, error)
        return []

    local_paths: list[str] = []
    for index, url in enumerate(urls):
        filename = os.path.basename(urlparse(url).path)
        if not filename:
            LOGGER.error("Skipping URL without a file name: %s", url)
            continue

        local_path = os.path.join(dest_dir, filename)
        if os.path.exists(local_path):
            LOGGER.info("Already downloaded; skipping %s", local_path)
            local_paths.append(local_path)
            continue

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            with open(local_path, "wb") as book_file:
                book_file.write(response.content)
            local_paths.append(local_path)
            LOGGER.info("Downloaded %s", local_path)
        except requests.RequestException as error:
            LOGGER.error("Error downloading %s: %s", url, error)
        except OSError as error:
            LOGGER.error("Could not save %s: %s", local_path, error)
        finally:
            # Leave a pause between requests to avoid overloading Gutenberg.
            if index < len(urls) - 1:
                time.sleep(1)

    LOGGER.info("Found %d book files ready to upload", len(local_paths))
    return local_paths


def create_bucket(bucket: str) -> bool:
    """Ensure the named bucket exists and is accessible to this AWS account."""
    try:
        s3.head_bucket(Bucket=bucket)
        LOGGER.info("Bucket %s already exists and is accessible", bucket)
        return True
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code", "Unknown")
        status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status_code != 404 and error_code not in {"404", "NoSuchBucket", "NotFound"}:
            LOGGER.error("Could not check bucket %s (%s)", bucket, error_code)
            return False
    except BotoCoreError as error:
        LOGGER.error("Could not check bucket %s: %s", bucket, error)
        return False

    try:
        # us-east-1 is the one region that does not use LocationConstraint.
        s3.create_bucket(Bucket=bucket)
        LOGGER.info("Created bucket %s in %s", bucket, AWS_REGION)
        return True
    except (BotoCoreError, ClientError) as error:
        LOGGER.error("Could not create bucket %s: %s", bucket, error)
        return False


def make_public(bucket: str) -> bool:
    """Allow anonymous reads of objects in the bucket through a bucket policy."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/*",
            }
        ],
    }
    try:
        # S3 rejects a public policy while this bucket-level block is enabled.
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
        s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
        LOGGER.info("Enabled public object reads for bucket %s", bucket)
        return True
    except (BotoCoreError, ClientError) as error:
        LOGGER.error("Could not make bucket %s public: %s", bucket, error)
        return False


def upload_files(bucket: str, file_paths: list[str]) -> int:
    """Upload book files at the bucket root and return the successful upload count."""
    uploaded_count = 0
    for file_path in file_paths:
        if not os.path.isfile(file_path):
            LOGGER.error("Book file does not exist: %s", file_path)
            continue

        key = os.path.basename(file_path)
        try:
            s3.upload_file(
                file_path,
                bucket,
                key,
                ExtraArgs={"ContentType": "text/plain; charset=utf-8"},
            )
            uploaded_count += 1
            LOGGER.info("Uploaded %s to s3://%s/%s", file_path, bucket, key)
        except (BotoCoreError, ClientError, OSError) as error:
            LOGGER.error("Could not upload %s: %s", file_path, error)

    LOGGER.info("Uploaded %d of %d book files", uploaded_count, len(file_paths))
    return uploaded_count


def upload_index(bucket: str, index_path: str) -> bool:
    """Upload the HTML index to the bucket root with an HTML content type."""
    if not os.path.isfile(index_path):
        LOGGER.error("Index page does not exist: %s", index_path)
        return False

    try:
        s3.upload_file(
            index_path,
            bucket,
            "index.html",
            ExtraArgs={"ContentType": "text/html"},
        )
        LOGGER.info("Uploaded %s to s3://%s/index.html", index_path, bucket)
        return True
    except (BotoCoreError, ClientError, OSError) as error:
        LOGGER.error("Could not upload index page %s: %s", index_path, error)
        return False


def main() -> None:
    """Run the book download, bucket setup, and upload workflow."""
    list_file, index_path, bucket = parse_args()
    if not os.path.isfile(list_file):
        LOGGER.error("Book list does not exist: %s", list_file)
        sys.exit(1)
    if not os.path.isfile(index_path):
        LOGGER.error("Index page does not exist: %s", index_path)
        sys.exit(1)

    file_paths = download_books(list_file, "books")
    if not file_paths:
        LOGGER.error("No books are available to upload; stopping.")
        sys.exit(1)
    if not create_bucket(bucket):
        LOGGER.error("Bucket setup failed; stopping.")
        sys.exit(1)
    if not make_public(bucket):
        LOGGER.error("Public access setup failed; stopping.")
        sys.exit(1)

    uploaded_count = upload_files(bucket, file_paths)
    index_uploaded = upload_index(bucket, index_path)
    if uploaded_count != len(file_paths) or not index_uploaded:
        LOGGER.error("Staging did not finish successfully; review the upload errors above.")
        sys.exit(1)

    LOGGER.info("Successfully staged the book collection.")
    LOGGER.info("Public index: https://%s.s3.amazonaws.com/index.html", bucket)


if __name__ == "__main__":
    main()
