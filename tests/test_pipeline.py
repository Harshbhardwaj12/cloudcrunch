"""
End-to-end tests with a mocked S3 (moto) - no AWS account needed.

    pip install -r requirements-dev.txt
    pytest -v
"""

import importlib.util
import io
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT, OUTPUT = "test-input-bucket", "test-output-bucket"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_image(fmt="JPEG", size=(3000, 2000), mode="RGB"):
    img = Image.new(mode, size)
    # gradient + noise-ish pattern so compression is realistic
    px = img.load()
    for x in range(0, size[0], 4):
        for y in range(0, size[1], 4):
            v = ((x * 7) ^ (y * 13)) & 255
            px[x, y] = (v, (x // 12) & 255, (y // 8) & 255) + ((200,) if mode == "RGBA" else ())
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=95) if fmt == "JPEG" else img.save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def aws():
    os.environ.update({
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1", "AWS_REGION": "us-east-1",
        "INPUT_BUCKET": INPUT, "OUTPUT_BUCKET": OUTPUT,
    })
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=INPUT)
        s3.create_bucket(Bucket=OUTPUT)
        processor = load("processor_app", "processor/app.py")
        api = load("api_app", "api/app.py")
        yield s3, processor, api


def s3_event(key):
    return {"Records": [{"s3": {"bucket": {"name": INPUT}, "object": {"key": key}}}]}


def http(method, path, body=None, params=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "body": json.dumps(body) if body else None,
        "pathParameters": params,
    }


# ---------------------------------------------------------------- unit tests
def test_parse_options_defaults_and_validation(aws):
    _, processor, _ = aws
    assert processor.parse_options({}) == {"max_size": 1024, "format": "WEBP", "quality": 75}
    assert processor.parse_options({"max-size": "999", "output-format": "gif", "quality": "abc"}) == \
        {"max_size": 1024, "format": "WEBP", "quality": 75}
    assert processor.parse_options({"Max-Size": "256", "output-format": "JPEG", "quality": "500"}) == \
        {"max_size": 256, "format": "JPEG", "quality": 95}


@pytest.mark.parametrize("src_fmt,mode,out_fmt", [
    ("JPEG", "RGB", "WEBP"), ("PNG", "RGBA", "WEBP"), ("PNG", "RGBA", "JPEG"),
    ("JPEG", "RGB", "PNG"), ("PNG", "P", "WEBP"),
])
def test_process_image_formats(aws, src_fmt, mode, out_fmt):
    _, processor, _ = aws
    data = make_image(src_fmt, (1600, 900), "RGB" if mode == "P" else mode)
    if mode == "P":
        img = Image.open(io.BytesIO(data)).convert("P")
        buf = io.BytesIO(); img.save(buf, "PNG"); data = buf.getvalue()
    out, stats = processor.process_image(data, {"max_size": 512, "format": out_fmt, "quality": 70})
    result = Image.open(io.BytesIO(out))
    assert result.format == out_fmt
    assert max(result.size) == 512
    assert stats["processed_width"] == 512 and stats["processed_height"] == 288  # aspect kept


def test_never_upscales(aws):
    _, processor, _ = aws
    out, stats = processor.process_image(make_image("PNG", (200, 100)), {"max_size": 1024, "format": "WEBP", "quality": 75})
    assert (stats["processed_width"], stats["processed_height"]) == (200, 100)


# ---------------------------------------------------------------- end-to-end
def test_full_pipeline(aws):
    s3, processor, api = aws

    # 1. browser asks API for an upload URL
    res = api.lambda_handler(http("POST", "/upload-url", {
        "filename": "my photo (1).jpg", "contentType": "image/jpeg",
        "maxSize": "1024", "format": "webp", "quality": "75",
    }), None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    job_id, key, fields = body["jobId"], body["key"], body["upload"]["fields"]
    assert key == f"uploads/{job_id}/my_photo__1_.jpg"
    assert fields["x-amz-meta-output-format"] == "webp"

    # 2. status is pending before processing
    pending = json.loads(api.lambda_handler(http("GET", f"/status/{job_id}", params={"jobId": job_id}), None)["body"])
    assert pending["status"] == "pending"

    # 3. simulate the browser upload (same metadata the presigned POST enforces)
    original = make_image("JPEG", (3000, 2000))
    s3.put_object(Bucket=INPUT, Key=key, Body=original, ContentType="image/jpeg", Metadata={
        "max-size": "1024", "output-format": "webp", "quality": "75"})

    # 4. S3 event -> processor Lambda
    out = processor.lambda_handler(s3_event(key.replace(" ", "+")), None)
    r = out["results"][0]
    assert r["status"] == "done"
    assert r["processed_bytes"] < r["original_bytes"]
    assert (r["processed_width"], r["processed_height"]) == (1024, 683)

    processed = s3.get_object(Bucket=OUTPUT, Key=f"processed/{job_id}.webp")
    assert processed["ContentType"] == "image/webp"

    # 5. API now reports done with download links
    done = json.loads(api.lambda_handler(http("GET", f"/status/{job_id}", params={"jobId": job_id}), None)["body"])
    assert done["status"] == "done"
    assert "attachment" in done["downloadUrl"] or "response-content-disposition" in done["downloadUrl"]
    print(f"\n  {r['original_bytes']:,} B -> {r['processed_bytes']:,} B  ({r['reduction_percent']}% smaller, {r['processing_ms']} ms)")


def test_corrupt_file_reports_error(aws):
    s3, processor, api = aws
    key = "uploads/" + "a" * 32 + "/broken.jpg"
    s3.put_object(Bucket=INPUT, Key=key, Body=b"not an image")
    out = processor.lambda_handler(s3_event(key), None)
    assert out["results"][0]["status"] == "error"
    status = json.loads(api.lambda_handler(http("GET", "/status/" + "a" * 32, params={"jobId": "a" * 32}), None)["body"])
    assert status["status"] == "error"


def test_api_rejects_bad_input(aws):
    _, _, api = aws
    bad_type = api.lambda_handler(http("POST", "/upload-url", {"contentType": "application/pdf"}), None)
    assert bad_type["statusCode"] == 400
    bad_opts = api.lambda_handler(http("POST", "/upload-url", {"contentType": "image/png", "maxSize": "9999"}), None)
    assert bad_opts["statusCode"] == 400
    bad_id = api.lambda_handler(http("GET", "/status/../../etc", params={"jobId": "../../etc"}), None)
    assert bad_id["statusCode"] == 400
