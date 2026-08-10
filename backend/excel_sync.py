"""
Excel (OneDrive/Microsoft Graph) integration for JONAH ENTERPRISES ERP.

The target file (JONAH ENTERPRISES-ERP-2026-FINAL.xlsm) is a real VBA-driven
ERP, audited before this module was written. Key facts this code relies on
(see the audit report for the full read):

  - Every table is a native Excel structured Table (ListObject): TW (work),
    TL (ledger), TP (parties/workers), TS (sites), TI (items).
  - IDs are Node-scoped: existing rows are Node "A" (this master file) or a
    lettered satellite. Node "P" is reserved here exclusively for
    app-generated rows so they can never collide with anything a human types
    in Excel, on this machine or a satellite.
  - Transactional IDs (T_WORK "Work ID", T_LEDGER "Voucher No.") are minted
    from a per-(Doc Type, Node) counter row in SYS_CONFIG!TSER — the desktop
    macro's own NextID() reads the FIRST row matching a Doc Type regardless
    of node, so a dedicated Node "P" row for "WORK"/"LABPAY" is invisible to
    it and safe to add.
  - Master IDs (M_PARTY "Party ID" etc., e.g. WKR-A-0001) are NOT minted by
    any macro in this workbook — they're a plain {Prefix}-{Node}-{0000}
    convention with no live counter anywhere. New Node "P" rows mint their
    own number by scanning the existing column for the highest "-P-####"
    suffix and incrementing.
  - There is no native upsert anywhere in the workbook (AddRow() always
    appends). Idempotent "same app record -> same Excel row" is entirely
    this module's responsibility, keyed off values *this app* wrote into a
    row on a previous sync (tracked in Mongo, not re-derived from Excel).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

import httpx

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
NODE_LETTER = "P"

# The four transactional Doc Types the app ever mints IDs for, and where a
# dedicated Node-P counter row must exist in TSER (created once, on connect).
TRANSACTIONAL_DOC_TYPES = {
    "WORK": {"prefix": "WK", "table": "TW", "id_column": "Work ID"},
    "LABPAY": {"prefix": "LP", "table": "TL", "id_column": "Entry ID"},
}

# Master tables the sync engine can auto-create rows in, and their ID prefix.
MASTER_TABLES = {
    "worker": {"table": "TP", "id_column": "Party ID", "prefix": "WKR"},
    "site": {"table": "TS", "id_column": "Site ID", "prefix": "SITE"},
    "door_type": {"table": "TI", "id_column": "Item ID", "prefix": "SRV"},
}


class GraphAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Graph API error {status_code}: {message}")


# --------------------------------------------------------------------------
# Abstract table store — the sync engine is written against this interface
# only, so it can be exercised in tests with an in-memory fake instead of a
# live Microsoft account.
# --------------------------------------------------------------------------
class ExcelTableStore(Protocol):
    async def get_headers(self, table: str) -> list: ...
    async def get_rows(self, table: str) -> list:  # list[{"index": int, "values": dict[str, Any]}]
        ...
    async def add_row(self, table: str, row: dict) -> int:  # returns new row index
        ...
    async def update_row(self, table: str, row_index: int, row: dict) -> None: ...


# --------------------------------------------------------------------------
# Real Microsoft Graph-backed store
# --------------------------------------------------------------------------
class GraphExcelStore:
    """Talks to one workbook (one OneDrive drive-item) via Microsoft Graph's
    Excel API. No workbook session is used — each call auto-commits, which is
    the simplest correct option for a sync job that runs occasionally rather
    than in a tight interactive loop."""

    def __init__(self, drive_item_id: str, get_access_token: Callable[[], Awaitable[str]]):
        self._item_id = drive_item_id
        self._get_token = get_access_token
        self._headers_cache: dict = {}

    async def _client(self) -> httpx.AsyncClient:
        token = await self._get_token()
        return httpx.AsyncClient(
            base_url=GRAPH_BASE,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30.0,
        )

    def _table_url(self, table: str) -> str:
        return f"/me/drive/items/{self._item_id}/workbook/tables('{table}')"

    async def get_headers(self, table: str) -> list:
        if table in self._headers_cache:
            return self._headers_cache[table]
        async with await self._client() as c:
            r = await c.get(f"{self._table_url(table)}/headerRowRange")
            if r.status_code >= 400:
                raise GraphAPIError(r.status_code, r.text)
            headers = r.json()["values"][0]
        self._headers_cache[table] = headers
        return headers

    async def get_rows(self, table: str) -> list:
        headers = await self.get_headers(table)
        async with await self._client() as c:
            r = await c.get(f"{self._table_url(table)}/rows")
            if r.status_code >= 400:
                raise GraphAPIError(r.status_code, r.text)
            data = r.json().get("value", [])
        out = []
        for row in data:
            vals = row.get("values", [[]])[0]
            mapped = {headers[i]: (vals[i] if i < len(vals) else None) for i in range(len(headers))}
            out.append({"index": row["index"], "values": mapped})
        return out

    async def add_row(self, table: str, row: dict) -> int:
        headers = await self.get_headers(table)
        values = [[row.get(h, "") for h in headers]]
        async with await self._client() as c:
            r = await c.post(f"{self._table_url(table)}/rows/add", json={"values": values})
            if r.status_code >= 400:
                raise GraphAPIError(r.status_code, r.text)
            return r.json()["index"]

    async def update_row(self, table: str, row_index: int, row: dict) -> None:
        headers = await self.get_headers(table)
        existing = await self.get_rows(table)
        current = next((r["values"] for r in existing if r["index"] == row_index), {})
        merged = {**current, **row}
        values = [[merged.get(h, "") for h in headers]]
        async with await self._client() as c:
            r = await c.patch(
                f"{self._table_url(table)}/rows/itemAt(index={row_index})",
                json={"values": values},
            )
            if r.status_code >= 400:
                raise GraphAPIError(r.status_code, r.text)


# --------------------------------------------------------------------------
# OAuth token acquisition (delegated Authorization Code flow, v2.0 endpoint)
# --------------------------------------------------------------------------
async def exchange_code_for_tokens(tenant: str, client_id: str, client_secret: str,
                                    code: str, redirect_uri: str) -> dict:
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "scope": "offline_access Files.ReadWrite",
    }
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(url, data=data)
    if r.status_code >= 400:
        raise GraphAPIError(r.status_code, r.text)
    return r.json()  # {access_token, refresh_token, expires_in, ...}


async def refresh_access_token(tenant: str, client_id: str, client_secret: str, refresh_token: str) -> dict:
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": "offline_access Files.ReadWrite",
    }
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(url, data=data)
    if r.status_code >= 400:
        raise GraphAPIError(r.status_code, r.text)
    return r.json()


async def resolve_file_by_path(get_access_token: Callable[[], Awaitable[str]], file_path: str) -> dict:
    """file_path like '/Documents/JONAH ENTERPRISES-ERP-2026-FINAL.xlsm', relative
    to the signed-in user's OneDrive root. Returns the drive item {id, name, webUrl}."""
    token = await get_access_token()
    clean_path = file_path.strip().lstrip("/")
    async with httpx.AsyncClient(
        base_url=GRAPH_BASE, headers={"Authorization": f"Bearer {token}"}, timeout=20.0
    ) as c:
        r = await c.get(f"/me/drive/root:/{clean_path}")
    if r.status_code >= 400:
        raise GraphAPIError(r.status_code, r.text)
    return r.json()


