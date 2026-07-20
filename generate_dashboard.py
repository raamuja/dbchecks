#!/usr/bin/env python3
"""
generate_dashboard.py
----------------------
Parses a Sybase db_size.txt file (one or more server sections, each with
DB Name / DB Size Alloted / DB Size Used / Log Size Alloted / Log Size Used)
and renders it into the HTML capacity dashboard.

Usage:
    python3 generate_dashboard.py <input_file> <output_html>

Exit codes:
    0  success
    1  input file missing / unreadable
    2  no valid data rows parsed
"""

import sys
import re
import json
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _CET = ZoneInfo("CET")
except Exception:
    # Fallback if the 'tzdata' package isn't installed on this host.
    # Uses a fixed UTC+1 offset (CET without daylight-saving adjustment).
    _CET = timezone(timedelta(hours=1), name="CET")


def now_cet_str():
    return datetime.now(_CET).strftime("%Y-%m-%d %H:%M:%S %Z")

DATA_ROW_RE = re.compile(r'^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$')
SKIP_PREFIXES = ("Database", "(")

# Maps raw section header text (as it appears in db_size.txt) to the display
# name shown on the dashboard. Matching is case-insensitive and ignores
# surrounding whitespace. Section headers not listed here are shown as-is,
# so new/unknown servers still render fine without needing a code change.
SERVER_NAME_MAP = {
    "MAIN UAT": "PBL1_DATA_UAT_SQL",
    "BACK UAT": "PBL1_BACK_UAT_SQL",
    "VAULT UAT": "PBL1_VAUL_UAT_SQL",
}


def display_name(raw_name):
    key = raw_name.strip().upper()
    return SERVER_NAME_MAP.get(key, raw_name)


