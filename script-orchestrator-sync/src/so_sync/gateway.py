from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional

import requests


class SheetsGatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        op: str,
        status_code: int | None = None,
        response_body: str | None = None,
        next_action: str | None = None,
    ):
        super().__init__(message)
        self.op = op
        self.status_code = status_code
        self.response_body = response_body
        self.next_action = next_action


@dataclass(frozen=True)
class SheetsGateway:
    base_url: str
    api_key: str = ""

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    @staticmethod
    def _extract_tab_name(text: str) -> str | None:
        if not text:
            return None
        m = re.search(r"'([^']+)'!", text)
        if m:
            return m.group(1)
        m = re.search(r'([A-Za-z0-9_ \-]+)![A-Z]+\d+', text)
        if m:
            return m.group(1).strip()
        return None

    @classmethod
    def _error_hint(cls, status_code: int | None, body: str, *, range_hint: str | None = None) -> str | None:
        text = body.lower()
        if status_code == 401 or "unauthorized" in text or "invalid api key" in text:
            return "Set SHEETS_JSON_API_KEY or verify gateway auth configuration"
        if status_code == 403 or "allowlist" in text or "not allowlisted" in text:
            return (
                "Add the sheet_id to SHEETS_ALLOWLIST and share the sheet with "
                "sheets-db-service@latest-ave-sheets-01.iam.gserviceaccount.com."
            )
        if "unable to parse range" in text:
            tab = cls._extract_tab_name(range_hint or "") or cls._extract_tab_name(body) or "<TAB>"
            return f"Tab name missing or mismatched; create/rename sheet tab to exactly {tab}"
        if "updates" in text and ("missing" in text or "required" in text):
            return "Write payload must be {sheet_id, updates:[{range, values}]}."
        if "ranges" in text and ("missing" in text or "required" in text):
            return "Read payload must be {sheet_id, ranges:[\"'Tab'!A1:Z9\", ...]}."
        return None

    def _raise_http_error(
        self,
        *,
        op: str,
        status_code: int,
        sheet_id: str,
        details: str,
        range_hint: str | None = None,
    ) -> None:
        hint = self._error_hint(status_code, details, range_hint=range_hint)
        base = f"{op} failed status={status_code} sheet_id={sheet_id} response_body={details}"
        if hint:
            base = f"{base} next_action={hint}"
        raise SheetsGatewayError(
            base,
            op=op,
            status_code=status_code,
            response_body=details,
            next_action=hint,
        )

    def read(
        self,
        sheet_id: str,
        ranges: List[str],
        *,
        value_render_option: Optional[str] = None,
        timeout_s: int = 60,
    ) -> Dict[str, Any]:
        if not isinstance(ranges, list) or not all(isinstance(r, str) and r.strip() for r in ranges):
            raise SheetsGatewayError(
                "read requires ranges as a non-empty list of A1 strings",
                op="read",
                next_action='Use payload shape: {"sheet_id": "...", "ranges": ["\'Tab\'!A1:Z9"]}',
            )
        payload: Dict[str, Any] = {"sheet_id": sheet_id, "ranges": ranges}
        if value_render_option:
            payload["valueRenderOption"] = value_render_option

        r = requests.post(
            f"{self.base_url.rstrip('/')}/v1/read",
            headers=self._headers(),
            json=payload,
            timeout=timeout_s,
        )
        if r.status_code != 200:
            range_hint = ranges[0] if ranges else None
            self._raise_http_error(
                op="read",
                status_code=r.status_code,
                sheet_id=sheet_id,
                details=r.text,
                range_hint=range_hint,
            )

        try:
            data = r.json()
        except Exception as exc:
            raise SheetsGatewayError(
                f"read returned non-JSON response for sheet_id={sheet_id}: {r.text}",
                op="read",
                status_code=r.status_code,
                response_body=r.text,
            ) from exc
        if not data.get("ok"):
            body = str(data)
            range_hint = ranges[0] if ranges else None
            hint = self._error_hint(r.status_code, body, range_hint=range_hint)
            message = f"read not ok for sheet_id={sheet_id}: {body}"
            if hint:
                message = f"{message} next_action={hint}"
            raise SheetsGatewayError(
                message,
                op="read",
                status_code=r.status_code,
                response_body=body,
                next_action=hint,
            )
        return data

    def write(
        self,
        sheet_id: str,
        updates: List[Dict[str, Any]],
        *,
        timeout_s: int = 60,
    ) -> Dict[str, Any]:
        if not isinstance(updates, list):
            raise SheetsGatewayError(
                "write requires updates as a list",
                op="write",
                next_action='Use payload shape: {"sheet_id": "...", "updates": [{"range": "...", "values": [[...]]}]}',
            )
        payload: Dict[str, Any] = {"sheet_id": sheet_id, "updates": updates}

        r = requests.post(
            f"{self.base_url.rstrip('/')}/v1/write",
            headers=self._headers(),
            json=payload,
            timeout=timeout_s,
        )
        if r.status_code != 200:
            range_hint = None
            if updates and isinstance(updates[0], dict):
                range_hint = str(updates[0].get("range") or "")
            self._raise_http_error(
                op="write",
                status_code=r.status_code,
                sheet_id=sheet_id,
                details=r.text,
                range_hint=range_hint,
            )

        try:
            data = r.json()
        except Exception as exc:
            raise SheetsGatewayError(
                f"write returned non-JSON response for sheet_id={sheet_id}: {r.text}",
                op="write",
                status_code=r.status_code,
                response_body=r.text,
            ) from exc
        if not data.get("ok"):
            body = str(data)
            range_hint = None
            if updates and isinstance(updates[0], dict):
                range_hint = str(updates[0].get("range") or "")
            hint = self._error_hint(r.status_code, body, range_hint=range_hint)
            message = f"write not ok for sheet_id={sheet_id}: {body}"
            if hint:
                message = f"{message} next_action={hint}"
            raise SheetsGatewayError(
                message,
                op="write",
                status_code=r.status_code,
                response_body=body,
                next_action=hint,
            )
        return data
