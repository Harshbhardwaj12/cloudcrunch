#!/usr/bin/env python3
"""
Delete everything this project created, so nothing keeps costing money.

    python scripts/teardown.py --stack image-processing --region ap-south-1
"""

import argparse
import json
import os
import subprocess

IS_WINDOWS = os.name == "nt"


def run(cmd, capture=False, check=True):
    print("  $", " ".join(cmd))
    return subprocess.run(cmd, check=check, shell=IS_WINDOWS, capture_output=capture, text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default="image-processing")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-south-1"))
    args = parser.parse_args()

    out = run([
        "aws", "cloudformation", "describe-stacks",
        "--stack-name", args.stack, "--region", args.region,
        "--query", "Stacks[0].Outputs", "--output", "json",
    ], capture=True).stdout
    outputs = {o["OutputKey"]: o["OutputValue"] for o in json.loads(out)}

    # Buckets must be empty before CloudFormation can delete them
    for key in ("InputBucketName", "OutputBucketName"):
        bucket = outputs.get(key)
        if bucket:
            print(f"Emptying s3://{bucket}")
            run(["aws", "s3", "rm", f"s3://{bucket}", "--recursive", "--region", args.region], check=False)

    run(["sam", "delete", "--stack-name", args.stack, "--region", args.region, "--no-prompts"])
    print("All resources deleted.")


if __name__ == "__main__":
    main()
