from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "Fabric Unified Permission Hub"
    APP_ENV: str = "development"
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8000
    APP_DEBUG: bool = False
    SECRET_KEY: str = "change-me"

    CONFIGS_DIR: str = "./configs"
    AUDIT_DIR: str = "./audits"
    DATA_DIR: str = "./data"

    AZURE_TENANT_ID: str | None = None
    AZURE_CLIENT_ID: str | None = None
    AZURE_CLIENT_SECRET: str | None = None

    # Default entity pointers for the hub UI — env-driven, NOT hardcoded
    DBX_WORKSPACE_URL: str | None = None
    DBX_WAREHOUSE_ID: str | None = None
    FABRIC_WORKSPACE_ID: str | None = None

    # Background re-scan interval in minutes (0 = disabled).
    SCAN_INTERVAL_MINUTES: int = 0

    # Require an approval before any real (non-dry-run) apply.
    REQUIRE_APPROVAL: bool = False

    # --- Authentication (Entra SSO) — disabled by default ---
    AUTH_ENABLED: bool = False
    AUTH_CLIENT_ID: str | None = None
    AUTH_CLIENT_SECRET: str | None = None
    AUTH_TENANT_ID: str | None = None
    AUTH_REDIRECT_URI: str = "http://127.0.0.1:8000/auth/callback"
    # Comma-separated email lists mapped to roles. Everyone authenticated is a viewer.
    AUTH_ADMIN_EMAILS: str = ""
    AUTH_APPROVER_EMAILS: str = ""

    @property
    def admin_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.AUTH_ADMIN_EMAILS.split(",") if e.strip()}

    @property
    def approver_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.AUTH_APPROVER_EMAILS.split(",") if e.strip()}

    @property
    def configs_path(self) -> Path:
        p = Path(self.CONFIGS_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def snapshots_path(self) -> Path:
        p = Path("./snapshots").resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def audit_path(self) -> Path:
        p = Path(self.AUDIT_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_path(self) -> Path:
        p = Path(self.DATA_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "uph.db"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.strip().lower() in {"prod", "production"}

    def validate_runtime_security(self) -> None:
        if not self.is_production:
            return

        errors: list[str] = []
        if self.APP_DEBUG:
            errors.append("APP_DEBUG must be false when APP_ENV=production")
        if not self.SECRET_KEY or self.SECRET_KEY == "change-me":
            errors.append("SECRET_KEY must be set to a non-default value when APP_ENV=production")
        if errors:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))


settings = Settings()
