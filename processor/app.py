"""
Image Processor Lambda
----------------------
Triggered automatically by S3 ObjectCreated events on the INPUT bucket.

For every uploaded image it:
  1. Downloads the original from the input bucket
  2. Reads processing options from the object's metadata (size, format, quality)
  3. Resizes (keeping aspect ratio), compresses and converts it with Pillow
  4. Uploads the result to the OUTPUT bucket under  processed/<job_id>.<ext>
  5. Writes a small JSON result file under            results/<job_id>.json
     (the frontend polls this through the API to know when the job is done)

The output bucket has NO trigger, so there is no risk of an infinite loop.
"""

import io
import json
import logging
import os
import time
import urllib.parse

import boto3
from PIL import Image, ImageOps

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "")

ALLOWED_SIZES = {256, 512, 1024, 2048}
ALLOWED_FORMATS = {"webp": "WEBP", "jpeg": "JPEG", "png": "PNG"}
CONTENT_TYPES = {"WEBP": "image/webp", "JPEG": "image/jpeg", "PNG": "image/png"}
EXTENSIONS = {"WEBP": "webp", "JPEG": "jpg", "PNG": "png"}

DEFAULT_SIZE = 1024
DEFAULT_FORMAT = "webp"
DEFAULT_QUALITY = 75


# --------------------------------------------------------------------------- #
#  Pure processing logic (no AWS) – easy to unit-test locally
# --------------------------------------------------------------------------- #
def parse_options(metadata: dict) -> dict:
    """Validate the options the user picked in the web app (sent as S3 metadata)."""
    metadata = {k.lower(): v for k, v in (metadata or {}).items()}

    try:
        size = int(metadata.get("max-size", DEFAULT_SIZE))
    except ValueError:
        size = DEFAULT_SIZE
    if size not in ALLOWED_SIZES:
        size = DEFAULT_SIZE

    fmt = metadata.get("output-format", DEFAULT_FORMAT).lower()
    if fmt not in ALLOWED_FORMATS:
        fmt = DEFAULT_FORMAT

    try:
        quality = int(metadata.get("quality", DEFAULT_QUALITY))
    except ValueError:
        quality = DEFAULT_QUALITY
    quality = max(10, min(95, quality))

    return {"max_size": size, "format": ALLOWED_FORMATS[fmt], "quality": quality}


def process_image(image_bytes: bytes, options: dict) -> tuple[bytes, dict]:
    """Resize + compress + convert. Returns (output_bytes, stats)."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        original_format = img.format
        img = ImageOps.exif_transpose(img)          # respect phone camera rotation
        original_w, original_h = img.size

        # Resize, keeping the aspect ratio; never upscale
        img.thumbnail((options["max_size"], options["max_size"]), Image.LANCZOS)

        fmt = options["format"]
        if fmt == "JPEG" and img.mode not in ("RGB", "L"):
            # JPEG has no transparency – flatten onto white
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif fmt == "WEBP" and img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.mode or img.mode == "P" else "RGB")

        buffer = io.BytesIO()
        save_args = {"format": fmt, "optimize": True}
        if fmt in ("JPEG", "WEBP"):
            save_args["quality"] = options["quality"]
        if fmt == "JPEG":
            save_args["progressive"] = True
        if fmt == "WEBP":
            save_args["method"] = 6                  # best compression
        img.save(buffer, **save_args)

        stats = {
            "original_format": original_format,
            "original_width": original_w,
            "original_height": original_h,
            "processed_width": img.width,
            "processed_height": img.height,
        }
    return buffer.getvalue(), stats


def job_id_from_key(key: str) -> str:
    """uploads/<job_id>/<filename>  ->  <job_id>"""
    parts = key.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "uploads" else parts[-1].rsplit(".", 1)[0]


# --------------------------------------------------------------------------- #
#  Lambda entry point
# --------------------------------------------------------------------------- #
def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        job_id = job_id_from_key(key)
        started = time.time()
        logger.info("New upload: s3://%s/%s (job %s)", bucket, key, job_id)

        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            original = obj["Body"].read()
            options = parse_options(obj.get("Metadata", {}))
            logger.info("Options: %s", options)

            output, stats = process_image(original, options)

            fmt = options["format"]
            out_key = f"processed/{job_id}.{EXTENSIONS[fmt]}"
            s3.put_object(
                Bucket=OUTPUT_BUCKET,
                Key=out_key,
                Body=output,
                ContentType=CONTENT_TYPES[fmt],
                Metadata={"source-key": key[:1000]},
            )

            result = {
                "status": "done",
                "job_id": job_id,
                "input_key": key,
                "output_key": out_key,
                "original_bytes": len(original),
                "processed_bytes": len(output),
                "reduction_percent": round(100 * (1 - len(output) / max(len(original), 1)), 1),
                "output_format": fmt,
                "quality": options["quality"],
                "max_size": options["max_size"],
                "processing_ms": int((time.time() - started) * 1000),
                **stats,
            }
            logger.info("Done: %s", json.dumps(result))

        except Exception as exc:  # noqa: BLE001 – report any failure to the user
            logger.exception("Processing failed for %s", key)
            result = {"status": "error", "job_id": job_id, "input_key": key, "error": str(exc)}

        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=f"results/{job_id}.json",
            Body=json.dumps(result).encode(),
            ContentType="application/json",
        )
        results.append(result)

    return {"processed": len(results), "results": results}
