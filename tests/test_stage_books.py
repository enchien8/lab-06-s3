"""Unit tests for the public-book staging workflow."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from botocore.exceptions import ClientError

from s3_lab import stage_books


class DownloadBooksTests(unittest.TestCase):
    """Test book download behavior without making HTTP requests."""

    def test_downloads_new_books_and_keeps_existing_files(self) -> None:
        """Save URL basenames, skip cached books, and pause between requests."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            list_file = root / "book_urls.txt"
            list_file.write_text(
                "https://www.gutenberg.org/cache/epub/11/pg11.txt\n"
                "\n"
                "https://www.gutenberg.org/cache/epub/12/pg12.txt\n",
                encoding="utf-8",
            )
            books_directory = root / "books"
            books_directory.mkdir()
            (books_directory / "pg12.txt").write_text("cached", encoding="utf-8")
            response = Mock(content=b"new book text")

            with (
                patch.object(stage_books.requests, "get", return_value=response) as get,
                patch.object(stage_books.time, "sleep") as sleep,
            ):
                local_paths = stage_books.download_books(
                    str(list_file), str(books_directory)
                )

            self.assertEqual(
                local_paths,
                [str(books_directory / "pg11.txt"), str(books_directory / "pg12.txt")],
            )
            self.assertEqual((books_directory / "pg11.txt").read_bytes(), b"new book text")
            self.assertEqual((books_directory / "pg12.txt").read_text(), "cached")
            get.assert_called_once_with(
                "https://www.gutenberg.org/cache/epub/11/pg11.txt", timeout=30
            )
            sleep.assert_called_once_with(1)


class S3SetupTests(unittest.TestCase):
    """Test bucket setup and policy calls with a mocked S3 client."""

    def test_create_bucket_creates_missing_us_east_1_bucket(self) -> None:
        """Create a bucket only after a 404 from the existence check."""
        client_error = ClientError(
            {
                "Error": {"Code": "404", "Message": "Not Found"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadBucket",
        )
        client = Mock()
        client.head_bucket.side_effect = client_error

        with patch.object(stage_books, "s3", client):
            self.assertTrue(stage_books.create_bucket("public-example"))

        client.create_bucket.assert_called_once_with(Bucket="public-example")

    def test_create_bucket_reuses_accessible_bucket(self) -> None:
        """Avoid recreating a bucket that is already accessible."""
        client = Mock()

        with patch.object(stage_books, "s3", client):
            self.assertTrue(stage_books.create_bucket("public-example"))

        client.create_bucket.assert_not_called()

    def test_make_public_disables_block_before_attaching_policy(self) -> None:
        """Apply the public access block configuration before the read policy."""
        client = Mock()

        with patch.object(stage_books, "s3", client):
            self.assertTrue(stage_books.make_public("public-example"))

        self.assertEqual(
            client.mock_calls,
            [
                call.put_public_access_block(
                    Bucket="public-example",
                    PublicAccessBlockConfiguration={
                        "BlockPublicAcls": False,
                        "IgnorePublicAcls": False,
                        "BlockPublicPolicy": False,
                        "RestrictPublicBuckets": False,
                    },
                ),
                call.put_bucket_policy(
                    Bucket="public-example",
                    Policy=json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Principal": "*",
                                    "Action": "s3:GetObject",
                                    "Resource": "arn:aws:s3:::public-example/*",
                                }
                            ],
                        }
                    ),
                ),
            ],
        )


class UploadTests(unittest.TestCase):
    """Test upload keys and content types without contacting S3."""

    def test_uploads_books_and_index_with_browser_content_types(self) -> None:
        """Upload books to the bucket root and the page as index.html."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            book_path = Path(temporary_directory) / "pg11.txt"
            index_path = Path(temporary_directory) / "page.html"
            book_path.write_text("book text", encoding="utf-8")
            index_path.write_text("<h1>Books</h1>", encoding="utf-8")
            client = Mock()

            with patch.object(stage_books, "s3", client):
                uploaded_count = stage_books.upload_files(
                    "public-example", [str(book_path)]
                )
                index_uploaded = stage_books.upload_index(
                    "public-example", str(index_path)
                )

        self.assertEqual(uploaded_count, 1)
        self.assertTrue(index_uploaded)
        client.upload_file.assert_has_calls(
            [
                call(
                    str(book_path),
                    "public-example",
                    "pg11.txt",
                    ExtraArgs={"ContentType": "text/plain; charset=utf-8"},
                ),
                call(
                    str(index_path),
                    "public-example",
                    "index.html",
                    ExtraArgs={"ContentType": "text/html"},
                ),
            ]
        )


if __name__ == "__main__":
    unittest.main()
