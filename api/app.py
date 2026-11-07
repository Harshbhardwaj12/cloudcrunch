"""
Upload / Status API Lambda (behind Amazon API Gateway HTTP API)
---------------------------------------------------------------
The browser never gets AWS credentials. Instead it calls this small API:

  POST /upload-url        -> returns a short-lived *presigned POST* so the browser
                             can upload straight to the input S3 bucket
                             (file type + max 10 MB enforced by S3 itself)
  GET  /status/{jobId}    -> returns "pending" until the processor Lambda has
                             finished, then the stats + presigned download links
"""

import json
import os
import re
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024
URL_EXPIRY_SECONDS = 900

# Regional endpoint + SigV4 avoids redirect/CORS issues with presigned URLs
s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_SIZES = {"256", "512", "1024", "2048"}
ALLOWED_FORMATS = {"webp", "jpeg", "png"}
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def safe_filename(name: str) -> str:
    name = os.path.basename(name or "image")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[-100:] or "image"


def create_upload(event) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"error": "Body must be JSON"})

    content_type = body.get("contentType", "")
    if content_type not in ALLOWED_CONTENT_TYPES:
        return response(400, {"error": "Only JPEG, PNG or WebP images are allowed"})

    max_size = str(body.get("maxSize", "1024"))
    out_format = str(body.get("format", "webp")).lower()
    quality = str(body.get("quality", "75"))
    if max_size not in ALLOWED_SIZES or out_format not in ALLOWED_FORMATS or not quality.isdigit():
        return response(400, {"error": "Invalid processing options"})

    job_id = uuid.uuid4().hex
    key = f"uploads/{job_id}/{safe_filename(body.get('filename'))}"

    # These fields travel with the upload and become S3 object metadata,
    # which is how the processor Lambda knows what the user asked for.
    fields = {
        "Content-Type": content_type,
        "x-amz-meta-max-size": max_size,
        "x-amz-meta-output-format": out_format,
        "x-amz-meta-quality": quality,
    }
    conditions = [{k: v} for k, v in fields.items()]
    conditions.append(["content-length-range", 1, MAX_UPLOAD_BYTES])

    post = s3.generate_presigned_post(
        Bucket=INPUT_BUCKET,
        Key=key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=URL_EXPIRY_SECONDS,
    )
    return response(200, {"jobId": job_id, "key": key, "upload": post})


def get_status(job_id: str) -> dict:
    if not JOB_ID_RE.match(job_id or ""):
        return response(400, {"error": "Invalid job id"})

    try:
        obj = s3.get_object(Bucket=OUTPUT_BUCKET, Key=f"results/{job_id}.json")
    except ClientError as err:
        if err.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return response(200, {"jobId": job_id, "status": "pending"})
        raise

    result = json.loads(obj["Body"].read())
    if result.get("status") == "done":
        out_key = result["output_key"]
        download_name = f"processed-{job_id[:8]}.{out_key.rsplit('.', 1)[-1]}"
        result["previewUrl"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": OUTPUT_BUCKET, "Key": out_key},
            ExpiresIn=URL_EXPIRY_SECONDS,
        )
        result["downloadUrl"] = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": OUTPUT_BUCKET,
                "Key": out_key,
                "ResponseContentDisposition": f'attachment; filename="{download_name}"',
            },
            ExpiresIn=URL_EXPIRY_SECONDS,
        )
    return response(200, result)


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "")

    if method == "POST" and path.endswith("/upload-url"):
        return create_upload(event)
    if method == "GET" and "/status/" in path:
        return get_status((event.get("pathParameters") or {}).get("jobId", ""))
    if method == "GET" and path.endswith("/health"):
        return response(200, {"ok": True, "input": INPUT_BUCKET, "output": OUTPUT_BUCKET})
    return response(404, {"error": "Not found"})
