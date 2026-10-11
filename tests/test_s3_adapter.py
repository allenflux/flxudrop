"""S3 adapter contract checks with a mocked SDK; no network or credentials."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import unittest
from unittest import mock

from fluxdrop_storage import (
    S3Store,
    StorageConfigurationError,
    StorageIntegrityError,
    StorageSettings,
    StorageUnavailable,
)


SDK_AVAILABLE = importlib.util.find_spec("boto3") is not None and importlib.util.find_spec("botocore") is not None


@unittest.skipUnless(SDK_AVAILABLE, "Optional S3 adapter tests require requirements.txt")
class S3AdapterTests(unittest.TestCase):
    def setUp(self):
        self.sdk = mock.Mock()
        self.factory_patch = mock.patch("boto3.client", return_value=self.sdk)
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.settings = StorageSettings(
            enabled=True,
            bucket="private-test-bucket",
            region="ap-northeast-3",
            endpoint="https://s3.ap-northeast-3.amazonaws.com",
            connect_timeout=1.25,
            read_timeout=4.5,
            max_object_bytes=8,
        )
        self.store = S3Store(self.settings)

    def test_constructor_does_not_create_client_or_resolve_credentials(self):
        self.factory.assert_not_called()

    def test_first_operation_lazily_creates_one_client_with_bounded_retries(self):
        self.factory.assert_not_called()
        self.sdk.head_object.return_value = {"ContentLength": 4}
        self.assertEqual(self.store.head("archive/part"), {"ContentLength": 4})
        self.store.head("archive/part")
        self.factory.assert_called_once()
        args, kwargs = self.factory.call_args
        self.assertEqual(args, ("s3",))
        self.assertEqual(kwargs["region_name"], "ap-northeast-3")
        self.assertEqual(kwargs["endpoint_url"], self.settings.endpoint)
        config = kwargs["config"]
        self.assertEqual(config.connect_timeout, 1.25)
        self.assertEqual(config.read_timeout, 4.5)
        self.assertEqual(config.retries, {"mode": "standard", "total_max_attempts": 3})
        self.assertFalse(any(key.startswith("aws_") for key in kwargs))

    def test_put_uses_standard_private_object_with_sha256_checksum(self):
        payload = b"sealed"
        self.store.put("archive/chunk", payload)
        self.sdk.put_object.assert_called_once_with(
            Bucket=self.settings.bucket,
            Key="archive/chunk",
            Body=payload,
            ContentType="application/octet-stream",
            StorageClass="STANDARD",
            ChecksumSHA256=base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii"),
        )

    def test_get_reads_with_limit_and_always_closes_body(self):
        raw = io.BytesIO(b"data")
        body = mock.Mock(wraps=raw)
        self.sdk.get_object.return_value = {"ContentLength": 4, "Body": body}
        self.assertEqual(self.store.get("archive/chunk"), b"data")
        body.read.assert_called_once_with(9)
        body.close.assert_called_once_with()
        self.assertTrue(raw.closed)
        self.sdk.get_object.assert_called_once_with(Bucket=self.settings.bucket, Key="archive/chunk")

    def test_get_rejects_oversized_content_length_before_reading_and_closes(self):
        raw = io.BytesIO(b"012345678")
        body = mock.Mock(wraps=raw)
        self.sdk.get_object.return_value = {"ContentLength": 9, "Body": body}
        with self.assertRaises(StorageIntegrityError):
            self.store.get("archive/chunk")
        body.read.assert_not_called()
        self.assertTrue(raw.closed)

    def test_get_rejects_stream_exceeding_limit_even_without_content_length(self):
        raw = io.BytesIO(b"012345678")
        body = mock.Mock(wraps=raw)
        self.sdk.get_object.return_value = {"Body": body}
        with self.assertRaises(StorageIntegrityError):
            self.store.get("archive/chunk")
        body.read.assert_called_once_with(9)
        self.assertTrue(raw.closed)

    def test_get_closes_body_on_stream_failure_and_sanitizes_error(self):
        body = mock.Mock()
        body.read.side_effect = OSError("sensitive endpoint and request details")
        self.sdk.get_object.return_value = {"Body": body}
        with self.assertRaises(StorageUnavailable) as raised:
            self.store.get("archive/chunk")
        self.assertNotIsInstance(raised.exception, StorageIntegrityError)
        self.assertNotIn("sensitive", str(raised.exception))
        body.close.assert_called_once_with()

    def test_credentials_and_bucket_errors_map_to_configuration_failure(self):
        from botocore.exceptions import ClientError

        for code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "NoSuchBucket"):
            with self.subTest(code=code):
                self.sdk.head_object.side_effect = ClientError(
                    {"Error": {"Code": code, "Message": "sensitive request details"}}, "HeadObject"
                )
                with self.assertRaises(StorageConfigurationError) as raised:
                    self.store.head("archive/chunk")
                self.assertEqual(raised.exception.code, "storage_configuration")
                self.assertNotIn("sensitive", str(raised.exception))

    def test_missing_objects_map_to_integrity_failure(self):
        from botocore.exceptions import ClientError

        for code in ("NoSuchKey", "NotFound", "404"):
            with self.subTest(code=code):
                self.sdk.get_object.side_effect = ClientError({"Error": {"Code": code}}, "GetObject")
                with self.assertRaises(StorageIntegrityError) as raised:
                    self.store.get("archive/chunk")
                self.assertEqual(raised.exception.code, "storage_integrity")

    def test_connection_failure_is_temporary_and_hides_endpoint(self):
        from botocore.exceptions import EndpointConnectionError

        self.sdk.delete_object.side_effect = EndpointConnectionError(endpoint_url="https://private-host.example")
        with self.assertRaises(StorageUnavailable) as raised:
            self.store.delete("archive/chunk")
        self.assertNotIsInstance(raised.exception, StorageIntegrityError)
        self.assertEqual(raised.exception.code, "storage_unavailable")
        self.assertNotIn("private-host", str(raised.exception))

    def test_list_keys_iterates_pages_and_maps_errors_during_iteration(self):
        paginator = self.sdk.get_paginator.return_value
        paginator.paginate.return_value = [{"Contents": [{"Key": "archive/a"}]}, {},
                                           {"Contents": [{"Key": "archive/b"}]}]
        self.assertEqual(list(self.store.list_keys("archive/")), ["archive/a", "archive/b"])
        self.sdk.get_paginator.assert_called_once_with("list_objects_v2")
        paginator.paginate.assert_called_once_with(Bucket=self.settings.bucket, Prefix="archive/")

        def broken_pages():
            yield {"Contents": [{"Key": "archive/a"}]}
            raise OSError("temporary page request failure")

        paginator.paginate.return_value = broken_pages()
        stream = self.store.list_keys("archive/")
        self.assertEqual(next(stream), "archive/a")
        with self.assertRaises(StorageUnavailable):
            next(stream)


if __name__ == "__main__":
    unittest.main()