# --------------------------------------------------------------------------
# ID minting
# --------------------------------------------------------------------------
_MASTER_ID_RE_CACHE: dict = {}


def _master_id_regex(prefix: str) -> re.Pattern:
    if prefix not in _MASTER_ID_RE_CACHE:
        _MASTER_ID_RE_CACHE[prefix] = re.compile(rf"^{re.escape(prefix)}-{NODE_LETTER}-(\d+)$")
    return _MASTER_ID_RE_CACHE[prefix]


async def mint_master_id(store: ExcelTableStore, kind: str) -> str:
    """WKR-P-0001 style — scans the live column for the highest existing
    Node-P number and increments. No live counter exists for these anywhere
    in the workbook (verified: no macro mints Party/Site/Item IDs), so the
    column itself is the only source of truth."""
    cfg = MASTER_TABLES[kind]
    rows = await store.get_rows(cfg["table"])
    rx = _master_id_regex(cfg["prefix"])
    highest = 0
    for r in rows:
        val = str(r["values"].get(cfg["id_column"]) or "")
        m = rx.match(val)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{cfg['prefix']}-{NODE_LETTER}-{highest + 1:04d}"


async def ensure_tser_node_p_rows(store: ExcelTableStore, fy_label: str) -> None:
    """One-time setup (safe to call repeatedly): make sure SYS_CONFIG!TSER has a
    Node-P counter row for each transactional doc type the app mints. Desktop
    NextID() takes the FIRST row matching a Doc Type regardless of node, so
    these additions are invisible to it as long as the existing Node-A rows
    stay first — Graph's rows/add always appends, which preserves that."""
    rows = await store.get_rows("TSER")
    have = {(r["values"].get("Doc Type"), r["values"].get("Node")) for r in rows}
    for doc_type, cfg in TRANSACTIONAL_DOC_TYPES.items():
        if (doc_type, NODE_LETTER) in have:
            continue
        await store.add_row("TSER", {
            "Doc Type": doc_type,
            "Prefix": cfg["prefix"],
            "Node": NODE_LETTER,
            "FY": fy_label,
            "Next Number": 1,
            "Example": f"{cfg['prefix']}/{fy_label.replace('FY ', '')}/{NODE_LETTER}/0001",
            "Description": f"{doc_type} row created by the app (Node {NODE_LETTER})",
        })


