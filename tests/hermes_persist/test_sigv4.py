"""SigV4 signer against the AUTHORITATIVE AWS Signature Version 4 test-suite vectors.

Source: rhymu8354/aws-sig-v4-test-suite (the canonical community mirror of
aws-sig-v4-test-suite), fetched and cross-checked live from GitHub:
  get-vanilla/                          → no query
  get-vanilla-query-order-key-case/    → query canonicalization + sorting
Both the canonical-request hash AND the final signature are asserted.
"""
import hashlib

from hermes_persist import sigv4

ACCESS = "AKIDEXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def _sign(query, amz="20150830T123600Z"):
    h = {"Host": "example.amazonaws.com", "X-Amz-Date": amz}
    cr, _ = sigv4.canonical_request("GET", "/", query, h, b"")
    cr_hash = hashlib.sha256(cr.encode()).hexdigest()
    signature, signed = sigv4.sign(
        "GET", "/", query, h, b"",
        access_key=ACCESS, secret_key=SECRET,
        region="us-east-1", service="service", amz_date=amz)
    return cr_hash, signature, signed


def test_get_vanilla_authoritative():
    cr_hash, signature, signed = _sign([])
    assert cr_hash == "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63"
    assert signature == "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    assert signed["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")


def test_get_vanilla_query_order_key_case_authoritative():
    cr_hash, signature, _ = _sign([("Param2", "value2"), ("Param1", "value1")])
    assert cr_hash == "816cd5b414d056048ba4f7c5386d6e0533120fb1fcfa93762cf0fc39e2cf19e0"
    assert signature == "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"


def test_deterministic_and_payload_dependent():
    h = {"Host": "abc.r2.cloudflarestorage.com"}
    common = dict(access_key="k", secret_key="s", region="auto", service="s3",
                  amz_date="20260101T000000Z")
    s1, _ = sigv4.sign("PUT", "/bkt/state/dir/a b.json", [], h, b"data", **common)
    s2, _ = sigv4.sign("PUT", "/bkt/state/dir/a b.json", [], h, b"data", **common)
    s3, _ = sigv4.sign("PUT", "/bkt/state/dir/a b.json", [], h, b"data2", **common)
    assert s1 == s2
    assert s1 != s3
