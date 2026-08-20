"""Runtime configuration.

The database deliberately lives outside the install directory (architecture
§14) so that an update can replace the program folder wholesale without
touching a single sale.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    elif sys.platform == "darwin":  # pragma: no cover - not a target OS
        base = Path.home() / "Library" / "Application Support"
    else:  # pragma: no cover - not a target OS
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "RetailPOS"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Cloud ───────────────────────────────────────────────────────────────
    supabase_url: str = ""
    #: The anon key only. The service_role key is never bundled (§1.7).
    supabase_anon_key: str = ""

    # ── Terminal identity ───────────────────────────────────────────────────
    store_code: str = "ST01"
    terminal_code: str = "T1"
    #: Printed at the head of every receipt. A GSTIN is required on a GST
    #: invoice; blank means the receipt omits the line rather than printing an
    #: empty one.
    store_name: str = "Demo Kirana"
    store_gstin: str = ""

    # ── Local storage ───────────────────────────────────────────────────────
    data_dir: Path = Field(default_factory=_default_data_dir)
    db_filename: str = "pos.sqlite3"

    # ── Shell ───────────────────────────────────────────────────────────────
    health_timeout_seconds: float = 20.0
    window_title: str = "Register"
    fullscreen: bool = True
    #: Boot the shell with no Supabase project configured (offline dev only).
    allow_offline_bootstrap: bool = False

    # ── argon2id, tuned to ~100 ms on target hardware (§11.4) ───────────────
    # Measured at ~87 ms with these values on a 2024 desktop. Memory is held
    # at 64 MiB rather than pushed higher because the authenticate-pin Edge
    # Function must verify with identical parameters inside a 256 MB runtime.
    # Re-run scripts/tune_argon2.py on the actual till before the pilot.
    argon2_time_cost: int = 12
    argon2_memory_cost_kib: int = 65536
    argon2_parallelism: int = 4

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "pos.lock"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "pos.log"

    @property
    def cloud_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
