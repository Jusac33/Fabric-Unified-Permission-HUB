"""Probe Fabric DAR API with various Path values to find what's accepted."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json, base64, httpx
from app.services.azure_identity import get_fabric_token, get_token

# Env-driven — set FABRIC_WORKSPACE_ID and FABRIC_ITEM_ID first.
WS = os.environ["FABRIC_WORKSPACE_ID"]
ITEM = os.environ["FABRIC_ITEM_ID"]
URL = f"https://api.fabric.microsoft.com/v1/workspaces/{WS}/items/{ITEM}/dataAccessRoles"

ftok = get_fabric_token()
gtok = get_token("https://graph.microsoft.com/.default")
me = httpx.get("https://graph.microsoft.com/v1.0/me",
               headers={"Authorization": f"Bearer {gtok}"}, timeout=15).json()
OID = me["id"]
TID = json.loads(base64.urlsafe_b64decode(ftok.split(".")[1] + "==="))["tid"]
print("oid", OID, "tid", TID)

H = {"Authorization": f"Bearer {ftok}", "Content-Type": "application/json"}
current = httpx.get(URL, headers=H, timeout=30).json()["value"]

paths = [
    "*",
    "/sales/orders",
    "/Tables/sales.orders",
    "/Tables/sales/orders",
    "/sales",
    "Tables/sales/orders",
]

for p in paths:
    safe = p.replace("/", "").replace("*", "all").replace(".", "")
    role = {
        "name": f"hubtest{safe}"[:60],
        "decisionRules": [{
            "effect": "Permit",
            "permission": [
                {"attributeName": "Path", "attributeValueIncludedIn": [p]},
                {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
            ],
        }],
        "members": {"microsoftEntraMembers": [
            {"objectId": OID, "tenantId": TID, "objectType": "User"}
        ]},
    }
    body = {"value": current + [role]}
    r = httpx.put(URL, headers=H, json=body, timeout=30)
    print(f"PATH={p!r:30s} -> {r.status_code} {r.text[:250]}")
