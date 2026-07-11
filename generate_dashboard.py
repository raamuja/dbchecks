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
    Returns (ase_sections, replication_sections).

    ase_sections: list of {"name": server_name, "rows": [...]} dicts (unchanged
    behaviour from before - DB/Log size sections).

    replication_sections: list of {"name": server_name, "rows": [...]} dicts,
    one per replication server block found under a "UAT REPLICATION" marker
    line. Each row is {"dsi": str, "queue": int|None, "agentStatus": str|None}
    - i.e. the raw DSI lines (e.g. "Connection" with a queue number, or
    "Rep_agent_N" with a Down/Up status) are kept as separate rows, mirroring
    exactly what's in the source file.

    The input file may optionally have a "UAT ASE" marker line before the DB
    sections and a "UAT REPLICATION" marker line before the replication
    blocks. If neither marker is present, the whole file is parsed as ASE
    data (fully backward compatible with older db_size.txt files that only
    ever had DB/Log size sections).
    """
    ase_sections = []
    replication_sections = []
    current_ase = None
    current_rep = None

    mode = "ase"

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip().rstrip("\r")
            if not line:
                continue

            upper = line.upper()
            if upper == "UAT ASE":
                mode = "ase"
                continue
            if upper == "UAT REPLICATION":
                mode = "replication"
                continue

            if mode == "ase":
                if line.startswith(SKIP_PREFIXES):
                    continue
                m = DATA_ROW_RE.match(line)
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
                    current_ase = {"name": display_name(line), "rows": []}
                    ase_sections.append(current_ase)

            else:  # mode == "replication"
                if line.upper().startswith("DSI"):
                    continue
                tokens = line.split()
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

    return ase_sections, replication_sections


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Sybase DB &amp; Log Size Monitor</title>
<style>
  :root{
    --bg:#0b0e14;
    --panel:#11151d;
    --panel-border:#1f2733;
    --grid-line:#2c3646;
    --text:#d7dee8;
    --text-dim:#7c8798;
    --text-faint:#4c5666;
    --accent:#4fd1c5;
    --accent-dim:#2a5c56;
    --healthy:#3ecf6e;
    --healthy-bg:rgba(62,207,110,0.12);
    --warning:#f5a524;
    --warning-bg:rgba(245,165,36,0.12);
    --critical:#f5484c;
    --critical-bg:rgba(245,72,76,0.14);
    --mono: "SF Mono","Consolas","Liberation Mono", monospace;
  }
  *{box-sizing:border-box;}
  body{
    margin:0;
    background:
      radial-gradient(1200px 500px at 15% -10%, rgba(79,209,197,0.07), transparent 60%),
      radial-gradient(900px 500px at 100% 0%, rgba(245,165,36,0.05), transparent 55%),
      var(--bg);
    color:var(--text);
    font-family: "Inter","Segoe UI",Helvetica,Arial,sans-serif;
    padding:54px 40px 40px;
    min-height:100vh;
  }
  header{
    display:flex;
    flex-direction:column;
    align-items:center;
    border-bottom:1px solid var(--panel-border);
    padding-bottom:12px;
    margin-bottom:14px;
    gap:10px;
  }
  .page-title-box{
    display:inline-block;
    margin:0 auto;
    padding:8px 28px;
    border:1px solid var(--accent-dim);
    border-radius:8px;
    background:linear-gradient(180deg, rgba(255,255,255,0.03), rgba(0,0,0,0.18));
    box-shadow:0 6px 18px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.04);
  }
  .page-title-box h1{
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
    gap:18px;
    font-family:var(--mono);
    font-size:13px;
    font-weight:700;
    color:var(--text-dim);
    flex-shrink:0;
  }
  .legend span{display:flex;align-items:center;gap:6px;white-space:nowrap;}
  .legend span.legend-healthy{ color:var(--healthy); }
  .legend span.legend-warning{ color:var(--warning); }
  .legend span.legend-critical{ color:var(--critical); }
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;}
  .dot.healthy{background:var(--healthy);box-shadow:0 0 8px rgba(62,207,110,0.9);}
  .dot.warning{background:var(--warning);box-shadow:0 0 8px rgba(245,165,36,0.9);}
  .dot.critical{background:var(--critical);box-shadow:0 0 8px rgba(245,72,76,0.9);}

  .section-title-wrap{
    text-align:center;
    margin-bottom:10px;
  }
  .section-title-box{
    display:inline-block;
    padding:7px 22px;
    border:1px solid var(--panel-border);
    border-radius:8px;
    background:var(--panel);
    box-shadow:0 3px 12px rgba(0,0,0,0.25);
  }
  .section-title-box h2{
    margin:0;
    text-align:center;
    font-size:clamp(13px, 1.3vw, 17px);
    font-weight:700;
    letter-spacing:0.02em;
    color:var(--accent);
    white-space:nowrap;
  }
  .section-title-box h2.ase-summary-title{
    color:#4fd1c5;
  }
  .section-title-box h2.rep-summary-title{
    color:#e8a33d;
  }
  .wide-header-wrap{
    margin-bottom:10px;
  }
  .wide-header-box{
    display:flex;
    align-items:center;
    justify-content:space-between;
    flex-wrap:wrap;
    gap:16px;
    width:100%;
    padding:7px 22px;
    border:1px solid var(--panel-border);
    border-radius:8px;
    background:var(--panel);
    box-shadow:0 3px 12px rgba(0,0,0,0.25);
  }
  .wide-header-box h2{
    text-align:left;
    margin:0;
    white-space:nowrap;
  }
  .wide-header-box .legend{
    justify-content:flex-end;
  }
  .rep-condition-legend{
    display:flex;
    justify-content:flex-end;
    gap:16px;
    font-family:var(--mono);
    font-size:13px;
    font-weight:700;
    flex-wrap:wrap;
    flex-shrink:0;
  }
  .rep-condition-legend span{ display:flex; align-items:center; gap:6px; white-space:nowrap; }
  .rep-condition-legend .dot{width:8px;height:8px;border-radius:50%;display:inline-block;}
  .rep-condition-legend .legend-warning{ color:var(--warning); }
  .rep-condition-legend .legend-critical{ color:var(--critical); }
  .summary-grid{
    display:grid;
    grid-template-columns:repeat(2, 1fr);
    gap:12px;
    margin-bottom:16px;
    align-items:stretch;
  }
  @media (max-width:900px){
    .summary-grid{ grid-template-columns:1fr !important; }
  }
  .summary-card{
    background:linear-gradient(180deg, rgba(79,209,197,0.10), rgba(79,209,197,0.045));
    border:1px solid rgba(79,209,197,0.28);
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
    padding:6px 14px;
    border-bottom:1px solid rgba(79,209,197,0.22);
    background:rgba(79,209,197,0.09);
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
    font-size:13px;
    color:#f0b662;
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
    background:#0e131b;
    color:var(--text);
    font-family:"Inter",sans-serif;
    font-weight:700;
    font-size:11px;
    letter-spacing:0;
    text-transform:uppercase;
    padding:6px 4px;
    text-align:center;
    border-bottom:1px solid var(--panel-border);
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .summary-table tbody td{
    padding:6px 6px;
    border-bottom:1px solid var(--grid-line);
    font-family:"Inter",sans-serif;
    font-size:13px;
    text-align:center;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .summary-table tbody tr:last-child td{ border-bottom:none; }
  .summary-table tbody tr:hover td{ background:rgba(255,255,255,0.018); }
  .summary-table .summary-dbname{
    font-family:"Inter",sans-serif;
    font-weight:600;
    font-size:13px;
    letter-spacing:0.01em;
    color:#6fe3d6;
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
  .summary-pctcell.healthy{ color:var(--healthy); }
  .summary-pctcell.warning{ color:var(--warning); }
  .summary-pctcell.critical{ color:var(--critical); }
  .summary-status-col{ text-align:center; padding:7px 4px; font-weight:700; }
  .summary-status-col.healthy{ color:var(--healthy); }
  .summary-status-col.warning{ color:var(--warning); }
  .summary-status-col.critical{ color:var(--critical); }
  .badge.badge-sm{
    padding:3px 7px;
    font-size:11px;
    gap:4px;
  }
  .badge.badge-sm .dot{ width:5px; height:5px; }
  .summary-empty{
    padding:20px 16px;
    text-align:center;
    font-family:"Inter",sans-serif;
    font-weight:600;
    font-size:15px;
    color:var(--healthy);
  }

  .replication-grid{
    display:grid;
    gap:12px;
    margin-bottom:16px;
    align-items:stretch;
  }
  @media (max-width:900px){
    .replication-grid{ grid-template-columns:1fr !important; }
  }
  .replication-card{
    background:linear-gradient(180deg, rgba(232,163,61,0.11), rgba(232,163,61,0.05));
    border:1px solid rgba(232,163,61,0.3);
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
    padding:6px 14px;
    border-bottom:1px solid rgba(232,163,61,0.24);
    background:rgba(232,163,61,0.1);
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
    background:#0e131b;
    color:var(--text);
    font-family:"Inter",sans-serif;
    font-weight:700;
    font-size:11px;
    letter-spacing:0;
    text-transform:uppercase;
    padding:6px 4px;
    text-align:center;
    border-bottom:1px solid var(--panel-border);
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .replication-table tbody td{
    padding:6px 4px;
    border-bottom:1px solid var(--grid-line);
    font-family:"Inter",sans-serif;
    font-size:13px;
    text-align:center;
    color:var(--text);
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  .replication-table tbody tr:last-child td{ border-bottom:none; }
  .replication-table tbody tr:hover td{ background:rgba(255,255,255,0.018); }
  .rep-dsi{ font-weight:600; color:#f0b662; text-align:center; }
  .rep-queue{ font-weight:600; }
  .rep-queue.healthy{ color:var(--healthy); }
  .rep-queue.warning{ color:var(--warning); }
  .rep-queue.critical{ color:var(--critical); }
  .rep-queue.dash{ color:var(--text-faint); font-weight:400; }
  .rep-agent{ font-weight:600; }
  .rep-agent.down{ color:var(--critical); }
  .rep-agent.up{ color:var(--healthy); }
  .rep-agent.dash{ color:var(--text-faint); font-weight:400; }
  .rep-status-col{ font-weight:700; }
  .rep-status-col.healthy{ color:var(--healthy); }
  .rep-status-col.warning{ color:var(--warning); }
  .rep-status-col.critical{ color:var(--critical); }
  .replication-empty{
    padding:20px 16px;
    text-align:center;
    font-family:"Inter",sans-serif;
    font-weight:600;
    font-size:15px;
    color:var(--healthy);
  }

  .server-block{ margin-bottom:0; min-width:0; }
  .dashboard-grid{
    display:grid;
    grid-template-columns:repeat(2, 1fr);
    gap:18px;
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
    margin-bottom:14px;
  }
  .server-tag{
    font-family:"Inter",sans-serif;
    font-size:13px;
    font-weight:700;
    letter-spacing:0.06em;
    color:var(--accent);
    border:1px solid var(--accent-dim);
    background:rgba(79,209,197,0.06);
    padding:4px 10px;
    border-radius:3px;
  }
  .server-head h2{
    font-size:18.5px;
    margin:0 0 0 auto;
    color:#eef2f7;
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
    background:#0e131b;
    color:var(--text-faint);
    font-family:"Inter",sans-serif;
    font-weight:700;
    font-size:13px;
    letter-spacing:0.02em;
    text-transform:uppercase;
    padding:9px 4px 6px;
    text-align:center;
    border-bottom:1px solid var(--grid-line);
  }
  thead tr.grp th.db-grp{ color:#7fd8cd; border-left:1px solid var(--panel-border);}
  thead tr.grp th.log-grp{ color:#e3b16a; border-left:1px solid var(--panel-border);}
  thead tr.sub th{
    background:#0e131b;
    color:var(--text);
    font-family:"Inter",sans-serif;
    font-weight:700;
    font-size:10.5px;
    letter-spacing:0.01em;
    text-transform:uppercase;
    padding:5px 3px 8px;
    text-align:center;
    border-bottom:1px solid var(--panel-border);
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
  }
  thead tr.sub th.div-left{ border-left:1px solid var(--panel-border); }

  tbody td{
    padding:7px 3px;
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
  tbody tr:hover td{ background:rgba(255,255,255,0.018); }
  td.dbname{
    text-align:center;
    font-family:"Inter",sans-serif;
    font-weight:600;
    color:#f2f5f9;
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
  td.pct.healthy .pct-val{ color:var(--healthy); }
  td.pct.warning .pct-val{ color:var(--warning); }
  td.pct.critical .pct-val{ color:var(--critical); }
  td.status-col{ text-align:center; padding:6px 4px; }

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
  .badge.healthy{ color:var(--healthy); background:var(--healthy-bg); border:1px solid rgba(62,207,110,0.35);}
  .badge.warning{ color:var(--warning); background:var(--warning-bg); border:1px solid rgba(245,165,36,0.4);}
  .badge.critical{ color:var(--critical); background:var(--critical-bg); border:1px solid rgba(245,72,76,0.45);}
  .badge .dot{width:6px;height:6px;}

  .bar-track{
    position:relative;
    width:64px;
    height:5px;
    border-radius:3px;
    background:#1a212c;
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
    position:fixed;
    top:16px;
    right:22px;
    font-family:var(--mono);
    font-size:16px;
    font-weight:700;
    color:var(--text);
    background:rgba(11,14,20,0.85);
    border:1px solid var(--panel-border);
    padding:6px 14px;
    border-radius:20px;
    z-index:10;
  }
</style>
</head>
<body>

<span id="refreshed" class="generated-badge"></span>

<header>
  <div class="page-title-box">
    <h1>NEOLINK - ASE, Replication and IQ Server Health Checks Dashboard</h1>
  </div>
</header>

<div class="wide-header-wrap">
  <div class="section-title-box wide-header-box">
    <h2 class="ase-summary-title">ASE - Status Critical &amp; Warning Databases by Server</h2>
    <div class="legend">
      <span class="legend-healthy"><i class="dot healthy"></i>Healthy &lt; 80%</span>
      <span class="legend-warning"><i class="dot warning"></i>Warning 80&ndash;90%</span>
      <span class="legend-critical"><i class="dot critical"></i>Critical &gt; 90%</span>
    </div>
  </div>
</div>
<div id="summary-grid" class="summary-grid"></div>

<div class="wide-header-wrap">
  <div class="section-title-box wide-header-box">
    <h2 class="rep-summary-title">Replication - Status Critical &amp; Warning</h2>
    <div class="rep-condition-legend">
      <span class="legend-warning"><i class="dot warning"></i>Warning: Queue 3,000&ndash;50,000</span>
      <span class="legend-critical"><i class="dot critical"></i>Critical: Queue &gt;50,000 or Agent Down</span>
    </div>
  </div>
</div>
<div id="replication-summary" class="replication-grid"></div>

<div class="section-title-wrap">
  <div class="section-title-box">
    <h2>Detailed breakup for all databases</h2>
  </div>
</div>
<div id="dashboard" class="dashboard-grid"></div>

<script>
const SOURCE_FILE = "__SOURCE_FILE__";
const GENERATED_AT = "__GENERATED_AT__";
const servers = __SERVERS_JSON__;
const replication = __REPLICATION_JSON__;

function statusFor(pct){
  if (pct < 80) return "healthy";
  if (pct <= 90) return "warning";
  return "critical";
}
function fmt(n){ return n.toLocaleString("en-IN"); }
function pctFmt(p){ return (Math.round(p*100)/100).toFixed(1) + "%"; }
function statusLabel(s){ return s.charAt(0).toUpperCase()+s.slice(1); }

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

function renderServer(server){
  if (!server.rows || server.rows.length === 0){
    return `
    <section class="server-block">
      <div class="server-head">
        <span class="server-tag">${server.name}</span>
        <h2>No data parsed</h2>
      </div>
      <div class="panel"><div class="empty-state">No rows found for this section.</div></div>
    </section>`;
  }

  const rows = server.rows.map(computeRow);

  const rowsHtml = rows.map((r, idx) => `
    <tr>
      <td class="slno">${idx + 1}</td>
      <td class="dbname">${r.name}</td>
      <td>${fmt(r.dbAlloc)}</td>
      <td>${fmt(r.dbUsed)}</td>
      <td>${fmt(r.dbBal)}</td>
      <td class="pct ${r.dbStatus}"><span class="pct-val">${pctFmt(r.dbPct)}</span></td>
      <td class="status-col">${badge(r.dbStatus)}</td>
      <td class="div-left">${fmt(r.logAlloc)}</td>
      <td>${fmt(r.logUsed)}</td>
      <td>${fmt(r.logBal)}</td>
      <td class="pct ${r.logStatus}"><span class="pct-val">${pctFmt(r.logPct)}</span></td>
      <td class="status-col">${badge(r.logStatus)}</td>
    </tr>
  `).join("");

  return `
  <section class="server-block">
    <div class="server-head">
      <span class="server-tag">${server.name}</span>
    </div>
    <div class="panel">
      <table>
        <colgroup>
          <col style="width:6%"><col style="width:11%">
          <col style="width:7%"><col style="width:7%"><col style="width:7%"><col style="width:8%"><col style="width:12%">
          <col style="width:7%"><col style="width:7%"><col style="width:7%"><col style="width:8%"><col style="width:13%">
        </colgroup>
        <thead>
          <tr class="grp">
            <th></th>
            <th></th>
            <th class="db-grp" colspan="5">Database Size</th>
            <th class="log-grp" colspan="5">Log Size</th>
          </tr>
          <tr class="sub">
            <th>Sl.No</th>
            <th>DB Name</th>
            <th>Allotted</th>
            <th>Used</th>
            <th>Balance</th>
            <th>% Used</th>
            <th class="status-col">Status</th>
            <th class="div-left">Allotted</th>
            <th>Used</th>
            <th>Balance</th>
            <th>% Used</th>
            <th class="status-col">Status</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
  </section>`;
}

function renderSummary(server, padCount){
  const rows = (server.rows || []).map(computeRow);

  // Only databases with at least one non-healthy metric
  const issues = rows.filter(r => r.dbStatus !== "healthy" || r.logStatus !== "healthy");

  // Overall row status = worst of DB/Log status for that database
  const overallStatus = r => (r.dbStatus === "critical" || r.logStatus === "critical") ? "critical" : "warning";
  const rank = {critical:0, warning:1};

  issues.sort((a,b) => {
    const oa = overallStatus(a), ob = overallStatus(b);
    if (rank[oa] !== rank[ob]) return rank[oa]-rank[ob];
    return Math.max(b.dbPct,b.logPct) - Math.max(a.dbPct,a.logPct);
  });

  const fillerRows = issues.length > 0 && padCount > 0
    ? Array.from({length: padCount}, () => `<tr class="filler-row"><td colspan="5">&nbsp;</td></tr>`).join("")
    : "";

  const bodyHtml = issues.length === 0 ? "" : `
    <table class="summary-table">
      <colgroup>
        <col class="col-slno"><col class="col-name"><col class="col-data"><col class="col-log"><col class="col-status">
      </colgroup>
      <thead>
        <tr>
          <th>Sl.No</th>
          <th>DB Name</th>
          <th>Data Size (%)</th>
          <th>Log Size (%)</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        ${issues.map((r, idx) => `
          <tr>
            <td class="summary-slno">${idx + 1}</td>
            <td class="summary-dbname">${r.name}</td>
            <td class="summary-pctcell ${r.dbStatus}">${pctFmt(r.dbPct)}</td>
            <td class="summary-pctcell ${r.logStatus}">${pctFmt(r.logPct)}</td>
            <td class="summary-status-col ${overallStatus(r)}">${statusLabel(overallStatus(r))}</td>
          </tr>`).join("")}
        ${fillerRows}
      </tbody>
    </table>`;

  return `
  <div class="summary-card">
    <div class="summary-card-head">
      <span class="server-tag">${server.name}</span>
    </div>
    ${bodyHtml}
  </div>`;
}
function countIssues(server){
  const rows = (server.rows || []).map(computeRow);
  return rows.filter(r => r.dbStatus !== "healthy" || r.logStatus !== "healthy").length;
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
document.getElementById("dashboard").innerHTML = servers.map(renderServer).join("");
document.getElementById("refreshed").textContent = "Generated " + GENERATED_AT;

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
  const filteredRows = (section.rows || []).filter(row => rowStatus(row) !== "healthy");

  const fillerRows = filteredRows.length > 0 && padCount > 0
    ? Array.from({length: padCount}, () => `<tr class="filler-row"><td colspan="4">&nbsp;</td></tr>`).join("")
    : "";

  const bodyHtml = filteredRows.length === 0
    ? `<div class="replication-empty">No Warning or Critical items &mdash; all healthy.</div>`
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
        <tbody>${filteredRows.map(row => {
          const st = rowStatus(row);
          const isDown = row.agentStatus && String(row.agentStatus).toLowerCase() === "down";
          const qs = queueStatus(row.queue);
          const queueDisplay = (row.queue === null || row.queue === undefined) ? "&mdash;" : fmt(row.queue);
          const agentDisplay = row.agentStatus ? row.agentStatus : "&mdash;";
          const queueCls = row.queue === null || row.queue === undefined ? "dash" : (qs || "");
          const agentCls = row.agentStatus ? (isDown ? "down" : "up") : "dash";
          return `
            <tr>
              <td class="rep-dsi">${row.dsi}</td>
              <td class="rep-queue ${queueCls}">${queueDisplay}</td>
              <td class="rep-agent ${agentCls}">${agentDisplay}</td>
              <td class="rep-status-col ${st}">${statusLabel(st)}</td>
            </tr>`;
        }).join("")}${fillerRows}</tbody>
      </table>`;

  return `
  <div class="replication-card">
    <div class="replication-card-head">
      <span class="server-tag">${section.name}</span>
    </div>
    ${bodyHtml}
  </div>`;
}
function countRepIssues(section){
  return (section.rows || []).filter(row => rowStatus(row) !== "healthy").length;
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

    sections, replication_records = parse_file(input_path)
    total_rows = sum(len(s["rows"]) for s in sections)
    total_rep = len(replication_records)

    if total_rows == 0 and total_rep == 0:
        print(f"ERROR: no valid data rows parsed from {input_path}", file=sys.stderr)
        sys.exit(2)

    generated_at = now_cet_str()

    html = HTML_TEMPLATE
    html = html.replace("__SERVERS_JSON__", json.dumps(sections))
    html = html.replace("__REPLICATION_JSON__", json.dumps(replication_records))
    html = html.replace("__SOURCE_FILE__", os.path.abspath(input_path))
    html = html.replace("__GENERATED_AT__", generated_at)

    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"OK: {total_rows} DB rows across {len(sections)} section(s), "
          f"{total_rep} replication server(s) -> {output_path}")


if __name__ == "__main__":
    main()