async def mint_ledger_entry_id(store: ExcelTableStore) -> str:
    """T_LEDGER's 'Entry ID' (e.g. LE-A-00001) is a per-row primary key in a
    different, simpler format than the Voucher No. NextID() mints — 5-digit,
    no FY segment. Like the master-table IDs, no live macro mints it (only
    AddRow's generic Row GUID/Created By/On are automatic), so the column
    itself is the source of truth, exactly like mint_master_id."""
    rows = await store.get_rows("TL")
    rx = re.compile(rf"^LE-{NODE_LETTER}-(\d+)$")
    highest = 0
    for r in rows:
        m = rx.match(str(r["values"].get("Entry ID") or ""))
        if m:
            highest = max(highest, int(m.group(1)))
    return f"LE-{NODE_LETTER}-{highest + 1:05d}"


async def mint_transactional_id(store: ExcelTableStore, doc_type: str, fy_label: str) -> str:
    cfg = TRANSACTIONAL_DOC_TYPES[doc_type]
    rows = await store.get_rows("TSER")
    match = next(
        (r for r in rows if r["values"].get("Doc Type") == doc_type and r["values"].get("Node") == NODE_LETTER),
        None,
    )
    fy_short = fy_label.replace("FY ", "").strip()
    if match is None:
        await ensure_tser_node_p_rows(store, fy_label)
        rows = await store.get_rows("TSER")
        match = next(
            r for r in rows
            if r["values"].get("Doc Type") == doc_type and r["values"].get("Node") == NODE_LETTER
        )
    next_num = int(match["values"].get("Next Number") or 1)
    candidate = f"{cfg['prefix']}/{fy_short}/{NODE_LETTER}/{next_num:04d}"
    await store.update_row("TSER", match["index"], {"Next Number": next_num + 1})
    return candidate


# --------------------------------------------------------------------------
# Matching helpers used by the admin "Excel Master Mapping" screen and the
# sync engine's auto-create path.
# --------------------------------------------------------------------------
async def find_row_by_id(store: ExcelTableStore, table: str, id_column: str, id_value: str) -> Optional[dict]:
    if not id_value:
        return None
    for r in await store.get_rows(table):
        if str(r["values"].get(id_column) or "").strip() == str(id_value).strip():
            return r
    return None
