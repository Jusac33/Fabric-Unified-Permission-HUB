"""Fabric writer — writes to workspace_role OR onelake_data_access_role per plan."""
from __future__ import annotations
import logging
import httpx

from src.auth.identity import IdentityProvider
from src.writers.fabric_write_planner import FabricWritePlan

log = logging.getLogger(__name__)


class FabricWriter:
    """Auth tier: Tier 1 — MI-issued Power BI token."""

    def __init__(self, identity: IdentityProvider, api_base: str):
        self._identity = identity
        self._api = api_base.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._identity.get_fabric_token()}",
                "Content-Type": "application/json"}

    def write(self, plan: FabricWritePlan) -> dict:
        if plan.layer == "skip":
            return {"action": "skipped", "reason": "no layer mapping"}
        if plan.dry_run:
            return {"action": "dry_run_would_apply", "plan": plan.__dict__}
        if plan.layer == "workspace_role":
            return self.write_workspace_role(plan)
        if plan.layer == "onelake_data_access_role":
            return self.write_onelake_data_access_role(plan)
        return {"action": "skipped", "reason": f"unknown layer {plan.layer}"}

    def write_workspace_role(self, plan: FabricWritePlan) -> dict:
        body = {
            "principal": {"id": plan.principal_entra_id, "type": plan.principal_type},
            "role": plan.role_or_action,
        }
        r = httpx.post(
            f"{self._api}/workspaces/{plan.workspace_id}/roleAssignments",
            headers=self._headers(), json=body, timeout=30,
        )
        if r.status_code >= 400:
            log.warning("write_workspace_role failed: %s %s", r.status_code, r.text[:300])
            return {"action": "error", "status": r.status_code, "body": r.text[:300]}
        return {"action": "granted", "status": r.status_code}

    def revoke_workspace_role(self, plan: FabricWritePlan) -> dict:
        r = httpx.delete(
            f"{self._api}/workspaces/{plan.workspace_id}/"
            f"roleAssignments/{plan.principal_entra_id}",
            headers=self._headers(), timeout=30,
        )
        if r.status_code >= 400 and r.status_code not in (404, 204):
            log.warning("revoke_workspace_role failed: %s %s", r.status_code, r.text[:300])
            return {"action": "error", "status": r.status_code, "body": r.text[:300]}
        return {"action": "revoked", "status": r.status_code}

    def write_onelake_data_access_role(self, plan: FabricWritePlan) -> dict:
        if not plan.item_id:
            return {"action": "skipped", "reason": "no item_id for OneLake DAR"}
        body = {
            "name": f"PermSync-{plan.principal_entra_id[:8]}",
            "members": {"entraMembers": [{
                "objectId": plan.principal_entra_id,
                "objectType": plan.principal_type,
            }]},
            "decisionRules": [{
                "effect": "Permit",
                "permission": [{
                    "attributeName": "Action",
                    "attributeValueIncludedIn": [plan.role_or_action],
                }],
            }],
        }
        try:
            r = httpx.post(
                f"{self._api}/workspaces/{plan.workspace_id}/items/"
                f"{plan.item_id}/dataAccessRoles?conflictResolution=Overwrite",
                headers=self._headers(), json=body, timeout=30,
            )
            if r.status_code in (404, 501):
                log.warning("OneLake DAR preview unavailable: %s", r.status_code)
                return {"action": "skipped", "reason": "preview API unavailable"}
            if r.status_code >= 400:
                log.warning("OneLake DAR failed: %s %s", r.status_code, r.text[:300])
                return {"action": "error", "status": r.status_code, "body": r.text[:300]}
            return {"action": "granted", "status": r.status_code, "layer": "onelake"}
        except httpx.HTTPError as e:
            log.warning("OneLake DAR exception: %s", e)
            return {"action": "error", "exception": str(e)}
