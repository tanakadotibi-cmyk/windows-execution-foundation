"""rank_observer -> Google Sheets (biz_metric_misc) writer. v1 2026-10-20.
Wraps rank_observer_metrics (unchanged): aggregate+plan via run(write_airtable=False), then
append-only, dedup_key-idempotent write to Sheets. Evidence column dropped by design."""
from __future__ import annotations
import argparse, ctypes, ctypes.wintypes as wt, json, os
from datetime import datetime
from pathlib import Path
from . import rank_observer_metrics as R
from .core import atomic_write_json

SPREADSHEET_ID = "1R_xQV6AYIwRRK1fW-AInwm2rXoThmOFRhNSjgeH8swI"
TAB = "biz_metric_misc"
COLUMNS = ["metric_record_id","business_id","metric_key","metric_label","value","unit","window_start","window_end",
 "observed_at","data_status","confidence","source","dedup_key","metric_class","metric_layer","source_project_id",
 "growth_experiment_id","publication_id","notes","migrated_from","airtable_record_id"]
SECRET = Path(os.environ.get("LOCALAPPDATA", "")) / "TantanManagementOS" / "secrets" / "google-sa-tantan-sheets-writer.dpapi"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets/" + SPREADSHEET_ID

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

def _dpapi_decrypt(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))); out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise R.RankMetricsError("CREDENTIAL_DECRYPT_FAILED")
    try: return ctypes.string_at(out.pbData, out.cbData)
    finally: ctypes.windll.kernel32.LocalFree(out.pbData)

def session():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    info = json.loads(_dpapi_decrypt(SECRET.read_bytes()).decode("utf-8"))
    return AuthorizedSession(service_account.Credentials.from_service_account_info(info, scopes=SCOPES))

def to_row(f: dict, observed_at: str) -> dict:
    return {"metric_record_id": f["Metric Record ID"], "business_id": f["Business ID"], "metric_key": f["Metric Key"],
      "metric_label": f["Metric Label"], "value": f["Value"], "unit": f["Unit"], "window_start": f["Window Start"],
      "window_end": f["Window End"], "observed_at": observed_at, "data_status": f.get("Data Status", ""),
      "confidence": f.get("Confidence", ""), "source": f["Source"], "dedup_key": f["Dedup Key"],
      "metric_class": f["Metric Class"], "metric_layer": f["Metric Layer"], "source_project_id": f["Source Project ID"],
      "growth_experiment_id": "", "publication_id": "", "notes": f["Notes"],
      "migrated_from": "rank_observer_sheets_v1", "airtable_record_id": ""}

def _read(s) -> dict[str, list]:
    r = s.get(f"{API}/values/{TAB}!A1:U", params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=60)
    if r.status_code != 200: raise R.RankMetricsError(f"SHEET_READ_FAILED_{r.status_code}")
    vals = r.json().get("values", [])
    if not vals or vals[0][:len(COLUMNS)] != COLUMNS: raise R.RankMetricsError("SHEET_SCHEMA_DRIFT")
    out = {}
    for v in vals[1:]:
        v = list(v) + [""] * (len(COLUMNS) - len(v)); k = str(v[12])
        if k in out: raise R.RankMetricsError("SHEET_DUPLICATE_DEDUP_KEY")
        out[k] = v
    return out

def _same(a, b) -> bool:
    try: return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError): return str(a) == str(b)

def write_sheet(rows: list[dict], s) -> dict:
    existing = _read(s); new = []; replay = 0
    for r in rows:
        e = existing.get(r["dedup_key"])
        if e is None: new.append(r); continue
        if str(e[0]) != r["metric_record_id"] or not _same(e[4], r["value"]): raise R.RankMetricsError("SHEET_CONTENT_CONFLICT")
        replay += 1
    if new:
        body = {"values": [[r[c] for c in COLUMNS] for r in new]}
        a = s.post(f"{API}/values/{TAB}!A1:U1:append", params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, json=body, timeout=60)
        if a.status_code != 200: raise R.RankMetricsError(f"SHEET_WRITE_FAILED_{a.status_code}")
    back = _read(s)
    if not all(r["dedup_key"] in back and _same(back[r["dedup_key"]][4], r["value"]) for r in rows):
        raise R.RankMetricsError("SHEET_READBACK_FAILED")
    return {"planned": len(rows), "written": len(new), "replay": replay, "readback": len(rows)}

def main() -> int:
    p = R.parser(); p.add_argument("--write-sheets", action="store_true"); a = p.parse_args()
    ev = R.run(a.date, write_airtable=False, acceptance_replay=False, evidence_path=a.evidence, runtime=a.runtime)
    rc = {"schema": "rank_observer_sheets_sync/1.0", "target_date": a.date, "spreadsheet_id": SPREADSHEET_ID, "tab": TAB,
          "plan_status": ev["status"], "status": "FAILED", "failure_code": ev.get("failure_code"), "write_sheets": a.write_sheets,
          "planned": 0, "written": 0, "replay": 0, "readback": 0, "credential_secret_output_count": 0}
    try:
        if ev["status"] == "PASS":
            plan = R.metric_plan(ev["aggregation_contexts"], a.date)
            rows = [to_row(f, datetime.now(R.JST).isoformat(timespec="seconds")) for f in plan]
            rc["planned"] = len(rows)
            if a.write_sheets: rc.update(write_sheet(rows, session()))
            rc["status"] = "PASS"
    except R.RankMetricsError as e: rc["failure_code"] = str(e)
    except Exception as e: rc["failure_code"] = "SHEET_UNEXPECTED_" + type(e).__name__
    rc["completed_at"] = datetime.now(R.JST).isoformat(timespec="seconds")
    atomic_write_json(a.runtime / "sheets-receipts" / f"{a.date.replace('-', '')}.json", rc)
    print(json.dumps(rc, ensure_ascii=False)); return 0 if rc["status"] == "PASS" else 20

if __name__ == "__main__":
    raise SystemExit(main())
