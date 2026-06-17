import argparse
import configparser
import os
from typing import Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key


"""
Bulk purge utility for ring documents.

Deletes all documents that belong to a given:
  - environment (maps to <env>_data table)
  - portfolio
  - org
  - ring (blueprint name)
  
  
USAGE

Preview First:

python purge_ring_documents.py xyz \
  --aws-profile default \
  --portfolio 74d2e509cbfe \
  --org c3db32134ca3 \
  --ring infrastructure_elements \
  --dry-run
  

Delete for real:

python purge_ring_documents.py xyz \
  --aws-profile default \
  --aws-region us-east-1 \
  --portfolio 74d2e509cbfe \
  --org c3db32134ca3 \
  --ring infrastructure_elements
  
  
Non-interactive delete:

python purge_ring_documents.py xyz \
  --aws-profile default \
  --portfolio c3db32134ca3 \
  --org c3db32134ca3 \
  --ring infrastructure_elements \
  --yes
  
  
"""


def get_available_aws_profiles() -> List[str]:
    profiles: List[str] = []
    aws_credentials_path = os.path.expanduser("~/.aws/credentials")
    aws_config_path = os.path.expanduser("~/.aws/config")

    if os.path.exists(aws_credentials_path):
        config = configparser.ConfigParser()
        config.read(aws_credentials_path)
        profiles.extend(config.sections())

    if os.path.exists(aws_config_path):
        config = configparser.ConfigParser()
        config.read(aws_config_path)
        for section in config.sections():
            if section.startswith("profile "):
                profile_name = section.replace("profile ", "")
                if profile_name not in profiles:
                    profiles.append(profile_name)

    return profiles if profiles else ["default"]


def get_profile_region(profile_name: str) -> str:
    config = configparser.ConfigParser()
    config_path = os.path.expanduser("~/.aws/config")

    if os.path.exists(config_path):
        config.read(config_path)
        profile_section = (
            f"profile {profile_name}" if profile_name != "default" else "default"
        )
        if profile_section in config and "region" in config[profile_section]:
            return config[profile_section]["region"]

    return "us-east-1"


def _query_ring_items_page(
    table,
    portfolio: str,
    org: str,
    ring: str,
    limit: int,
    last_evaluated_key: Optional[Dict] = None,
) -> Tuple[List[Dict], Optional[Dict]]:
    portfolio_index = f"irn:data:{portfolio}"
    path_index_prefix = f"irn:h_index:{org}:{ring}"
    kwargs = {
        "IndexName": "path_index",
        "KeyConditionExpression": Key("portfolio_index").eq(portfolio_index)
        & Key("path_index").begins_with(path_index_prefix),
        "Limit": limit,
    }
    if last_evaluated_key:
        kwargs["ExclusiveStartKey"] = last_evaluated_key
    response = table.query(**kwargs)
    return response.get("Items", []), response.get("LastEvaluatedKey")


def _iter_ring_items(table, portfolio: str, org: str, ring: str, page_limit: int):
    last_key: Optional[Dict] = None
    while True:
        items, last_key = _query_ring_items_page(
            table=table,
            portfolio=portfolio,
            org=org,
            ring=ring,
            limit=page_limit,
            last_evaluated_key=last_key,
        )
        for item in items:
            yield item
        if not last_key:
            break


def purge_ring_documents(
    environment_name: str,
    aws_profile: str,
    portfolio: str,
    org: str,
    ring: str,
    region: Optional[str] = None,
    dry_run: bool = False,
    page_limit: int = 200,
) -> Dict[str, int]:
    if region is None:
        region = get_profile_region(aws_profile)

    boto3.setup_default_session(profile_name=aws_profile)
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table_name = f"{environment_name}_data"
    table = dynamodb.Table(table_name)

    scanned = 0
    deleted = 0
    failed = 0

    print(
        f"Target table={table_name} portfolio={portfolio} org={org} ring={ring} region={region}"
    )
    if dry_run:
        print("Dry run enabled. No deletes will be executed.")

    for item in _iter_ring_items(
        table=table, portfolio=portfolio, org=org, ring=ring, page_limit=page_limit
    ):
        scanned += 1
        portfolio_index = item.get("portfolio_index")
        doc_index = item.get("doc_index")
        doc_id = item.get("_id")
        if not portfolio_index or not doc_index:
            failed += 1
            print(f"Skip invalid item missing key fields (_id={doc_id})")
            continue

        if dry_run:
            continue

        try:
            table.delete_item(
                Key={
                    "portfolio_index": portfolio_index,
                    "doc_index": doc_index,
                }
            )
            deleted += 1
            if deleted % 100 == 0:
                print(f"Deleted {deleted} items...")
        except Exception as exc:
            failed += 1
            print(f"Failed delete _id={doc_id}: {exc}")

    return {
        "scanned": scanned,
        "deleted": deleted if not dry_run else 0,
        "failed": failed,
    }


