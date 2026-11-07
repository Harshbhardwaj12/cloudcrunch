#!/usr/bin/env python3
"""
(Re)build layer/pillow-layer.zip — the Pillow library compiled for AWS Lambda
(Amazon Linux, Python 3.12, x86_64). A pre-built zip is already included,
so you only need this if you want a different Pillow version.

Works on any OS because it downloads the Linux wheel instead of compiling.

    python scripts/build_layer.py
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PILLOW_VERSION = "12.0.0"


def main():
    out_zip = os.path.join(ROOT, "layer", "pillow-layer.zip")
    os.makedirs(os.path.dirname(out_zip), exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([
            sys.executable, "-m", "pip", "download", f"pillow=={PILLOW_VERSION}",
            "--platform", "manylinux2014_x86_64", "--python-version", "3.12",
            "--only-binary=:all:", "--no-deps", "-d", tmp,
        ], check=True)
        wheel = glob.glob(os.path.join(tmp, "*.whl"))[0]

        # A Lambda layer for Python must put packages inside a "python/" folder
        with zipfile.ZipFile(wheel) as whl, zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as layer:
            for item in whl.infolist():
                info = zipfile.ZipInfo("python/" + item.filename, item.date_time)
                info.external_attr = item.external_attr or (0o644 << 16)
                info.compress_type = zipfile.ZIP_DEFLATED
                layer.writestr(info, whl.read(item.filename))

    print(f"Built {out_zip} ({os.path.getsize(out_zip) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
