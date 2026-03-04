'''Configuration handling for the sync agent.
Environment variables are read via ``python-dotenv`` (if a .env file is present) and
exposed as a dataclass for easy injection.
'''

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# Load a .env file if present (no error if missing)
load_dotenv()

@dataclass(frozen=True)
class AppConfig:
    sheets_json_base_url: str = os.getenv(
        "SHEETS_JSON_BASE_URL",
        "https://YOUR-SHEETS-JSON-GATEWAY.a.run.app",
    )
    sheets_json_api_key: Optional[str] = os.getenv("SHEETS_JSON_API_KEY")
    control_sheet_id: str = os.getenv("CONTROL_SHEET_ID")
    # Optional default target sheet – individual jobs may override it
    default_target_sheet_id: Optional[str] = os.getenv("DEFAULT_TARGET_SHEET_ID")

    def __post_init__(self):
        # Fail fast if required configuration is missing
        if not self.control_sheet_id:
            raise ValueError("Environment variable CONTROL_SHEET_ID must be set for the sync agent to operate")

    @property
    def gateway(self):
        """Factory property returning a configured :class:`so_sync.gateway.SheetsGateway`.
        The gateway is cheap to instantiate; callers can reuse the same instance.
        """
        from .gateway import SheetsGateway
        return SheetsGateway(base_url=self.sheets_json_base_url, api_key=self.sheets_json_api_key or "")
