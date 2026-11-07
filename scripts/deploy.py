#!/usr/bin/env python3
"""
One-command deployment (works on Windows, macOS and Linux).

    python scripts/deploy.py                 # deploy stack "image-processing"
    python scripts/deploy.py --stack mystack --region ap-south-1

Requires: AWS CLI (configured with `aws configure`) and AWS SAM CLI.
After deploying it writes the API URL into frontend/config.js automatically.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_WINDOWS = os.name == "nt"


def run(cmd, capture=False):
    print("  $", " ".join(cmd))
    return subprocess.run(
        cmd, cwd=ROOT, check=True, shell=IS_WINDOWS,
        capture_output=capture, text=True,
    )


def require(tool, hint):
    if not shutil.which(tool):
        sys.exit(f"ERROR: '{tool}' not found. {hint}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default="image-processing")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-south-1"))
    args = parser.parse_args()

    require("aws", "Install the AWS CLI: https://aws.amazon.com/cli/")
    require("sam", "Install the AWS SAM CLI: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html")

    if not os.path.exists(os.path.join(ROOT, "layer", "pillow-layer.zip")):
        sys.exit("ERROR: layer/pillow-layer.zip missing. Run: python scripts/build_layer.py")

    print(f"\n[1/3] Checking AWS credentials ...")
    ident = json.loads(run(["aws", "sts", "get-caller-identity", "--output", "json"], capture=True).stdout)
    print(f"      Account {ident['Account']}  ({ident['Arn']})")

    print(f"\n[2/3] Deploying stack '{args.stack}' to {args.region} (takes ~2 min) ...")
    run([
        "sam", "deploy",
        "--stack-name", args.stack,
        "--region", args.region,
        "--resolve-s3",
        "--capabilities", "CAPABILITY_IAM",
        "--no-confirm-changeset",
        "--no-fail-on-empty-changeset",
    ])

    print("\n[3/3] Reading stack outputs ...")
    out = run([
        "aws", "cloudformation", "describe-stacks",
        "--stack-name", args.stack, "--region", args.region,
        "--query", "Stacks[0].Outputs", "--output", "json",
    ], capture=True).stdout
    outputs = {o["OutputKey"]: o["OutputValue"] for o in json.loads(out)}

    config_path = os.path.join(ROOT, "frontend", "config.js")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("// Written automatically by scripts/deploy.py\n")
        f.write(f'window.APP_CONFIG = {{ API_URL: "{outputs["ApiUrl"]}" }};\n')

    print("\nDeployed successfully!")
    for k, v in outputs.items():
        print(f"  {k:24} {v}")
    print(f"\nfrontend/config.js updated. Now open frontend/index.html in your browser.")


if __name__ == "__main__":
    main()