def _confirm_execution(environment: str, portfolio: str, org: str, ring: str) -> bool:
    print("WARNING: This operation will permanently delete documents.")
    print(f"env={environment} portfolio={portfolio} org={org} ring={ring}")
    token = input("Type DELETE to continue: ").strip()
    return token == "DELETE"


def refresh_ring_cache(
    portfolio: str,
    org: str,
    ring: str,
    aws_profile: str,
    region: str,
) -> bool:
    """
    Refresh S3 cache for the target ring after bulk purge.

    Uses the same DataController entrypoint as AID/ARD orchestrators.
    """
    try:
        boto3.setup_default_session(profile_name=aws_profile, region_name=region)
        from renglo.common import load_config
        from renglo.data.data_controller import DataController

        config = load_config()
        dac = DataController(config=config)
        dac.refresh_s3_cache(portfolio, org, ring, None)
        print(
            f"Refreshed cache for ring={ring} portfolio={portfolio} org={org} region={region}"
        )
        return True
    except Exception as exc:
        print(
            f"WARNING: Purge completed but cache refresh failed ({ring}): {exc}"
        )
        print(
            "You can refresh later by running AID/ARD orchestrator or calling refresh_s3_cache manually."
        )
        return False


def main():
    available_profiles = get_available_aws_profiles()
    parser = argparse.ArgumentParser(
        description=(
            "Delete all documents for a specific portfolio/org/ring from <env>_data "
            "using the path_index secondary index."
        )
    )
    parser.add_argument(
        "environment_name", type=str, help="Environment prefix (e.g. dev, test, prod)"
    )
    parser.add_argument(
        "--aws-profile",
        type=str,
        choices=available_profiles,
        default="default",
        help=f"AWS profile (available: {', '.join(available_profiles)})",
    )
    parser.add_argument("--aws-region", type=str, help="AWS region override")
    parser.add_argument("--portfolio", type=str, required=True, help="Portfolio id")
    parser.add_argument("--org", type=str, required=True, help="Org id")
    parser.add_argument(
        "--ring",
        type=str,
        help="Ring (blueprint name), e.g. infrastructure_elements",
    )
    parser.add_argument("--blueprint", type=str, help="Alias for --ring")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview matches without deleting",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive DELETE confirmation",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=200,
        help="Query page size for path_index scan (default: 200)",
    )
    parser.add_argument(
        "--skip-cache-refresh",
        action="store_true",
        help="Do not trigger refresh_s3_cache after successful delete",
    )
    args = parser.parse_args()

    ring = (args.ring or args.blueprint or "").strip()
    if not ring:
        parser.error("You must provide --ring (or --blueprint)")

    region = args.aws_region or get_profile_region(args.aws_profile)

    if not args.dry_run and not args.yes:
        confirmed = _confirm_execution(
            args.environment_name, args.portfolio, args.org, ring
        )
        if not confirmed:
            print("Cancelled.")
            return

    results = purge_ring_documents(
        environment_name=args.environment_name,
        aws_profile=args.aws_profile,
        portfolio=args.portfolio,
        org=args.org,
        ring=ring,
        region=region,
        dry_run=args.dry_run,
        page_limit=args.page_limit,
    )

    print("\nPurge summary")
    print(f"scanned: {results['scanned']}")
    print(f"deleted: {results['deleted']}")
    print(f"failed : {results['failed']}")

    if not args.dry_run and not args.skip_cache_refresh:
        refresh_ring_cache(args.portfolio, args.org, ring, args.aws_profile, region)


if __name__ == "__main__":
    main()
