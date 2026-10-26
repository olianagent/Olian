"""Runtime settings. Secrets and deployment knobs come from the environment (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("OLIAN_DATA_DIR", "./data")).resolve())
    hermes_bin: str = field(default_factory=lambda: _env("OLIAN_HERMES_BIN", "hermes"))
    # "process": Hermes gateway as a child process of the orchestrator; "external": already running.
    hermes_mode: str = field(default_factory=lambda: _env("OLIAN_HERMES_MODE", "process"))
    port_base: int = field(default_factory=lambda: int(_env("OLIAN_PORT_BASE", "8700")))
    api_key: str = field(default_factory=lambda: _env("OLIAN_API_KEY", ""))
    webhook_secret: str = field(default_factory=lambda: _env("OLIAN_WEBHOOK_SECRET", ""))
    host: str = field(default_factory=lambda: _env("OLIAN_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("OLIAN_PORT", "8600")))
    # Default model config handed to every new project's Hermes profile (project.yaml may override).
    default_provider: str = field(default_factory=lambda: _env("OLIAN_DEFAULT_PROVIDER", ""))
    default_model: str = field(default_factory=lambda: _env("OLIAN_DEFAULT_MODEL", ""))
    sandbox_image: str = field(default_factory=lambda: _env("OLIAN_SANDBOX_IMAGE", "nousresearch/hermes-sandbox:desktop"))
    # Meta WhatsApp Cloud API (outreach sender); per-project overrides live in project.yaml.
    wa_token: str = field(default_factory=lambda: _env("WHATSAPP_CLOUD_TOKEN", ""))
    wa_verify_token: str = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN", ""))
    wa_app_secret: str = field(default_factory=lambda: _env("WHATSAPP_APP_SECRET", ""))
    # URL under which a project's Hermes (possibly inside a docker sandbox) reaches this orchestrator.
    agent_api_url: str = field(default_factory=lambda: _env("OLIAN_AGENT_API_URL", ""))
    telegram_notify_token: str = field(default_factory=lambda: _env("OLIAN_NOTIFY_TELEGRAM_TOKEN", ""))

    @property
    def agent_url(self) -> str:
        return (self.agent_api_url or f"http://127.0.0.1:{self.port}").rstrip("/")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "olian.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def hermes_root(self) -> Path:
        """Parent of per-project Hermes homes; each project's HERMES_HOME is <hermes_root>/<slug>."""
        return self.data_dir / "hermes"

    def session_fernet(self):
        """Fernet for Telegram user sessions at rest. A session string IS the account, so no key = no storage."""
        key = os.environ.get("OLIAN_SESSION_KEY", "")
        if not key:
            raise RuntimeError("OLIAN_SESSION_KEY is not set (olian setup generates one): refusing to store Telegram sessions unencrypted")
        from cryptography.fernet import Fernet
        return Fernet(key.encode())

    def pii_salt(self) -> bytes:
        """Secret salt for pseudonymising author ids. From OLIAN_PII_SALT, else a generated file (0600) in the data dir.
        Losing it makes existing pseudonyms unlinkable (which is safe); leaking it weakens them."""
        env = os.environ.get("OLIAN_PII_SALT", "")
        if env:
            return env.encode()
        path = self.data_dir / ".pii_salt"
        if not path.exists():
            import secrets
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_hex(32))
            path.chmod(0o600)
        return path.read_text().strip().encode()

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.projects_dir, self.hermes_root):
            p.mkdir(parents=True, exist_ok=True)
