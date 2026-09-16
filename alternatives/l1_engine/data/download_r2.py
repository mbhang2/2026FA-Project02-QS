from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import boto3
from dotenv import load_dotenv
from alternatives.l1_engine._roots import tree_root

# --- Configuration ---
# Anchored on a marker and a name, never on a depth (see `../_roots.py`). When
# this counted directories the Task 44 move sent it looking for the R2
# credentials one directory too high, where it would have reported "please
# ensure r2.env is correctly filled out" -- a path bug wearing a
# configuration bug's clothes.
ENV_PATH = tree_root() / "secrets" / "r2.env"
PLACEHOLDER_PREFIX = "YOUR_"

def validate_env_vars(env_vars: dict[str, str]) -> None:
    """Validates R2 environment variables."""
    required_vars = [
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
        "R2_ENDPOINT",
    ]
    missing_vars = [var for var in required_vars if var not in env_vars]
    if missing_vars:
        print(f"Error: Missing required environment variables in {ENV_PATH}: {', '.join(missing_vars)}")
        print("Please ensure r2.env is correctly filled out.")
        sys.exit(1)

    placeholder_vars = {
        k: v for k, v in env_vars.items() if v.startswith(PLACEHOLDER_PREFIX)
    }
    if placeholder_vars:
        print(f"Error: Placeholder values found in {ENV_PATH}:")
        for k, v in placeholder_vars.items():
            print(f"  {k}={v}")
        print("Please replace all placeholder values with your actual R2 credentials.")
        sys.exit(1)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync files from Cloudflare R2 bucket to local directory."
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="raw/",
        help="R2 bucket prefix to sync from (e.g., 'raw/data/').",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path("market_data/raw/"),
        help="Local destination directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List objects without downloading them.",
    )
    args = parser.parse_args()

    # Load environment variables
    load_dotenv(dotenv_path=ENV_PATH)

    env_vars = {
        "R2_ACCOUNT_ID": os.getenv("R2_ACCOUNT_ID", ""),
        "R2_ACCESS_KEY_ID": os.getenv("R2_ACCESS_KEY_ID", ""),
        "R2_SECRET_ACCESS_KEY": os.getenv("R2_SECRET_ACCESS_KEY", ""),
        "R2_BUCKET": os.getenv("R2_BUCKET", ""),
        "R2_ENDPOINT": os.getenv("R2_ENDPOINT", ""),
    }
    validate_env_vars(env_vars)

    # Ensure destination directory exists
    args.dest.mkdir(parents=True, exist_ok=True)

    session = boto3.session.Session()
    s3_client = session.client(
        service_name="s3",
        endpoint_url=env_vars["R2_ENDPOINT"],
        aws_access_key_id=env_vars["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env_vars["R2_SECRET_ACCESS_KEY"],
        region_name="auto",  # R2 typically uses 'auto'
    )

    print(f"Syncing from R2 bucket '{env_vars['R2_BUCKET']}' (prefix: '{args.prefix}') to '{args.dest}'")

    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=env_vars["R2_BUCKET"], Prefix=args.prefix)

    downloaded_count = 0
    for page in pages:
        if "Contents" in page:
            for obj in page["Contents"]:
                key = obj["Key"]
                local_file_path = args.dest / Path(key).name # Use .name to avoid recreating subdirectories if prefix contains them

                if args.dry_run:
                    print(f"  [DRY RUN] Would download: s3://{env_vars['R2_BUCKET']}/{key} -> {local_file_path}")
                else:
                    try:
                        print(f"  Downloading: {key} -> {local_file_path}")
                        s3_client.download_file(env_vars["R2_BUCKET"], key, str(local_file_path))
                        downloaded_count += 1
                    except Exception as e:
                        print(f"    Error downloading {key}: {e}")
    
    if args.dry_run:
        print("Dry run complete. No files were downloaded.")
    else:
        print(f"Download complete. Downloaded {downloaded_count} files.")


if __name__ == "__main__":
    main()
