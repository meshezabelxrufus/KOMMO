#!/usr/bin/env python3
"""
deploy_workflows_to_production.py
===================================
Deploys ONLY the 5 required Kommo CRM workflows to a production n8n instance.

This script:
  1. Reads the clean exported workflow JSONs from n8n_exports/
  2. Patches all localhost references to the production domain
  3. Patches Python binary paths to the Docker container path
  4. Imports each workflow via the n8n REST API
  5. Activates each workflow
  6. Reports the final state

Usage:
    # Deploy to production (dry-run first — prints what will change)
    python3 deploy_workflows_to_production.py --dry-run

    # Deploy to production
    python3 deploy_workflows_to_production.py \
        --n8n-url https://n8n.yourdomain.com \
        --api-key YOUR_PROD_API_KEY

    # Deploy without activating (safe for initial import)
    python3 deploy_workflows_to_production.py \
        --n8n-url https://n8n.yourdomain.com \
        --api-key YOUR_PROD_API_KEY \
        --no-activate
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EXPORTS_DIR = Path(__file__).parent / "n8n_exports"

# The 5 workflows to deploy — in dependency order
# (orchestrator last so downstream IDs are already known)
WORKFLOW_FILES = [
    "Kommo_CRM__Google_Sheets_Sync.json",
    "Kommo_CRM__Google_Drive_Upload.json",
    "Kommo_CRM__Claude_AI_Analysis.json",
    "Kommo_CRM__Notifications_Hub.json",
    "Kommo_CRM__Daily_AI_Pipeline.json",   # Deploy orchestrator last
]

# Patches: replace localhost/dev references with production equivalents
PROD_PATCHES = {
    # Python binary — local venv → Docker container path
    "/Users/abdulwaseyhussain/Downloads/KOMMO/.venv/bin/python": "python3",
    "/Users/abdulwaseyhussain/Downloads/KOMMO": "/opt/kommo-platform/app",
    # Local webhook URLs
    "http://localhost:5678": "{PROD_N8N_URL}",
    # Log paths
    "/tmp/kommo_run.log": "/app/logs/kommo_run.log",
}


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def patch_workflow(wf_dict: dict, prod_n8n_url: str) -> dict:
    """
    Recursively replace all localhost/dev references in the workflow JSON
    with their production equivalents.
    """
    raw = json.dumps(wf_dict, ensure_ascii=False)
    for old, new in PROD_PATCHES.items():
        raw = raw.replace(old, new.replace("{PROD_N8N_URL}", prod_n8n_url))
    return json.loads(raw)


def import_workflow(session: requests.Session, base_url: str, wf_dict: dict,
                    dry_run: bool) -> dict | None:
    """POST the workflow to n8n. Returns the created workflow dict."""
    name = wf_dict.get("name", "unknown")
    print(f"\n  ▶ Importing: {name}")

    if dry_run:
        print(f"    [DRY RUN] Would POST to {base_url}/api/v1/workflows")
        print(f"    Nodes: {len(wf_dict.get('nodes', []))}")
        return {"id": "DRY_RUN", "name": name}

    resp = session.post(f"{base_url}/api/v1/workflows", json=wf_dict)
    if resp.status_code not in (200, 201):
        print(f"    ❌ Import failed: HTTP {resp.status_code} — {resp.text[:300]}")
        return None

    created = resp.json()
    print(f"    ✅ Imported  ID={created['id']}")
    return created


def activate_workflow(session: requests.Session, base_url: str, wf_id: str,
                      dry_run: bool) -> bool:
    """PATCH the workflow to set active=true."""
    if dry_run:
        print(f"    [DRY RUN] Would activate workflow {wf_id}")
        return True

    resp = session.patch(
        f"{base_url}/api/v1/workflows/{wf_id}/activate"
    )
    if resp.status_code in (200, 204):
        print(f"    ✅ Activated ID={wf_id}")
        return True
    else:
        print(f"    ⚠️  Activation failed: HTTP {resp.status_code} — {resp.text[:200]}")
        return False


def verify_deployment(session: requests.Session, base_url: str) -> None:
    """Print a summary of all active workflows on the target instance."""
    resp = session.get(f"{base_url}/api/v1/workflows?limit=50")
    if resp.status_code != 200:
        print("  ⚠️  Could not fetch workflow list for verification")
        return

    workflows = resp.json().get("data", [])
    kommo_wfs = [w for w in workflows if "Kommo" in w.get("name", "")]

    print("\n" + "─" * 60)
    print("  Production Workflow Status")
    print("─" * 60)
    for w in kommo_wfs:
        status = "✅ ACTIVE" if w.get("active") else "⏸  INACTIVE"
        print(f"  {status}  {w['id']:<25} {w['name']}")
    print("─" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy Kommo CRM workflows to production n8n"
    )
    parser.add_argument(
        "--n8n-url",
        default="http://localhost:5678",
        help="Base URL of the production n8n instance (default: localhost for testing)"
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="n8n API key for the production instance"
    )
    parser.add_argument(
        "--no-activate",
        action="store_true",
        help="Import workflows but do NOT activate them"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deployed without making any changes"
    )
    args = parser.parse_args()

    base_url = args.n8n_url.rstrip("/")

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  Kommo CRM — Production Workflow Deployment          ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(f"\n  Target:   {base_url}")
    print(f"  Activate: {not args.no_activate}")
    print(f"  Dry run:  {args.dry_run}")
    print(f"  Exports:  {EXPORTS_DIR}\n")

    # Session with auth header
    session = requests.Session()
    session.headers.update({
        "X-N8N-API-KEY": args.api_key,
        "Content-Type": "application/json",
    })

    # Verify connection
    print("  Verifying connection to production n8n...")
    if not args.dry_run:
        try:
            r = session.get(f"{base_url}/api/v1/workflows?limit=1", timeout=10)
            r.raise_for_status()
            print("  ✅ Connected successfully\n")
        except Exception as e:
            print(f"  ❌ Cannot connect to {base_url}: {e}")
            sys.exit(1)

    # Check for existing Kommo workflows (prevent duplicates)
    if not args.dry_run:
        existing = session.get(f"{base_url}/api/v1/workflows?limit=50").json().get("data", [])
        existing_names = {w["name"] for w in existing}
        kommo_existing = [n for n in existing_names if "Kommo" in n]
        if kommo_existing:
            print(f"  ⚠️  Found {len(kommo_existing)} existing Kommo workflow(s) on this instance:")
            for n in kommo_existing:
                print(f"       - {n}")
            print("  These will be DUPLICATED. Delete them first if this is a re-deploy.\n")

    # Deploy each workflow in order
    deployed = []
    for filename in WORKFLOW_FILES:
        fpath = EXPORTS_DIR / filename
        if not fpath.exists():
            print(f"  ❌ Export file not found: {fpath}")
            print(f"     Run the export step first.")
            continue

        wf_dict = json.loads(fpath.read_text(encoding="utf-8"))

        # Apply production patches
        wf_dict = patch_workflow(wf_dict, base_url)

        # Import
        created = import_workflow(session, base_url, wf_dict, args.dry_run)
        if created is None:
            continue

        # Activate (unless --no-activate)
        if not args.no_activate:
            time.sleep(1)  # Brief pause to let n8n register the workflow
            activate_workflow(session, base_url, created["id"], args.dry_run)

        deployed.append(created)
        time.sleep(0.5)

    # Final verification
    print(f"\n  Deployed {len(deployed)}/{len(WORKFLOW_FILES)} workflows")
    if not args.dry_run:
        verify_deployment(session, base_url)

    print("\n  Next steps:")
    print("  1. Open the n8n UI and manually attach credentials to each workflow")
    print("     (Google Sheets, Google Drive, Anthropic, Gmail)")
    print("  2. Verify webhook URLs in the Notifications Hub workflow")
    print("  3. Trigger a manual test run of the Daily AI Pipeline")
    print("")


if __name__ == "__main__":
    main()
