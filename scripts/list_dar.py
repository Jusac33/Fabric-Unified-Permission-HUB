import os, sys, httpx, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.azure_identity import get_fabric_token

# Env-driven — set FABRIC_WORKSPACE_ID and FABRIC_ITEM_ID first.
WS = os.environ["FABRIC_WORKSPACE_ID"]
ITEM = os.environ["FABRIC_ITEM_ID"]
url = (f'https://api.fabric.microsoft.com/v1/workspaces/{WS}'
       f'/items/{ITEM}/dataAccessRoles')
r = httpx.get(url, headers={'Authorization': f'Bearer {get_fabric_token()}'}, timeout=30)
data = r.json()['value']
print(f'Total OneLake DAR roles on mirrored catalog: {len(data)}')
for role in data:
    members = role.get('members', {}).get('microsoftEntraMembers', [])
    rules = role.get('decisionRules', [])
    paths, actions = [], []
    for rule in rules:
        for p in rule.get('permission', []):
            if p.get('attributeName') == 'Path':
                paths += p.get('attributeValueIncludedIn', [])
            elif p.get('attributeName') == 'Action':
                actions += p.get('attributeValueIncludedIn', [])
    print(f"  - {role.get('name'):40s} actions={actions} paths={paths} members={len(members)}")
