from __future__ import annotations

import datetime
import io
import json
import unittest
from unittest.mock import patch

from host.runtime.core import aws_sigv4
from host.runtime.core.aws_sigv4 import sign_post
from host.runtime.root_helpers import aws_account

ACCESS_KEY_ID = "AKIAEXAMPLEKEY000001"
SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
SIGNING_TIME = datetime.datetime(2026, 7, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)


class SigV4Tests(unittest.TestCase):
    """The two pinned Authorization vectors were cross-generated with botocore
    1.43.50 (SigV4Auth) for the exact same inputs, so the hand-rolled signer is
    verified against AWS's own client implementation, not against itself."""

    def test_sts_shaped_request_matches_botocore(self) -> None:
        signed = sign_post(
            host="sts.us-east-1.amazonaws.com",
            region="us-east-1",
            service="sts",
            access_key_id=ACCESS_KEY_ID,
            secret_access_key=SECRET_ACCESS_KEY,
            body=b"Action=GetCallerIdentity&Version=2011-06-15",
            content_type="application/x-www-form-urlencoded",
            extra_headers={"accept": "application/json"},
            now=SIGNING_TIME,
        )
        self.assertEqual(signed.url, "https://sts.us-east-1.amazonaws.com/")
        self.assertEqual(signed.headers["x-amz-date"], "20260717T120000Z")
        self.assertEqual(
            signed.headers["authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLEKEY000001/20260717/us-east-1/sts/aws4_request, "
            "SignedHeaders=accept;content-type;host;x-amz-date, "
            "Signature=ac957d6e1f9668613f14f1079fe4dadc94548fd8c0c808e8f590b3d43abe6d2d",
        )

    def test_json_target_header_shaped_request_matches_botocore(self) -> None:
        # A second signing shape (amz-json content type plus an x-amz-target
        # header) keeps the signer pinned beyond the STS form-encoded case.
        body = json.dumps(
            {
                "TimePeriod": {"Start": "2026-06-18", "End": "2026-07-18"},
                "Granularity": "DAILY",
                "Metrics": ["UnblendedCost"],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            }
        ).encode()
        signed = sign_post(
            host="ce.us-east-1.amazonaws.com",
            region="us-east-1",
            service="ce",
            access_key_id=ACCESS_KEY_ID,
            secret_access_key=SECRET_ACCESS_KEY,
            body=body,
            content_type="application/x-amz-json-1.1",
            extra_headers={"x-amz-target": "AWSInsightsIndexService.GetCostAndUsage"},
            now=SIGNING_TIME,
        )
        self.assertEqual(
            signed.headers["authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLEKEY000001/20260717/us-east-1/ce/aws4_request, "
            "SignedHeaders=content-type;host;x-amz-date;x-amz-target, "
            "Signature=abb73da331e485248972038eda276f37e40c6e6c075bd13ef23e24280060284a",
        )


class HeaderSignatureTests(unittest.TestCase):
    """The general re-signing path the proxy uses for Bedrock, pinned against
    botocore 1.43.50 (SigV4Auth) vectors: a smithy-shaped Converse request
    (pre-encoded ``%3A`` in the model path, ``x-amz-content-sha256`` header)
    and an httpx-shaped invoke request (raw colon path, no content-sha
    header). The double-encoding rule (a wire ``%3A`` canonicalizes to
    ``%253A``) and the raw-colon encoding are both exercised."""

    BODY = json.dumps({"messages": [{"role": "user", "content": [{"text": "hi"}]}]}).encode()
    BODY_HASH = "23aab397f4d1f51b8846a1cea8ead51c4766038fe8b80730645a50f05af4d41e"
    HOST = "bedrock-runtime.us-east-1.amazonaws.com"
    AMZ_DATE = "20260717T162253Z"
    VECTOR_KEY = "AKIATRUSTYCLAWBEDROK"
    VECTOR_SECRET = "trustyclaw-proxy-signs-this-request"

    def signature(self, *, path: str, signed: tuple[str, ...], key: str, secret: str, extra: list[tuple[str, str]]) -> str:
        headers = [("content-type", "application/json"), ("host", self.HOST), ("x-amz-date", self.AMZ_DATE), *extra]
        _authorization, signature = aws_sigv4.header_signature(
            method="POST", path=path, query="", headers=headers,
            signed_headers=signed, payload_hash=self.BODY_HASH,
            amz_date=self.AMZ_DATE, date_stamp=self.AMZ_DATE[:8],
            region="us-east-1", service="bedrock",
            access_key_id=key, secret_access_key=secret,
        )
        return signature

    def test_smithy_shaped_converse_request_matches_botocore(self) -> None:
        self.assertEqual(
            self.signature(
                path="/model/deepseek.v3.2/converse-stream",
                signed=("content-type", "host", "x-amz-content-sha256", "x-amz-date"),
                key=self.VECTOR_KEY, secret=self.VECTOR_SECRET,
                extra=[("x-amz-content-sha256", self.BODY_HASH)],
            ),
            "fab6ca44eff3b5419e60e6f671b9534401445ec6bce132f4b6722f28f3d6565a",
        )

    def test_request_without_content_hash_header_matches_botocore(self) -> None:
        self.assertEqual(
            self.signature(
                path="/model/qwen.qwen3-coder-next/converse",
                signed=("content-type", "host", "x-amz-date"),
                key=self.VECTOR_KEY, secret=self.VECTOR_SECRET,
                extra=[],
            ),
            "d0ab79dc56cf7cb6a6c9f2c88ea92bc06132420bc34e2a17cd36775a9cf919a0",
        )

    def test_resigning_with_the_real_key_matches_botocore(self) -> None:
        self.assertEqual(
            self.signature(
                path="/model/deepseek.v3.2/converse-stream",
                signed=("content-type", "host", "x-amz-content-sha256", "x-amz-date"),
                key="AKIAOPERATORKEY00001", secret=SECRET_ACCESS_KEY,
                extra=[("x-amz-content-sha256", self.BODY_HASH)],
            ),
            "5fda126666a20bc3af97f99c0fcaee1be3f57a3415f939164ed6a23a832517cd",
        )

    def test_parse_authorization_round_trips(self) -> None:
        parsed = aws_sigv4.parse_authorization(
            "AWS4-HMAC-SHA256 Credential=AKIATRUSTYCLAWBEDROK/20260717/us-east-1/bedrock/aws4_request, "
            "SignedHeaders=content-type;host;x-amz-date, Signature=" + "a" * 64
        )
        assert parsed is not None
        self.assertEqual(parsed.access_key_id, "AKIATRUSTYCLAWBEDROK")
        self.assertEqual(parsed.date_stamp, "20260717")
        self.assertEqual(parsed.region, "us-east-1")
        self.assertEqual(parsed.service, "bedrock")
        self.assertEqual(parsed.signed_headers, ("content-type", "host", "x-amz-date"))
        self.assertEqual(parsed.signature, "a" * 64)
        for value in (
            "",
            "Bearer token",
            "AWS4-HMAC-SHA256 Credential=AKIA/20260717/us-east-1/bedrock/oops, SignedHeaders=host, Signature=" + "a" * 64,
            "AWS4-HMAC-SHA256 Credential=AKIA/20260717/us-east-1/bedrock/aws4_request, SignedHeaders=host, Signature=short",
        ):
            self.assertIsNone(aws_sigv4.parse_authorization(value), value)

    def test_payload_hash_prefers_the_content_sha_header(self) -> None:
        self.assertEqual(
            aws_sigv4.payload_hash_for([("X-Amz-Content-Sha256", "cafe" * 16)], self.BODY), "cafe" * 16
        )
        self.assertEqual(aws_sigv4.payload_hash_for([], self.BODY), self.BODY_HASH)


class AwsAccountHelperTests(unittest.TestCase):
    def credential_env(self, access_key_id: str, secret_access_key: str):  # type: ignore[no-untyped-def]
        return patch.dict(
            "os.environ",
            {
                aws_account.ACCESS_KEY_ID_ENV: access_key_id,
                aws_account.SECRET_ACCESS_KEY_ENV: secret_access_key,
            },
        )

    def test_read_credentials_reads_the_helper_environment(self) -> None:
        with self.credential_env(ACCESS_KEY_ID, SECRET_ACCESS_KEY):
            self.assertEqual(aws_account._read_credentials(), (ACCESS_KEY_ID, SECRET_ACCESS_KEY))

    def test_read_credentials_rejects_empty_environment(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit) as caught:
                with patch.object(aws_account.sys, "stderr", io.StringIO()) as stderr:
                    aws_account._read_credentials()
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("no usable AWS credentials", stderr.getvalue())

    def test_read_credentials_rejects_malformed_key(self) -> None:
        with self.credential_env("lowercase-bad", SECRET_ACCESS_KEY):
            with self.assertRaises(SystemExit):
                with patch.object(aws_account.sys, "stderr", io.StringIO()):
                    aws_account._read_credentials()

    def test_get_caller_identity_parses_sts_response(self) -> None:
        response = b"""<?xml version="1.0" encoding="UTF-8"?>
<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <Arn>arn:aws:iam::123456789012:user/hermes-bedrock</Arn>
    <UserId>AIDAEXAMPLE</UserId>
    <Account>123456789012</Account>
  </GetCallerIdentityResult>
</GetCallerIdentityResponse>"""
        with patch.object(aws_account, "_post_raw", return_value=response) as post:
            self.assertEqual(
                aws_account._get_caller_identity(ACCESS_KEY_ID, SECRET_ACCESS_KEY),
                {
                    "account_id": "123456789012",
                    "arn": "arn:aws:iam::123456789012:user/hermes-bedrock",
                    "user_id": "AIDAEXAMPLE",
                },
            )
        self.assertEqual(post.call_args.kwargs["extra_headers"], {})

    def test_get_caller_identity_rejects_malformed_account(self) -> None:
        response = b"""<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult><Account>evil</Account><Arn>arn:x</Arn></GetCallerIdentityResult>
</GetCallerIdentityResponse>"""
        with patch.object(aws_account, "_post_raw", return_value=response):
            with self.assertRaises(SystemExit):
                with patch.object(aws_account.sys, "stderr", io.StringIO()):
                    aws_account._get_caller_identity(ACCESS_KEY_ID, SECRET_ACCESS_KEY)

    def test_get_caller_identity_rejects_non_xml_response(self) -> None:
        with patch.object(aws_account, "_post_raw", return_value=b'{"Account":"123456789012"}'):
            with self.assertRaises(SystemExit):
                with patch.object(aws_account.sys, "stderr", io.StringIO()) as stderr:
                    aws_account._get_caller_identity(ACCESS_KEY_ID, SECRET_ACCESS_KEY)
        self.assertIn("not XML", stderr.getvalue())

    def test_main_rejects_removed_spend_mode(self) -> None:
        # Spend deliberately has no helper mode: cost is metered live at the
        # proxy. Any argument other than --attest is a usage error.
        with patch.object(aws_account.sys, "argv", ["read-aws-account", "--spend"]):
            with self.assertRaises(SystemExit) as caught:
                with patch.object(aws_account.sys, "stderr", io.StringIO()) as stderr:
                    aws_account.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("usage: read-aws-account --attest", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