def parse_file(path):
    """
    Returns (ase_sections, replication_sections, iq_sections).

    ase_sections: list of {"name": server_name, "rows": [...]} dicts (unchanged
    behaviour from before - DB/Log size sections).

    replication_sections: list of {"name": server_name, "rows": [...]} dicts,
    one per replication server block found under a "UAT REPLICATION" marker
    line. Each row is {"dsi": str, "queue": int|None, "agentStatus": str|None}.

    iq_sections: list of {"name": server_name, "rows": [...]} dicts, one per
    IQ server block found under a "UAT IQ" marker line. Each row is
    {"process": str, "usage": int|None, "transactions": str|None} - process
    names can contain spaces (e.g. "Long Running"), so these lines are split
    on tabs rather than generic whitespace.

    The input file may optionally have "UAT ASE" / "UAT REPLICATION" / "UAT IQ"
    marker lines. If none are present, the whole file is parsed as ASE data
    (fully backward compatible with older db_size.txt files).
    """
    ase_sections = []
    replication_sections = []
    iq_sections = []
    current_ase = None
    current_rep = None
    current_iq = None

    mode = "ase"

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\r\n")
            stripped = line.strip()
            if not stripped:
                continue

            upper = stripped.upper()
            if upper == "UAT ASE":
                mode = "ase"
                continue
            if upper == "UAT REPLICATION":
                mode = "replication"
                continue
            if upper == "UAT IQ":
                mode = "iq"
                continue

            if mode == "ase":
                if stripped.startswith(SKIP_PREFIXES):
                    continue
                m = DATA_ROW_RE.match(stripped)
                if m:
                    if current_ase is None:
                        current_ase = {"name": "UNSPECIFIED SERVER", "rows": []}
                        ase_sections.append(current_ase)
                    name, dba, dbu, lga, lgu = m.groups()
                    current_ase["rows"].append({
                        "name": name,
                        "dbAlloc": int(dba),
                        "dbUsed": int(dbu),
                        "logAlloc": int(lga),
                        "logUsed": int(lgu),
                    })
                else:
                    current_ase = {"name": display_name(stripped), "rows": []}
                    ase_sections.append(current_ase)

            elif mode == "replication":
                if stripped.upper().startswith("DSI"):
                    continue
                tokens = stripped.split()
                if len(tokens) == 1:
                    # New replication server name line, e.g. "PB1_REP1_UAT_SQL"
                    current_rep = {"name": tokens[0], "rows": []}
                    replication_sections.append(current_rep)
                elif len(tokens) >= 2:
                    if current_rep is None:
                        current_rep = {"name": "UNSPECIFIED REPLICATION SERVER", "rows": []}
                        replication_sections.append(current_rep)
                    dsi_name = tokens[0]
                    last = tokens[-1]
                    if last.isdigit():
                        current_rep["rows"].append({
                            "dsi": dsi_name, "queue": int(last), "agentStatus": None
                        })
                    else:
                        current_rep["rows"].append({
                            "dsi": dsi_name, "queue": None, "agentStatus": last
                        })

            else:  # mode == "iq"
                if stripped.upper().startswith("PROCESS"):
                    continue
                fields = [f.strip() for f in line.split("\t") if f.strip()]
                if len(fields) == 1:
                    # New IQ server name line, e.g. "PB1_IQ_WRITER"
                    current_iq = {"name": fields[0], "rows": []}
                    iq_sections.append(current_iq)
                elif len(fields) >= 2:
                    if current_iq is None:
                        current_iq = {"name": "UNSPECIFIED IQ SERVER", "rows": []}
                        iq_sections.append(current_iq)
                    process_name = fields[0]
                    last = fields[-1]
                    if last.isdigit():
                        current_iq["rows"].append({
                            "process": process_name, "usage": int(last), "transactions": None
                        })
                    else:
                        current_iq["rows"].append({
                            "process": process_name, "usage": None, "transactions": last
                        })

    return ase_sections, replication_sections, iq_sections


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Sybase DB &amp; Log Size Monitor</title>
<style>
  :root{
    --bg:#eef1f6;
    --panel:#ffffff;
    --panel-border:#dbe1e9;
    --grid-line:#e8ecf1;
    --text:#212936;
    --text-dim:#5b6472;
    --text-faint:#98a1ad;
    --accent:#0f766e;
    --accent-dim:#a9d3cd;
    --healthy:#15803d;
    --healthy-bg:rgba(21,128,61,0.10);
    --healthy-muted:#4f8f68;
    --warning:#713f12;
    --warning-bg:rgba(113,63,18,0.15);
    --critical:#991b1b;
    --critical-bg:rgba(153,27,27,0.11);
    --warning-strong:#C2410C;
    --critical-strong:#8B0000;
    --mono: "SF Mono","Consolas","Liberation Mono", monospace;
  }
  *{box-sizing:border-box;}
  html{ scroll-behavior:smooth; }
  body{
    margin:0;
    background:
      radial-gradient(1200px 500px at 15% -10%, rgba(15,118,110,0.05), transparent 60%),
      radial-gradient(900px 500px at 100% 0%, rgba(113,63,18,0.04), transparent 55%),
      var(--bg);
    color:var(--text);
    font-family: "Inter","Segoe UI",Helvetica,Arial,sans-serif;
    padding:6px 8px 4px;
    min-height:100vh;
  }
  header.top-bar{
    display:flex;
    flex-direction:row;
    align-items:center;
    justify-content:center;
    width:100%;
    position:relative;
    border-bottom:1px solid var(--panel-border);
    padding-bottom:4px;
    margin-bottom:6px;
  }
  .page-title-box{
    display:grid;
    grid-template-columns:1fr auto 1fr;
    align-items:center;
    gap:18px;
    width:100%;
    padding:5px 20px;
    border:1px solid var(--panel-border);
    border-radius:10px;
    background:linear-gradient(180deg, #ffffff, #f4f7fa);
    box-shadow:0 4px 14px rgba(15,23,42,0.06), inset 0 1px 0 rgba(255,255,255,0.6);
  }
  .title-spacer-left{
    min-width:0;
  }
  .title-right-group{
    display:flex;
    align-items:center;
    gap:10px;
    justify-self:end;
    flex-wrap:nowrap;
  }
  .theme-toggle{
    justify-self:start;
  }
  .generated-badge{
    justify-self:end;
  }
  .theme-toggle{
    display:flex;
    gap:4px;
    flex-shrink:0;
  }
  .theme-btn{
    font-family:"Inter",sans-serif;
    font-size:12px;
    font-weight:700;
    color:var(--text-dim);
    background:#f2f5f8;
    border:1px solid var(--panel-border);
    border-radius:6px;
    padding:5px 9px;
    cursor:pointer;
    white-space:nowrap;
  }
  .theme-btn.active{
    color:#ffffff;
    background:var(--accent);
    border-color:var(--accent);
  }
  .dash-filter{
    display:flex;
    align-items:center;
    gap:6px;
    flex:0 0 auto;
    white-space:nowrap;
  }
  .dash-filter .filter-pointer{
    font-size:56px;
    line-height:1;
    display:inline-block;
    transform:translateY(-6px);
    color:#1f2937;
    animation:pointer-blink 0.6s ease-in-out infinite;
  }
  @keyframes pointer-blink{
    0%, 100% { opacity:1; text-shadow:0 0 3px rgba(31,41,55,0.9), 0 0 8px rgba(31,41,55,0.5); }
    50% { opacity:0.4; text-shadow:0 0 1px rgba(31,41,55,0.3); }
  }
  @media (prefers-reduced-motion: reduce){
    .dash-filter .filter-pointer{ animation:none; text-shadow:0 0 3px rgba(31,41,55,0.7); }
  }
  .select-glow-wrap{
    display:inline-block;
    border-radius:6px;
    animation:dropdown-glow 0.6s ease-in-out infinite;
  }
  .dash-filter select{
    font-family:"Inter",sans-serif;
    font-size:22px;
    font-weight:700;
    color:var(--accent);
    background:#ffffff;
    border:1px solid var(--panel-border);
    border-radius:6px;
    padding:4px 12px;
    cursor:pointer;
    display:block;
  }
  @keyframes dropdown-glow{
    0%, 100% { box-shadow:0 0 3px 1px rgba(15,118,110,0.55), 0 0 8px 2px rgba(15,118,110,0.35); border-color:rgba(15,118,110,0.6); }
    50% { box-shadow:0 0 0 0 rgba(15,118,110,0); border-color:var(--panel-border); }
  }
  @media (prefers-reduced-motion: reduce){
    .select-glow-wrap{ animation:none; box-shadow:0 0 3px 1px rgba(15,118,110,0.5); }
  }
  .dash-filter select:focus{
    outline:2px solid var(--accent);
    outline-offset:1px;
  }
  tr.row-hidden{ display:none !important; }
  .card-hidden{ display:none !important; }
  .page-title-box h1{
    flex:1 1 auto;
    margin:0;
    text-align:center;
    font-family:var(--mono);
    font-weight:700;
    letter-spacing:0.02em;
    text-transform:uppercase;
    color:var(--accent);
    font-size:clamp(15px, 1.9vw, 24px);
    white-space:nowrap;
  }
  .legend{
    display:flex;
    justify-content:center;
    gap:14px;
    font-family:var(--mono);
    font-size:13px;
    font-weight:700;
    color:var(--text-dim);
    flex-shrink:0;
  }
  .legend span{
    display:flex;
    align-items:center;
    gap:5px;
    white-space:nowrap;
    padding:2px 8px;
    border-radius:14px;
  }
  .legend span.legend-healthy{ color:var(--healthy); background:var(--healthy-bg); }
  .legend span.legend-warning{ color:var(--warning); background:var(--warning-bg); }
  .legend span.legend-critical{ color:var(--critical); background:var(--critical-bg); }
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;}
  .dot.healthy{background:var(--healthy);box-shadow:0 0 0 3px rgba(21,128,61,0.16);}
  .dot.warning{background:var(--warning);box-shadow:0 0 0 3px rgba(113,63,18,0.24);}
  .dot.critical{background:var(--critical);box-shadow:0 0 0 3px rgba(153,27,27,0.24);}

  .section-title-wrap{
    text-align:center;
    margin-bottom:5px;
  }
  .section-title-box{
    display:inline-block;
    padding:4px 16px;
    border:1px solid var(--panel-border);
    border-radius:8px;
    background:var(--panel);
    box-shadow:0 3px 10px rgba(15,23,42,0.06);
  }
  .section-title-box h2{
    margin:0;
    text-align:center;
    font-size:clamp(14px, 1.5vw, 18px);
    font-weight:700;
    letter-spacing:0.02em;
    color:var(--accent);
    white-space:nowrap;
  }
  .section-title-box h2.ase-summary-title{
    color:#0f766e;
    text-align:center;
  }
  .section-title-box h2.rep-summary-title{
    color:#92400e;
    text-align:center;
  }
  .section-title-box h2.iq-summary-title{
    color:#6d28d9;
    text-align:center;
  }
  .wide-header-wrap{
    margin-top:12px;
    margin-bottom:6px;
    scroll-margin-top:52px;
  }
  .wide-header-box{
    display:flex;
    align-items:center;
    justify-content:space-between;
    flex-wrap:wrap;
    gap:16px;
    width:100%;
    padding:3px 10px;
    border:1px solid var(--panel-border);
    border-radius:8px;
    background:var(--panel);
    box-shadow:0 3px 10px rgba(15,23,42,0.06);
  }
  .wide-header-box h2{
    text-align:left;
    flex:1 1 auto;
    margin:0;
    white-space:nowrap;
  }
  .wide-header-box .legend{
    justify-content:flex-end;
    font-size:clamp(11px, 1.1vw, 14px);
  }
  .wide-header-box.ase-header-box{
    background:#e8f6f4;
    border:1px solid #a9d3cd;
  }
  .wide-header-box.rep-header-box{
    background:#fdf1e0;
    border:1px solid #ecc38a;
  }
  .wide-header-box.iq-header-box{
    background:#f1ecfb;
    border:1px solid #cdb8f5;
  }
  .rep-condition-legend{
    display:flex;
    justify-content:flex-end;
    gap:14px;
    font-family:var(--mono);
    font-size:clamp(11px, 1.1vw, 14px);
    font-weight:700;
    flex-wrap:wrap;
    flex-shrink:0;
  }
  .rep-condition-legend span{
    display:flex;
    align-items:center;
    gap:5px;
    white-space:nowrap;
    padding:2px 8px;
    border-radius:14px;
  }
  .rep-condition-legend .dot{width:8px;height:8px;border-radius:50%;display:inline-block;}
  .rep-condition-legend .legend-warning{ color:var(--warning); background:var(--warning-bg); }
  .rep-condition-legend .legend-critical{ color:var(--critical); background:var(--critical-bg); }
  .iq-condition-legend{
    display:flex;
    justify-content:flex-end;
    gap:14px;
    font-family:var(--mono);
    font-size:clamp(11px, 1.1vw, 14px);
    font-weight:700;
    flex-wrap:wrap;
    flex-shrink:0;
  }
  .iq-condition-legend span{
    display:flex;
    align-items:center;
    gap:5px;
    white-space:nowrap;
    padding:2px 8px;
    border-radius:14px;
  }
  .iq-condition-legend .dot{width:8px;height:8px;border-radius:50%;display:inline-block;}
  .iq-condition-legend .legend-warning{ color:var(--warning); background:var(--warning-bg); }
  .iq-condition-legend .legend-critical{ color:var(--critical); background:var(--critical-bg); }
  .summary-grid{
    display:grid;
    grid-template-columns:repeat(2, 1fr);
    gap:12px;
    margin-bottom:8px;
    align-items:stretch;
  }
  @media (max-width:900px){
    .summary-grid{ grid-template-columns:1fr !important; }
  }
  .summary-card{
    background:linear-gradient(180deg, rgba(15,118,110,0.07), rgba(15,118,110,0.02));
    border:1px solid rgba(15,118,110,0.22);
    border-radius:8px;
    overflow:hidden;
    display:flex;
    flex-direction:column;
    height:100%;
    box-sizing:border-box;
  }
  .summary-card-head{
    display:flex;
    align-items:center;
    justify-content:center;
    padding:3px 10px;
    border-bottom:1px solid rgba(15,118,110,0.18);
    background:rgba(15,118,110,0.07);
  }
  .summary-card-head .server-tag{
    margin:0;
    border:none;
    background:none;
    padding:0;
    font-size:13.5px;
  }
  .replication-card-head .server-tag{
    margin:0;
    border:none;
    background:none;
    padding:0;
    font-size:13.5px;
    color:#92400e;
  }
  .summary-list{
    list-style:none;
    margin:0;
    padding:0;
  }
  .summary-table{
    width:100%;
    table-layout:fixed;
    border-collapse:collapse;
    font-size:13px;
  }
  .summary-table col.col-slno{ width:9%; }
  .summary-table col.col-name{ width:13%; }
  .summary-table col.col-data{ width:29%; }
  .summary-table col.col-log{ width:29%; }
  .summary-table col.col-status{ width:20%; }
  .summary-table thead th{
    background:#f2f5f8;
    color:var(--text);
    font-family:"Inter",sans-serif;
    font-weight:700;
    font-size:11px;
    letter-spacing:0;
    text-transform:uppercase;
    padding:4px 5px;
    text-align:center;
    line-height:1.3;
    border-bottom:1px solid #000000;
    border-right:1px solid #000000;
    white-space:normal;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .summary-table thead th:last-child{ border-right:1px solid #000000; }
  .summary-table tbody td{
    padding:5px 6px;
    border-bottom:1px solid #000000;
    border-right:1px solid #000000;
    font-family:"Inter",sans-serif;
    font-size:12.5px;
    text-align:center;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .summary-table tbody td:last-child{ border-right:1px solid #000000; }
  .summary-table{ border:1px solid #000000; }
  .summary-table tbody tr:last-child td{ border-bottom:1px solid #000000; }
  .summary-table tbody tr:hover td{ background:rgba(15,23,42,0.03); }
  .summary-table .summary-dbname{
    font-family:"Inter",sans-serif;
    font-weight:600;
    font-size:13px;
    letter-spacing:0.01em;
    color:#2b6b62;
    text-align:center;
  }
  .summary-slno{
    text-align:center;
    color:var(--text-dim);
    font-weight:600;
  }
  .summary-pctcell{
    text-align:center;
    font-weight:600;
    white-space:nowrap;
  }
  .summary-pctcell.healthy{ color:var(--healthy-muted); }
  .summary-pctcell.warning{ color:var(--warning); }
  .summary-pctcell.critical{ color:var(--critical); }

  /* Solid dark-orange / dark-red fill with white text -- applied ONLY to the
     STATUS column badges (e.g. "✗ Critical" / "⚠ Warning"), never to the
     numeric DATA/LOG/QUEUE/USAGE value cells, which keep their original color. */
  .summary-status-col.warning, .rep-status-col.warning, .iq-status-col.warning{
    color:#ffffff;
    background:var(--warning-strong);
    border-radius:4px;
    padding:1px 6px;
  }
  .summary-status-col.critical, .rep-status-col.critical, .iq-status-col.critical{
    color:#ffffff;
    background:var(--critical-strong);
    border-radius:4px;
    padding:1px 6px;
  }
  .summary-status-col{ text-align:center; padding:9px 6px; font-weight:700; }
  .summary-status-col.healthy{ color:var(--healthy-muted); }
  .badge.badge-sm{
    padding:3px 7px;
    font-size:11px;
    gap:4px;
  }
  .badge.badge-sm .dot{ width:5px; height:5px; }
  .summary-empty{
    flex:1;
    display:flex;
    align-items:center;
    justify-content:center;
    padding:10px 12px 4px;
    text-align:center;
    font-family:"Inter",sans-serif;
    font-weight:600;
    font-size:13px;
    color:var(--healthy);
  }

  .replication-grid{
    display:grid;
    gap:12px;
    margin-bottom:8px;
    align-items:stretch;
  }
  @media (max-width:900px){
    .replication-grid{ grid-template-columns:1fr !important; }
  }
  .replication-card{
    background:linear-gradient(180deg, rgba(113,63,18,0.08), rgba(113,63,18,0.02));
    border:1px solid rgba(113,63,18,0.22);
    border-radius:8px;
    overflow:hidden;
    display:flex;
    flex-direction:column;
    height:100%;
    box-sizing:border-box;
  }
  .replication-card-head{
    display:flex;
    align-items:center;
    justify-content:center;
    padding:3px 10px;
    border-bottom:1px solid rgba(113,63,18,0.18);
    background:rgba(113,63,18,0.07);
  }
  .replication-table{
    width:100%;
    table-layout:fixed;
    border-collapse:collapse;
    font-size:13px;
  }
  .replication-table col.col-dsi{ width:38%; }
  .replication-table col.col-queue{ width:22%; }
  .replication-table col.col-agent{ width:20%; }
  .replication-table col.col-status{ width:20%; }
  .replication-table thead th{
    background:#f2f5f8;
    color:var(--text);
    font-family:"Inter",sans-serif;
    font-weight:700;
    font-size:11px;
    letter-spacing:0;
    text-transform:uppercase;
    padding:4px 5px;
    text-align:center;
    line-height:1.3;
    border-bottom:1px solid #000000;
    border-right:1px solid #000000;
    white-space:normal;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .replication-table thead th:last-child{ border-right:1px solid #000000; }
  .replication-table tbody td{
    padding:5px 6px;
    border-bottom:1px solid #000000;
    border-right:1px solid #000000;
    font-family:"Inter",sans-serif;
    font-size:12.5px;
    text-align:center;
    color:var(--text);
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .replication-table tbody td:last-child{ border-right:1px solid #000000; }
  .replication-table{ border:1px solid #000000; }
  .replication-table tbody tr:last-child td{ border-bottom:1px solid #000000; }
  .replication-table tbody tr:hover td{ background:rgba(15,23,42,0.03); }
  .rep-dsi{ font-weight:600; letter-spacing:0.01em; color:#8a6a3f; text-align:center; }
  .rep-queue{ font-weight:600; }
  .rep-queue.healthy{ color:var(--healthy-muted); }
  .rep-queue.warning{ color:var(--warning); }
  .rep-queue.critical{ color:var(--critical); }
  .rep-queue.dash{ color:var(--text-faint); font-weight:400; }
  .rep-agent{ font-weight:600; }
  .rep-agent.down{ color:var(--critical); }
  .rep-agent.up{ color:var(--healthy-muted); }
  .rep-agent.dash{ color:var(--text-faint); font-weight:400; }
  .rep-status-col{ font-weight:700; }
  .rep-status-col.healthy{ color:var(--healthy-muted); }
  .replication-empty{
    flex:1;
    display:flex;
    align-items:center;
    justify-content:center;
    padding:10px 12px 4px;
    text-align:center;
    font-family:"Inter",sans-serif;
    font-weight:600;
    font-size:13px;
    color:var(--healthy);
  }

  .iq-grid{
    display:grid;
    gap:12px;
    margin-bottom:4px;
    align-items:stretch;
  }
  @media (max-width:900px){
    .iq-grid{ grid-template-columns:1fr !important; }
  }
  .iq-card{
    background:linear-gradient(180deg, rgba(109,40,217,0.08), rgba(109,40,217,0.02));
    border:1px solid rgba(109,40,217,0.25);
    border-radius:8px;
    overflow:hidden;
    display:flex;
    flex-direction:column;
    height:100%;
    box-sizing:border-box;
  }
  .iq-card-head{
    display:flex;
    align-items:center;
    justify-content:center;
    padding:3px 10px;
    border-bottom:1px solid rgba(109,40,217,0.2);
    background:rgba(109,40,217,0.08);
  }
  .iq-card-head .server-tag{
    margin:0;
    border:none;
    background:none;
    padding:0;
    font-size:13.5px;
    color:#6d28d9;
  }
  .iq-table{
    width:100%;
    table-layout:fixed;
    border-collapse:collapse;
    font-size:13px;
  }
  .iq-table col.col-process{ width:38%; }
  .iq-table col.col-usage{ width:22%; }
  .iq-table col.col-transactions{ width:20%; }
  .iq-table col.col-status{ width:20%; }
  .iq-table thead th{
    background:#f2f5f8;
    color:var(--text);
    font-family:"Inter",sans-serif;
    font-weight:700;
    font-size:11px;
    letter-spacing:0;
    text-transform:uppercase;
    padding:4px 5px;
    text-align:center;
    line-height:1.3;
    border-bottom:1px solid #000000;
    border-right:1px solid #000000;
    white-space:normal;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .iq-table thead th:last-child{ border-right:1px solid #000000; }
  .iq-table tbody td{
    padding:5px 6px;
    border-bottom:1px solid #000000;
    border-right:1px solid #000000;
    font-family:"Inter",sans-serif;
    font-size:12.5px;
    text-align:center;
    color:var(--text);
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .iq-table tbody td:last-child{ border-right:1px solid #000000; }
  .iq-table{ border:1px solid #000000; }
  .iq-table tbody tr:last-child td{ border-bottom:1px solid #000000; }
  .iq-table tbody tr:hover td{ background:rgba(15,23,42,0.03); }
  .iq-process{ font-weight:600; letter-spacing:0.01em; color:#6b5f8a; text-align:center; }
  .iq-usage{ font-weight:600; }
  .iq-usage.healthy{ color:var(--healthy-muted); }
  .iq-usage.warning{ color:var(--warning); }
  .iq-usage.critical{ color:var(--critical); }
  .iq-usage.dash{ color:var(--text-faint); font-weight:400; }
  .iq-txn{ font-weight:600; }
  .iq-txn.yes{ color:var(--warning); }
  .iq-txn.no{ color:var(--healthy-muted); }
  .iq-txn.dash{ color:var(--text-faint); font-weight:400; }
  .iq-status-col{ font-weight:700; }
  .iq-status-col.healthy{ color:var(--healthy-muted); }
  .iq-empty{
    flex:1;
    display:flex;
    align-items:center;
    justify-content:center;
    padding:10px 12px 4px;
    text-align:center;
    font-family:"Inter",sans-serif;
    font-weight:600;
    font-size:13px;
    color:var(--healthy);
  }

  .status-count-wrap{
    margin-bottom:6px;
    overflow-x:auto;
  }
  #status-count-summary{
    display:flex;
    justify-content:center;
    align-items:stretch;
    gap:16px;
    flex-wrap:nowrap;
    width:100%;
    margin:14px auto 0;
  }
  .status-count-card{
    display:inline-block;
    flex:1 1 0;
    min-width:0;
  }
  .status-count-table{
    width:100%;
    border-collapse:collapse;
    font-family:"Inter",sans-serif;
    font-size:12px;
    box-shadow:0 4px 14px rgba(15,23,42,0.08);
  }
  .status-count-table thead th.count-head-category{
    font-weight:800;
    font-size:14px;
    letter-spacing:0.02em;
    padding:4px 10px;
    border:1px solid #d5dbe3;
    text-align:center;
  }
  .status-count-table thead th.count-head-category.cat-ase{ background:#0f766e; color:#ffffff; }
  .status-count-table thead th.count-head-category.cat-rep{ background:#713f12; color:#ffffff; }
  .status-count-table thead th.count-head-category.cat-iq{ background:#6d28d9; color:#ffffff; }
  .status-count-table thead th.count-sub-head{
    font-weight:700;
    font-size:11px;
    padding:2px 10px;
    border:1px solid #d5dbe3;
    text-align:left;
  }
  .status-count-table thead th.count-sub-head.cat-ase{ background:#0f766e; color:#ffffff; }
  .status-count-table thead th.count-sub-head.cat-rep{ background:#713f12; color:#ffffff; }
  .status-count-table thead th.count-sub-head.cat-iq{ background:#6d28d9; color:#ffffff; }
  .status-count-table tbody td{
    padding:3px 10px;
    border:1px solid #d5dbe3;
    text-align:left;
    font-weight:700;
    font-size:13px;
    color:#212936;
  }
  .status-count-table .count-status-cell.healthy,
  .status-count-table .count-value-cell.healthy{ background:#dcfce7; color:#15803d; }
  .status-count-table .count-status-cell.warning,
  .status-count-table .count-value-cell.warning{ background:#fef3c7; color:var(--warning-strong); }
  .status-count-table .count-status-cell.critical,
  .status-count-table .count-value-cell.critical{ background:#fee2e2; color:var(--critical-strong); }
  .status-count-table .count-value-cell{
    text-align:center;
  }

  .server-block{ margin-bottom:0; min-width:0; }
  .dashboard-grid{
    display:grid;
    grid-template-columns:repeat(2, 1fr);
    gap:22px;
    align-items:start;
  }
  @media (max-width:1100px){
    .dashboard-grid{ grid-template-columns:1fr; }
  }
  .server-head{
    display:flex;
    align-items:center;
    justify-content:center;
    gap:12px;
    margin-bottom:18px;
  }
  .server-tag{
    font-family:"Inter",sans-serif;
    font-size:13px;
    font-weight:700;
    letter-spacing:0.06em;
    color:var(--accent);
    border:1px solid var(--accent-dim);
    background:rgba(15,118,110,0.06);
    padding:4px 10px;
    border-radius:3px;
  }
  .server-head h2{
    font-size:18.5px;
    margin:0 0 0 auto;
    color:#1c2530;
    font-weight:600;
    letter-spacing:0.01em;
    text-align:right;
  }
  .server-head .count{
    font-family:var(--mono);
    font-size:13px;
    color:var(--text-faint);
  }

  .panel{
    background:var(--panel);
    border:1px solid var(--panel-border);
    border-radius:8px;
    overflow-x:hidden;
  }
  table{
    width:100%;
    table-layout:fixed;
    border-collapse:collapse;
    font-size:12.5px;
  }
  thead tr.grp th{
    background:#f2f5f8;
    color:var(--text-dim);
    font-family:"Inter",sans-serif;
    font-weight:700;
    font-size:13px;
    letter-spacing:0.02em;
    text-transform:uppercase;
    padding:9px 4px 6px;
    text-align:center;
    border-bottom:1px solid var(--grid-line);
  }
  thead tr.grp th.db-grp{ color:#0f766e; border-left:1px solid var(--panel-border);}
  thead tr.grp th.log-grp{ color:#92400e; border-left:1px solid var(--panel-border);}
  thead tr.sub th{
    background:#f2f5f8;
    color:var(--text);
    font-family:"Inter",sans-serif;
    font-weight:700;
    font-size:10.5px;
    letter-spacing:0.01em;
    text-transform:uppercase;
    padding:4px 3px 6px;
    text-align:center;
    line-height:1.3;
    border-bottom:1px solid var(--panel-border);
    white-space:normal;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  thead tr.sub th.div-left{ border-left:1px solid var(--panel-border); }

  tbody td{
    padding:9px 5px;
    border-bottom:1px solid var(--grid-line);
    font-family:"Inter",sans-serif;
    font-size:12.5px;
    text-align:center;
    color:var(--text);
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  tbody tr:last-child td{ border-bottom:none; }
  tbody tr:hover td{ background:rgba(15,23,42,0.03); }
  td.dbname{
    text-align:center;
    font-family:"Inter",sans-serif;
    font-weight:600;
    color:#1c2530;
    letter-spacing:0.01em;
  }
  td.slno{
    text-align:center;
    color:var(--text-dim);
    font-weight:600;
  }
  td.div-left{ border-left:1px solid var(--panel-border); }
  td.pct{ color:var(--text-dim); }
  .pct-val{ font-weight:600; color:inherit; }
  td.pct.healthy .pct-val{ color:var(--healthy-muted); }
  td.pct.warning .pct-val{ color:var(--warning); }
  td.pct.critical .pct-val{ color:var(--critical); }
  td.status-col{ text-align:center; padding:8px 6px; }

  .badge{
    display:inline-flex;
    align-items:center;
    gap:4px;
    font-family:"Inter",sans-serif;
    font-size:11px;
    font-weight:600;
    letter-spacing:0.01em;
    padding:3px 8px;
    border-radius:20px;
    white-space:nowrap;
  }
  .badge.healthy{ color:var(--healthy-muted); background:rgba(21,128,61,0.07); border:1px solid rgba(21,128,61,0.2);}
  .badge.warning{ color:var(--warning); background:var(--warning-bg); border:1px solid rgba(113,63,18,0.3);}
  .badge.critical{ color:var(--critical); background:var(--critical-bg); border:1px solid rgba(153,27,27,0.3);}
  .badge .dot{width:6px;height:6px;}

  .bar-track{
    position:relative;
    width:64px;
    height:5px;
    border-radius:3px;
    background:#e5e9ee;
    display:inline-block;
    margin-right:8px;
    vertical-align:middle;
  }
  .bar-fill{
    position:absolute; left:0; top:0; height:100%; border-radius:3px;
  }
  .bar-fill.healthy{ background:var(--healthy); }
  .bar-fill.warning{ background:var(--warning); }
  .bar-fill.critical{ background:var(--critical); }

  .empty-state{
    font-family:"Inter",sans-serif;
    font-size:14px;
    color:var(--text-faint);
    padding:24px;
    text-align:center;
  }

  .generated-badge{
    font-family:"Calibri","Trebuchet MS",sans-serif;
    font-size:18px;
    font-weight:700;
    color:#6b3f10;
    background:#ffe8c2;
    border:none;
    padding:3px 10px;
    border-radius:6px;
    white-space:nowrap;
    flex-shrink:0;
  }

  /* ---- At-a-glance summary banner ---- */
  .at-a-glance-row{
    display:flex;
    align-items:center;
    gap:14px;
    margin-bottom:4px;
  }
  .at-a-glance-row .dash-filter{
    flex:0 0 auto;
  }
  .at-a-glance{
    flex:0 1 auto;
    margin:0 0 0 auto;
    display:flex;
    justify-content:center;
    align-items:center;
    gap:12px;
    flex-wrap:wrap;
    padding:4px 24px;
    border-radius:10px;
    font-family:"Inter",sans-serif;
    font-weight:800;
    font-size:20px;
    letter-spacing:0.01em;
    text-align:center;
  }
  .at-a-glance.all-healthy{
    background:var(--healthy-bg);
    border:1.5px solid rgba(21,128,61,0.3);
    color:var(--healthy);
  }
  .at-a-glance.has-issues{
    background:linear-gradient(90deg, rgba(153,27,27,0.09), rgba(113,63,18,0.07));
    border:1.5px solid rgba(153,27,27,0.3);
    color:var(--text);
    box-shadow:0 4px 14px rgba(153,27,27,0.08);
  }
  .at-a-glance .aag-count{
    display:inline-flex;
    align-items:center;
    gap:8px;
  }
  .at-a-glance .aag-count.critical{ color:var(--critical); }
  .at-a-glance .aag-count.warning{ color:var(--warning); }
  .at-a-glance .aag-sep{ color:var(--text-faint); font-weight:400; }

  /* ---- Status icon glyphs (colorblind-safe, alongside color) ---- */
  .status-icon{
    display:inline-block;
    font-size:0.92em;
    line-height:1;
  }
  /* Bigger, bolder icons on Warning/Critical badges specifically, so the
     glyph shape (✗ vs ⚠) carries more of the distinction, not just color. */
  .summary-status-col.warning .status-icon, .rep-status-col.warning .status-icon, .iq-status-col.warning .status-icon,
  .summary-status-col.critical .status-icon, .rep-status-col.critical .status-icon, .iq-status-col.critical .status-icon{
    font-size:1.35em;
    font-weight:900;
    vertical-align:-0.08em;
    margin-right:2px;
  }

  /* ---- Row-level tooltips ---- */
  [title]{ cursor:help; }

  /* ---- Pulse on Critical items only (Warning/Healthy stay static) ---- */
  @keyframes pulse-critical{
    0%, 100% { box-shadow:0 0 0 0 rgba(153,27,27,0.4); }
    50% { box-shadow:0 0 8px 2px rgba(153,27,27,0.4); }
  }
  .summary-status-col.critical,
  .rep-status-col.critical,
  .iq-status-col.critical,
  .count-status-cell.critical,
  .count-value-cell.critical,
  .badge.critical{
    animation:pulse-critical 1.7s ease-in-out infinite;
  }
  @media (prefers-reduced-motion: reduce){
    .summary-status-col.critical,
    .rep-status-col.critical,
    .iq-status-col.critical,
    .count-status-cell.critical,
    .count-value-cell.critical,
    .badge.critical{
      animation:none;
    }
  }


  /* ==================================================================
     DARK THEME
     ================================================================== */
  body.theme-dark{
    --bg:#0f1520;
    --panel:#1a2332;
    --panel-border:#324056;
    --grid-line:#26303f;
    --text:#e5e9f0;
    --text-dim:#a9b4c4;
    --text-faint:#6b7686;
    --accent:#2dd4bf;
    --accent-dim:#1f5f57;
    --healthy:#4ade80;
    --healthy-bg:rgba(74,222,128,0.14);
    --healthy-muted:#86efac;
    --warning:#fb923c;
    --warning-bg:rgba(251,146,60,0.16);
    --critical:#f87171;
    --critical-bg:rgba(248,113,113,0.16);
    --warning-strong:#fb923c;
    --critical-strong:#f87171;
  }
  body.theme-dark .page-title-box{
    background:linear-gradient(180deg, #1a2332, #141b28);
    box-shadow:0 4px 14px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.04);
  }
  body.theme-dark .theme-btn{ background:#1a2332; color:var(--text-dim); }
  body.theme-dark .dash-filter select{ background:#1a2332; color:var(--accent); }
  body.theme-dark .summary-card{
    background:linear-gradient(180deg, rgba(45,212,191,0.10), rgba(20,27,40,0.55));
    border-color:rgba(45,212,191,0.28);
  }
  body.theme-dark .replication-card{
    background:linear-gradient(180deg, rgba(251,146,60,0.10), rgba(20,27,40,0.55));
    border-color:rgba(251,146,60,0.28);
  }
  body.theme-dark .iq-card{
    background:linear-gradient(180deg, rgba(167,139,250,0.10), rgba(20,27,40,0.55));
    border-color:rgba(167,139,250,0.28);
  }
  body.theme-dark .summary-table,
  body.theme-dark .summary-table th,
  body.theme-dark .summary-table td,
  body.theme-dark .replication-table,
  body.theme-dark .replication-table th,
  body.theme-dark .replication-table td,
  body.theme-dark .iq-table,
  body.theme-dark .iq-table th,
  body.theme-dark .iq-table td,
  body.theme-dark .status-count-table th,
  body.theme-dark .status-count-table td{
    border-color:#324056 !important;
  }
  body.theme-dark .summary-table thead th,
  body.theme-dark .replication-table thead th,
  body.theme-dark .iq-table thead th,
  body.theme-dark thead tr.grp th,
  body.theme-dark thead tr.sub th,
  body.theme-dark .dash-filter select{
    background:#202b3d !important;
  }
  body.theme-dark .summary-table thead th,
  body.theme-dark .replication-table thead th,
  body.theme-dark .iq-table thead th,
  body.theme-dark thead tr.sub th{
    color:#e5e9f0 !important;
  }
  body.theme-dark thead tr.grp th.db-grp{ color:#5eead4 !important; }
  body.theme-dark thead tr.grp th.log-grp{ color:#fdba74 !important; }
  body.theme-dark .summary-table tbody tr:hover td,
  body.theme-dark .replication-table tbody tr:hover td,
  body.theme-dark .iq-table tbody tr:hover td,
  body.theme-dark tbody tr:hover td{
    background:rgba(255,255,255,0.04);
  }
  body.theme-dark .summary-dbname{ color:#5eead4; }
  body.theme-dark .rep-dsi{ color:#fdba74; }
  body.theme-dark .iq-process{ color:#c4b5fd; }
  body.theme-dark .server-tag{ color:#ffffff; }
  body.theme-dark .wide-header-box.ase-header-box{
    background:rgba(45,212,191,0.10);
    border-color:rgba(45,212,191,0.35);
  }
  body.theme-dark .wide-header-box.rep-header-box{
    background:rgba(251,146,60,0.10);
    border-color:rgba(251,146,60,0.35);
  }
  body.theme-dark .wide-header-box.iq-header-box{
    background:rgba(167,139,250,0.10);
    border-color:rgba(167,139,250,0.35);
  }
  body.theme-dark .status-count-table .count-value-cell.healthy{ background:rgba(74,222,128,0.16); color:#4ade80; }
  body.theme-dark .status-count-table .count-value-cell.warning{ background:rgba(251,146,60,0.16); color:#fb923c; }
  body.theme-dark .status-count-table .count-value-cell.critical{ background:rgba(248,113,113,0.16); color:#f87171; }
  body.theme-dark .status-count-table .count-status-cell.healthy{ background:rgba(74,222,128,0.16); color:#4ade80; }
  body.theme-dark .status-count-table .count-status-cell.warning{ background:rgba(251,146,60,0.16); color:#fb923c; }
  body.theme-dark .status-count-table .count-status-cell.critical{ background:rgba(248,113,113,0.16); color:#f87171; }
  body.theme-dark .status-count-table tbody td{ color:var(--text); }
  body.theme-dark .generated-badge{ background:#3a2c15; color:#facc82; }
  body.theme-dark .legend span.legend-healthy{ background:rgba(74,222,128,0.14); }
  body.theme-dark .legend span.legend-warning{ background:rgba(251,146,60,0.16); }
  body.theme-dark .legend span.legend-critical{ background:rgba(248,113,113,0.16); }
  body.theme-dark .summary-status-col.warning,
  body.theme-dark .rep-status-col.warning,
  body.theme-dark .iq-status-col.warning{ color:#3a1f0a; }
  body.theme-dark .summary-status-col.critical,
  body.theme-dark .rep-status-col.critical,
  body.theme-dark .iq-status-col.critical{ color:#3a0a0a; }
  body.theme-dark .filter-pointer{ color:#e5e9f0; }
  body.theme-dark .at-a-glance.has-issues{ background:#1a2332; border-color:#324056; }
  body.theme-dark .at-a-glance.all-healthy{ background:rgba(74,222,128,0.12); }
  @media print{
    body.theme-dark{
      --bg:#eef1f6; --panel:#ffffff; --panel-border:#dbe1e9; --text:#212936;
      --text-dim:#5b6472; --text-faint:#98a1ad; --accent:#0f766e; --accent-dim:#a9d3cd;
      --healthy:#15803d; --healthy-bg:rgba(21,128,61,0.10); --healthy-muted:#4f8f68;
      --warning:#713f12; --warning-bg:rgba(113,63,18,0.15); --critical:#991b1b;
      --critical-bg:rgba(153,27,27,0.11); --warning-strong:#C2410C; --critical-strong:#8B0000;
    }
    body.theme-dark .summary-card,
    body.theme-dark .replication-card,
    body.theme-dark .iq-card{ background:#ffffff; border-color:var(--panel-border); }
  }

  /* ---- Print / PDF export: keep colors, avoid mid-card page breaks ---- */
  @page{
    size: A4 landscape;
    margin: 8mm;
  }
  @media print{
    *{
      -webkit-print-color-adjust:exact !important;
      print-color-adjust:exact !important;
      color-adjust:exact !important;
    }
    body{ padding:4px 4px 2px; }
    .at-a-glance,
    .wide-header-wrap,
    .status-count-card,
    .summary-card,
    .replication-card,
    .iq-card,
    .server-block{
      break-inside:avoid;
      page-break-inside:avoid;
    }
    .wide-header-wrap{
      break-after:avoid;
      page-break-after:avoid;
    }
    .summary-table tbody tr,
    .replication-table tbody tr,
    .iq-table tbody tr,
    table tbody tr{
      break-inside:avoid;
      page-break-inside:avoid;
    }
  }
</style>
</head>
<body>

<header class="top-bar">
  <div class="page-title-box">
    <div class="theme-toggle" role="group" aria-label="Theme">
      <button type="button" id="themeLightBtn" class="theme-btn active" onclick="setTheme('light')">&#9728;&#65039; Light Theme</button>
      <button type="button" id="themeDarkBtn" class="theme-btn" onclick="setTheme('dark')">&#127769; Dark Theme</button>
    </div>
    <h1>NEOLINK - ASE, Replication and IQ Server Health Checks Dashboard</h1>
    <span id="refreshed" class="generated-badge"></span>
  </div>
</header>

<div class="at-a-glance-row">
  <div class="dash-filter">
    <span class="filter-pointer">&#9758;</span>
    <span class="select-glow-wrap">
    <select id="dashFilter" onchange="applyFilter(this.value)" aria-label="Filter dashboard">
      <option value="all" style="background:#f1f5f9; color:#1f2937;">All</option>
      <option value="critical" style="background:#fee2e2; color:#8B0000;">Only Critical</option>
      <option value="warning" style="background:#ffedd5; color:#C2410C;">Only Warning</option>
      <option value="healthy" style="background:#dcfce7; color:#15803d;">Only Healthy</option>
      <option value="critical-warning" selected style="background:#fde8e8; color:#7c2d12;">Critical &amp; Warning</option>
      <option value="ase" style="background:#ccfbf1; color:#0f766e;">ASE (all servers)</option>
      <option value="replication" style="background:#f3e8d9; color:#713f12;">Replication (all servers)</option>
      <option value="iq" style="background:#ede9fe; color:#6d28d9;">IQ (all servers)</option>
    </select>
    </span>
  </div>
  <div id="at-a-glance" class="at-a-glance"></div>
</div>

<div class="status-count-wrap">
  <div id="status-count-summary"></div>
</div>

<div id="section-ase" class="wide-header-wrap">
  <div class="section-title-box wide-header-box ase-header-box">
    <h2 class="ase-summary-title">ASE - Status Critical &amp; Warning Databases by Server</h2>
    <div class="legend">
      <span class="legend-healthy"><i class="dot healthy"></i>Healthy &lt; 80%</span>
      <span class="legend-warning"><i class="dot warning"></i>Warning 80&ndash;90%</span>
      <span class="legend-critical"><i class="dot critical"></i>Critical &gt; 90%</span>
    </div>
  </div>
</div>
<div id="summary-grid" class="summary-grid"></div>

<div id="section-replication" class="wide-header-wrap">
  <div class="section-title-box wide-header-box rep-header-box">
    <h2 class="rep-summary-title">Replication - Status Critical &amp; Warning</h2>
    <div class="rep-condition-legend">
      <span class="legend-warning"><i class="dot warning"></i>Warning: Queue 3,000&ndash;50,000</span>
      <span class="legend-critical"><i class="dot critical"></i>Critical: Queue &gt;50,000 or Agent Down</span>
    </div>
  </div>
</div>
<div id="replication-summary" class="replication-grid"></div>

<div id="section-iq" class="wide-header-wrap">
  <div class="section-title-box wide-header-box iq-header-box">
    <h2 class="iq-summary-title">IQ - Status Critical &amp; Warning</h2>
    <div class="iq-condition-legend">
      <span class="legend-warning"><i class="dot warning"></i>Warning: Usage 76&ndash;94% or Long Running present</span>
      <span class="legend-critical"><i class="dot critical"></i>Critical: Usage 95&ndash;100%</span>
    </div>
  </div>
</div>
<div id="iq-summary" class="iq-grid"></div>

<script>
const SOURCE_FILE = "__SOURCE_FILE__";
const GENERATED_AT = "__GENERATED_AT__";
const servers = __SERVERS_JSON__;
const replication = __REPLICATION_JSON__;
const iq = __IQ_JSON__;

function statusFor(pct){
  if (pct < 80) return "healthy";
  if (pct <= 90) return "warning";
  return "critical";
}
function fmt(n){ return n.toLocaleString("en-IN"); }
function pctFmt(p){ return (Math.round(p*100)/100).toFixed(1) + "%"; }
function statusIcon(s){
  if (s === "healthy") return '<span class="status-icon">&#10003;</span>';
  if (s === "warning") return '<span class="status-icon">&#9888;</span>';
  if (s === "critical") return '<span class="status-icon">&#10007;</span>';
  return "";
}
function statusLabel(s){ return `${statusIcon(s)} ${s.charAt(0).toUpperCase()+s.slice(1)}`; }

function computeRow(r){
  const dbBal = r.dbAlloc - r.dbUsed;
  const dbPct = r.dbAlloc > 0 ? (r.dbUsed / r.dbAlloc) * 100 : 0;
  const dbStatus = statusFor(dbPct);
  const logBal = r.logAlloc - r.logUsed;
  const logPct = r.logAlloc > 0 ? (r.logUsed / r.logAlloc) * 100 : 0;
  const logStatus = statusFor(logPct);
  return {...r, dbBal, dbPct, dbStatus, logBal, logPct, logStatus};
}

function badge(status, small){
  const cls = small ? `badge ${status} badge-sm` : `badge ${status}`;
  return `<span class="${cls}">${statusLabel(status)}</span>`;
}
function bar(pct, status){
  const w = Math.min(pct,100).toFixed(1);
  return `<span class="bar-track"><span class="bar-fill ${status}" style="width:${w}%"></span></span>`;
}

function renderSummary(server, padCount){
  const rows = (server.rows || []).map(computeRow);

  // Overall row status now covers healthy too, so "Only Healthy" has data to show
  const combinedStatus = r => (r.dbStatus === "critical" || r.logStatus === "critical") ? "critical"
    : (r.dbStatus === "warning" || r.logStatus === "warning") ? "warning" : "healthy";
  const rank = {critical:0, warning:1, healthy:2};

  const sortedRows = rows.slice().sort((a,b) => {
    const oa = combinedStatus(a), ob = combinedStatus(b);
    if (rank[oa] !== rank[ob]) return rank[oa]-rank[ob];
    return Math.max(b.dbPct,b.logPct) - Math.max(a.dbPct,a.logPct);
  });

  const fillerRows = sortedRows.length > 0 && padCount > 0
    ? Array.from({length: padCount}, () => `<tr class="filler-row"><td colspan="5">&nbsp;</td></tr>`).join("")
    : "";

  const bodyHtml = sortedRows.length === 0 ? "" : `
    <table class="summary-table">
      <colgroup>
        <col class="col-slno"><col class="col-name"><col class="col-data"><col class="col-log"><col class="col-status">
      </colgroup>
      <thead>
        <tr>
          <th>Sl.No</th>
          <th>DB<br>Name</th>
          <th>Data<br>Size (%)</th>
          <th>Log<br>Size (%)</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        ${sortedRows.map((r, idx) => `
          <tr data-status="${combinedStatus(r)}" title="Data: ${fmt(r.dbUsed)}/${fmt(r.dbAlloc)} MB used (${pctFmt(r.dbPct)}) &#10; Log: ${fmt(r.logUsed)}/${fmt(r.logAlloc)} MB used (${pctFmt(r.logPct)}) &#10; Checked: ${GENERATED_AT}">
            <td class="summary-slno">${idx + 1}</td>
            <td class="summary-dbname">${r.name}</td>
            <td class="summary-pctcell ${r.dbStatus}">${pctFmt(r.dbPct)}</td>
            <td class="summary-pctcell ${r.logStatus}">${pctFmt(r.logPct)}</td>
            <td class="summary-status-col ${combinedStatus(r)}">${statusLabel(combinedStatus(r))}</td>
          </tr>`).join("")}
        ${fillerRows}
      </tbody>
    </table>
    <div class="summary-empty all-healthy-note" style="display:none;">No Warning or Critical items &mdash; all healthy.</div>`;

  return `
  <div class="summary-card">
    <div class="summary-card-head">
      <span class="server-tag">${server.name}</span>
    </div>
    ${bodyHtml}
  </div>`;
}
function countIssues(server){
  // Counts ALL rows now (grid alignment needs to work for the "All" view too)
  return (server.rows || []).length;
}
function renderGridWithEqualRows(containerEl, items, columnsCap, countFn, renderFn){
  const columns = Math.max(1, Math.min(columnsCap, items.length));
  containerEl.style.gridTemplateColumns = `repeat(${columns}, 1fr)`;
  const counts = items.map(countFn);
  let html = "";
  for (let i = 0; i < items.length; i += columns){
    const chunkCounts = counts.slice(i, i + columns);
    const maxCount = Math.max(0, ...chunkCounts);
    for (let j = i; j < Math.min(i + columns, items.length); j++){
      const padCount = maxCount - counts[j];
      html += renderFn(items[j], padCount);
    }
  }
  containerEl.innerHTML = html;
}

const summaryGridEl = document.getElementById("summary-grid");
renderGridWithEqualRows(summaryGridEl, servers, 3, countIssues, renderSummary);

document.getElementById("refreshed").textContent = GENERATED_AT;

// ---- Replication summary ----
function queueStatus(queue){
  if (queue === null || queue === undefined) return null;
  if (queue > 50000) return "critical";
  if (queue > 3000) return "warning";
  return "healthy";
}
function rowStatus(row){
  if (row.agentStatus && String(row.agentStatus).toLowerCase() === "down") return "critical";
  const qs = queueStatus(row.queue);
  return qs || "healthy";
}
function renderReplicationCard(section, padCount){
  const allRows = section.rows || [];
  const sortedRows = allRows.slice().sort((a,b) => {
    const rank = {critical:0, warning:1, healthy:2};
    const ra = rank[rowStatus(a)], rb = rank[rowStatus(b)];
    return ra - rb;
  });

  const fillerRows = sortedRows.length > 0 && padCount > 0
    ? Array.from({length: padCount}, () => `<tr class="filler-row"><td colspan="4">&nbsp;</td></tr>`).join("")
    : "";

  const bodyHtml = sortedRows.length === 0
    ? ""
    : `<table class="replication-table">
        <colgroup>
          <col class="col-dsi"><col class="col-queue"><col class="col-agent"><col class="col-status">
        </colgroup>
        <thead>
          <tr>
            <th>DSI</th>
            <th>Queue</th>
            <th>Agent_Status</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>${sortedRows.map(row => {
          const st = rowStatus(row);
          const isDown = row.agentStatus && String(row.agentStatus).toLowerCase() === "down";
          const qs = queueStatus(row.queue);
          const queueDisplay = (row.queue === null || row.queue === undefined) ? "&mdash;" : fmt(row.queue);
          const agentDisplay = row.agentStatus ? row.agentStatus : "&mdash;";
          const queueCls = row.queue === null || row.queue === undefined ? "dash" : (qs || "");
          const agentCls = row.agentStatus ? (isDown ? "down" : "up") : "dash";
          return `
            <tr data-status="${st}" title="Queue: ${row.queue === null || row.queue === undefined ? "\u2014" : fmt(row.queue) + " msgs"} &#10; Agent: ${row.agentStatus ? row.agentStatus : "\u2014"} &#10; Checked: ${GENERATED_AT}">
              <td class="rep-dsi">${row.dsi}</td>
              <td class="rep-queue ${queueCls}">${queueDisplay}</td>
              <td class="rep-agent ${agentCls}">${agentDisplay}</td>
              <td class="rep-status-col ${st}">${statusLabel(st)}</td>
            </tr>`;
        }).join("")}${fillerRows}</tbody>
      </table>
      <div class="replication-empty all-healthy-note" style="display:none;">No Warning or Critical items &mdash; all healthy.</div>`;

  return `
  <div class="replication-card">
    <div class="replication-card-head">
      <span class="server-tag">${section.name}</span>
    </div>
    ${bodyHtml}
  </div>`;
}
function countRepIssues(section){
  return (section.rows || []).length;
}
function renderReplication(sections){
  const container = document.getElementById("replication-summary");
  if (!sections || sections.length === 0){
    container.innerHTML = `<div class="replication-card"><div class="replication-empty">No replication data found.</div></div>`;
    return;
  }
  renderGridWithEqualRows(container, sections, 3, countRepIssues, renderReplicationCard);
}
renderReplication(replication);

// ---- IQ summary ----
function usageStatus(usage){
  if (usage === null || usage === undefined) return null;
  if (usage >= 95) return "critical";
  if (usage >= 76) return "warning";
  return "healthy";
}
function iqRowStatus(row){
  if (row.transactions){
    return String(row.transactions).toLowerCase() === "yes" ? "warning" : "healthy";
  }
  const us = usageStatus(row.usage);
  return us || "healthy";
}
function renderIqCard(section, padCount){
  const allRows = section.rows || [];
  const sortedRows = allRows.slice().sort((a,b) => {
    const rank = {critical:0, warning:1, healthy:2};
    return rank[iqRowStatus(a)] - rank[iqRowStatus(b)];
  });

  const fillerRows = sortedRows.length > 0 && padCount > 0
    ? Array.from({length: padCount}, () => `<tr class="filler-row"><td colspan="4">&nbsp;</td></tr>`).join("")
    : "";

  const bodyHtml = sortedRows.length === 0
    ? ""
    : `<table class="iq-table">
        <colgroup>
          <col class="col-process"><col class="col-usage"><col class="col-transactions"><col class="col-status">
        </colgroup>
        <thead>
          <tr>
            <th>Process</th>
            <th>Usage</th>
            <th>Transactions</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>${sortedRows.map(row => {
          const st = iqRowStatus(row);
          const isYes = row.transactions && String(row.transactions).toLowerCase() === "yes";
          const us = usageStatus(row.usage);
          const usageDisplay = (row.usage === null || row.usage === undefined) ? "&mdash;" : (row.usage + "%");
          const txnDisplay = row.transactions ? row.transactions : "&mdash;";
          const usageCls = row.usage === null || row.usage === undefined ? "dash" : (us || "");
          const txnCls = row.transactions ? (isYes ? "yes" : "no") : "dash";
          return `
            <tr data-status="${st}" title="Usage: ${row.usage === null || row.usage === undefined ? "\u2014" : row.usage + "%"} &#10; Long Running: ${row.transactions ? row.transactions : "\u2014"} &#10; Checked: ${GENERATED_AT}">
              <td class="iq-process">${row.process}</td>
              <td class="iq-usage ${usageCls}">${usageDisplay}</td>
              <td class="iq-txn ${txnCls}">${txnDisplay}</td>
              <td class="iq-status-col ${st}">${statusLabel(st)}</td>
            </tr>`;
        }).join("")}${fillerRows}</tbody>
      </table>
      <div class="iq-empty all-healthy-note" style="display:none;">No Warning or Critical items &mdash; all healthy.</div>`;

  return `
  <div class="iq-card">
    <div class="iq-card-head">
      <span class="server-tag">${section.name}</span>
    </div>
    ${bodyHtml}
  </div>`;
}
function countIqIssues(section){
  return (section.rows || []).length;
}
function renderIq(sections){
  const container = document.getElementById("iq-summary");
  if (!sections || sections.length === 0){
    container.innerHTML = `<div class="iq-card"><div class="iq-empty">No IQ data found.</div></div>`;
    return;
  }
  renderGridWithEqualRows(container, sections, 3, countIqIssues, renderIqCard);
}
renderIq(iq);

// ---- Status count summary table ----
function dbOverallStatus(r){
  if (r.dbStatus === "critical" || r.logStatus === "critical") return "critical";
  if (r.dbStatus === "warning" || r.logStatus === "warning") return "warning";
  return "healthy";
}
function tallyCounts(items, statusFn){
  const counts = {healthy:0, warning:0, critical:0};
  items.forEach(item => { counts[statusFn(item)]++; });
  return counts;
}
function renderAtAGlance(totalCritical, totalWarning){
  const el = document.getElementById("at-a-glance");
  if (!el) return;
  if (totalCritical === 0 && totalWarning === 0){
    el.className = "at-a-glance all-healthy";
    el.innerHTML = `<span class="aag-count">${statusIcon("healthy")} All Systems Healthy</span>`;
    return;
  }
  el.className = "at-a-glance has-issues";
  const parts = [];
  if (totalCritical > 0) parts.push(`<span class="aag-count critical">${statusIcon("critical")} ${totalCritical} Critical</span>`);
  if (totalWarning > 0) parts.push(`<span class="aag-count warning">${statusIcon("warning")} ${totalWarning} Warning</span>`);
  el.innerHTML = parts.join('<span class="aag-sep">&middot;</span>');
}
function renderCountTable(){
  const container = document.getElementById("status-count-summary");
  if (!container) return;

  const aseDatabases = [];
  servers.forEach(s => (s.rows || []).forEach(r => aseDatabases.push(computeRow(r))));
  const aseCounts = tallyCounts(aseDatabases, dbOverallStatus);

  const repRows = [];
  replication.forEach(s => (s.rows || []).forEach(r => repRows.push(r)));
  const repCounts = tallyCounts(repRows, rowStatus);

  const iqRows = [];
  iq.forEach(s => (s.rows || []).forEach(r => iqRows.push(r)));
  const iqCounts = tallyCounts(iqRows, iqRowStatus);

  const totalCritical = aseCounts.critical + repCounts.critical + iqCounts.critical;
  const totalWarning = aseCounts.warning + repCounts.warning + iqCounts.warning;
  renderAtAGlance(totalCritical, totalWarning);

  const categories = [
    {name: "ASE", cls: "ase", counts: aseCounts},
    {name: "Replication", cls: "rep", counts: repCounts},
    {name: "IQ", cls: "iq", counts: iqCounts},
  ];

  const statusOrder = ["critical", "warning", "healthy"];

  const cardsHtml = categories.map(cat => `
    <div class="status-count-card">
      <table class="status-count-table">
        <thead>
          <tr>
            <th class="count-head-category cat-${cat.cls}" colspan="2">${cat.name}</th>
          </tr>
          <tr>
            <th class="count-sub-head cat-${cat.cls}">Status</th>
            <th class="count-sub-head cat-${cat.cls}">Count</th>
          </tr>
        </thead>
        <tbody>
          ${statusOrder.map(st => `
          <tr>
            <td class="count-status-cell ${st}">${statusLabel(st)}</td>
            <td class="count-value-cell ${st}">${cat.counts[st]}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`).join("");

  container.innerHTML = cardsHtml;
}
renderCountTable();

/* ---- Dropdown filter: All / Critical / Warning / Critical & Warning / ASE / Replication / IQ ---- */
/* ---- Light / Dark theme toggle ---- */
function setTheme(mode){
  document.body.classList.toggle("theme-dark", mode === "dark");
  const lightBtn = document.getElementById("themeLightBtn");
  const darkBtn = document.getElementById("themeDarkBtn");
  if (lightBtn) lightBtn.classList.toggle("active", mode === "light");
  if (darkBtn) darkBtn.classList.toggle("active", mode === "dark");
}

function applyFilter(value){
  const sectionGroups = {
    ase: [document.getElementById("section-ase"), document.getElementById("summary-grid")],
    replication: [document.getElementById("section-replication"), document.getElementById("replication-summary")],
    iq: [document.getElementById("section-iq"), document.getElementById("iq-summary")],
  };

  let visibleSections = ["ase", "replication", "iq"];
  if (value === "ase" || value === "replication" || value === "iq") visibleSections = [value];

  Object.keys(sectionGroups).forEach(key => {
    const show = visibleSections.includes(key);
    sectionGroups[key].forEach(el => { if (el) el.classList.toggle("card-hidden", !show); });
  });

  let statuses = null; // null = show critical + warning + healthy (no status filtering)
  if (value === "critical") statuses = ["critical"];
  else if (value === "warning") statuses = ["warning"];
  else if (value === "healthy") statuses = ["healthy"];
  else if (value === "critical-warning") statuses = ["critical", "warning"];
  // "all" and the section-only options (ase/replication/iq) leave statuses null -> show everything

  document.querySelectorAll("tr[data-status]").forEach(tr => {
    const hide = statuses !== null && !statuses.includes(tr.dataset.status);
    tr.classList.toggle("row-hidden", hide);
  });

  // Per card: show the table normally if any row matches; if nothing matches AND
  // the card is genuinely all-healthy, show the friendly note instead of an empty
  // table; otherwise (nothing matches for an unrelated reason) collapse the card.
  document.querySelectorAll(".summary-card, .replication-card, .iq-card").forEach(card => {
    const rows = card.querySelectorAll("tr[data-status]");
    const table = card.querySelector("table");
    const note = card.querySelector(".all-healthy-note");
    if (rows.length === 0) { card.classList.remove("card-hidden"); return; }

    const allHealthy = Array.from(rows).every(tr => tr.dataset.status === "healthy");
    const anyVisible = Array.from(rows).some(tr => !tr.classList.contains("row-hidden"));

    if (anyVisible){
      card.classList.remove("card-hidden");
      if (table) table.style.display = "";
      if (note) note.style.display = "none";
    } else if (allHealthy){
      card.classList.remove("card-hidden");
      if (table) table.style.display = "none";
      if (note) note.style.display = "";
    } else {
      card.classList.add("card-hidden");
    }
  });

  // The row/card DOM writes above can cause some browsers to silently freeze
  // the dropdown's CSS glow animation. Force it to restart every time.
  const glowWrap = document.querySelector(".select-glow-wrap");
  if (glowWrap){
    glowWrap.style.animation = "none";
    void glowWrap.offsetHeight; // force reflow so the browser "sees" the reset
    glowWrap.style.animation = "";
  }
}
applyFilter(document.getElementById("dashFilter") ? document.getElementById("dashFilter").value : "critical-warning");
</script>

</body>
</html>
"""


def main():
    if len(sys.argv) != 3:
        print("Usage: generate_dashboard.py <input_file> <output_html>", file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    if not os.path.isfile(input_path):
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    sections, replication_records, iq_records = parse_file(input_path)
    total_rows = sum(len(s["rows"]) for s in sections)
    total_rep = len(replication_records)
    total_iq = len(iq_records)

    if total_rows == 0 and total_rep == 0 and total_iq == 0:
        print(f"ERROR: no valid data rows parsed from {input_path}", file=sys.stderr)
        sys.exit(2)

    generated_at = now_cet_str()

    html = HTML_TEMPLATE
    html = html.replace("__SERVERS_JSON__", json.dumps(sections))
    html = html.replace("__REPLICATION_JSON__", json.dumps(replication_records))
    html = html.replace("__IQ_JSON__", json.dumps(iq_records))
    html = html.replace("__SOURCE_FILE__", os.path.abspath(input_path))
    html = html.replace("__GENERATED_AT__", generated_at)

    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"OK: {total_rows} DB rows across {len(sections)} section(s), "
          f"{total_rep} replication server(s), {total_iq} IQ server(s) -> {output_path}")


if __name__ == "__main__":
    main()
