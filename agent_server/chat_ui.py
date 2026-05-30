"""Minimal embedded chat UI served on GET / by the FastAPI agent server.

Avoids the dependency on the external e2e-chatbot-app-next frontend that the
default `start-app` script clones from GitHub — which is brittle in the
deployed Databricks Apps runtime.

This is a 4-pane operational console (Palantir Foundry-style):
  - Signals (left rail): 9 source tiles, auto-refresh /signals/latest
  - Brief (center): markdown brief w/ object chips + provenance chips
  - Actions (right rail): pending-approval action cards
  - Chat (bottom right, floating, collapsible): scenario picker + free-text
  - Timeline (bottom, collapsible): supervisor + subagent activity
"""
from __future__ import annotations
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>The 11 PM Ops Brief — Operational Console</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Nunito+Sans:wght@300;400;600;700;800&display=swap" rel="stylesheet">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🌙</text></svg>">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
<style>
  :root {
    --primary:#0061ff;--primary-dark:#0049c7;--primary-bg:#e6f0ff;
    --success:#16a34a;--success-bg:#dcfce7;
    --warning:#b45309;--warning-bg:#fef3c7;
    --danger:#dc2626;--danger-bg:#fee2e2;
    --info:#0369a1;--info-bg:#e0f2fe;
    --text:#111827;--text-secondary:#6b7280;
    --border:#e5e7eb;--surface:#ffffff;--page-bg:#f9fafb;--nav-bg:#0f1729;
    --radius-sm:4px;--radius-md:6px;--radius-lg:8px;
    --shadow-sm:0 1px 2px rgba(0,0,0,.05);
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;font-family:"Nunito Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);background:var(--page-bg);line-height:1.5;height:100%}
  body{display:flex;flex-direction:column;height:100vh;overflow:hidden}

  /* ============ HEADER ============ */
  header{background:var(--nav-bg);color:#fff;padding:10px 22px;display:flex;align-items:center;gap:16px;border-bottom:3px solid var(--primary);flex-shrink:0;z-index:50}
  header .logo{font-size:22px;line-height:1}
  header h1{font-size:16px;margin:0;font-weight:800;letter-spacing:.2px;white-space:nowrap}
  header .sev-wrap{display:flex;align-items:center;gap:10px;margin-left:18px;padding:4px 12px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:999px;cursor:pointer;transition:background .15s,border-color .15s;position:relative}
  header .sev-wrap:hover{background:rgba(255,255,255,.09);border-color:rgba(255,255,255,.18)}
  header .sev-wrap::after{content:"ⓘ";font-size:9px;color:#a5b4fc;margin-left:2px;opacity:.7}
  /* Posture popover — anchored to the header pill, opens on click */
  #posture-popover{position:absolute;top:46px;left:50%;transform:translateX(-50%);min-width:330px;max-width:380px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:var(--radius-lg);box-shadow:0 18px 48px rgba(0,0,0,.22);z-index:300;padding:14px 16px 12px;display:none;font-family:"Nunito Sans",sans-serif;text-align:left}
  #posture-popover.open{display:block}
  #posture-popover::before{content:"";position:absolute;top:-7px;left:50%;transform:translateX(-50%) rotate(45deg);width:12px;height:12px;background:var(--surface);border-left:1px solid var(--border);border-top:1px solid var(--border)}
  #posture-popover .pop-title{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;color:var(--text-secondary);margin-bottom:8px;display:flex;align-items:center;justify-content:space-between}
  #posture-popover .pop-title button{background:transparent;border:0;font-family:inherit;font-size:14px;color:var(--text-secondary);cursor:pointer;padding:0 4px;line-height:1}
  #posture-popover .pop-title button:hover{color:var(--text)}
  #posture-popover .pop-band{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);align-items:flex-start}
  #posture-popover .pop-band:last-of-type{border-bottom:0}
  #posture-popover .pop-band .pop-dot{width:10px;height:10px;border-radius:50%;margin-top:5px;flex-shrink:0}
  #posture-popover .pop-band .pop-label{font-size:11.5px;font-weight:800;letter-spacing:.4px;margin-bottom:2px;display:flex;align-items:center;gap:6px}
  #posture-popover .pop-band .pop-range{font-size:10px;color:var(--text-secondary);font-weight:700;font-variant-numeric:tabular-nums;background:var(--page-bg);padding:1px 6px;border-radius:999px}
  #posture-popover .pop-band .pop-desc{font-size:12px;color:var(--text);line-height:1.45}
  #posture-popover .pop-foot{margin-top:8px;padding-top:8px;border-top:1px dashed var(--border);font-size:10.5px;color:var(--text-secondary);line-height:1.4}
  header .sev-label{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#a5b4fc;font-weight:800}
  header .sev-bar{width:120px;height:6px;background:rgba(255,255,255,.1);border-radius:999px;overflow:hidden;position:relative}
  header .sev-fill{height:100%;width:0%;background:var(--success);border-radius:999px;transition:width .6s ease-out,background .3s}
  header .sev-num{font-size:11px;font-weight:700;font-variant-numeric:tabular-nums;color:#a5b4fc;letter-spacing:.3px}
  header .sev-pill{font-size:10px;font-weight:800;padding:3px 10px;border-radius:999px;text-transform:uppercase;letter-spacing:.5px;background:rgba(255,255,255,.12);min-width:60px;text-align:center}
  header .spacer{flex:1}
  header .clock{font-variant-numeric:tabular-nums;font-weight:700;font-size:13px;color:#e6f0ff;letter-spacing:.5px}
  header .clock-sub{font-size:10px;color:#a5b4fc;text-transform:uppercase;letter-spacing:.6px;font-weight:700}
  header button.icon-btn{font-family:inherit;font-size:11px;font-weight:700;padding:6px 11px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:var(--radius-sm);color:#e6f0ff;cursor:pointer;letter-spacing:.3px;transition:all .15s;display:inline-flex;align-items:center;gap:5px}
  header button.icon-btn:hover{background:rgba(255,255,255,.16);border-color:rgba(255,255,255,.22);color:#fff}
  header button.icon-btn.primary{background:var(--primary);border-color:var(--primary);color:#fff}
  header button.icon-btn.primary:hover{background:var(--primary-dark);border-color:var(--primary-dark)}

  /* ============ MAIN GRID ============
     Timeline (agent activity log) is no longer a bottom pane — it lives inside
     the Debug slide-out (🔍 header button). That reclaims ~200px of vertical
     space for the brief pane, which is where the operator actually reads. */
  .console{flex:1;display:grid;grid-template-columns:240px 1fr 320px;grid-template-rows:1fr;grid-template-areas:"signals brief actions";gap:1px;background:var(--border);overflow:hidden;min-height:0}
  .pane{background:var(--surface);display:flex;flex-direction:column;overflow:hidden;min-height:0}
  .pane-hdr{padding:8px 12px;border-bottom:1px solid var(--border);background:#fafbfc;font-size:10px;font-weight:800;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.6px;display:flex;align-items:center;gap:6px;flex-shrink:0;min-height:34px;overflow:hidden}
  .pane-hdr .pane-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
  .pane-hdr .count{margin-left:auto;font-size:10px;background:var(--primary-bg);color:var(--primary-dark);padding:2px 7px;border-radius:var(--radius-sm);letter-spacing:.2px;white-space:nowrap;flex-shrink:0;font-weight:700}
  .pane-hdr .live-dot{width:6px;height:6px;border-radius:50%;background:var(--success);box-shadow:0 0 0 2px var(--success-bg);animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

  /* ============ SIGNALS RAIL (LEFT) ============ */
  #signals-pane{grid-area:signals}
  #signals-list{flex:1;overflow-y:auto;padding:8px}
  .sig-tile{padding:9px 12px 10px;border:1px solid var(--border);border-radius:var(--radius-md);margin-bottom:5px;background:var(--surface);cursor:pointer;transition:all .15s;position:relative}
  .sig-tile:hover{border-color:var(--primary);box-shadow:var(--shadow-sm);transform:translateX(1px)}
  .sig-tile .row1{display:flex;align-items:center;gap:6px;margin-bottom:3px}
  .sig-tile .src-icon{font-size:13px;line-height:1}
  .sig-tile .src-name{font-size:12px;font-weight:700;color:var(--text);letter-spacing:.1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:110px}
  .sig-tile .status-dot{width:7px;height:7px;border-radius:50%;margin-left:auto;flex-shrink:0}
  .status-dot.live{background:var(--success);box-shadow:0 0 0 2px var(--success-bg)}
  .status-dot.synthetic{background:var(--info);box-shadow:0 0 0 2px var(--info-bg)}
  .status-dot.stale{background:var(--text-secondary);box-shadow:0 0 0 2px #e5e7eb}
  .status-dot.error{background:var(--danger);box-shadow:0 0 0 2px var(--danger-bg)}
  .sig-tile .row2{display:flex;align-items:baseline;gap:6px}
  .sig-tile .count{font-size:18px;font-weight:800;color:var(--primary-dark);font-variant-numeric:tabular-nums;line-height:1.1}
  .sig-tile .count.zero{color:var(--text-secondary)}
  .sig-tile .unit{font-size:10px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.4px;font-weight:700}
  .sig-tile .row3{font-size:10px;color:var(--text-secondary);margin-top:2px;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sig-tile .row4{display:flex;align-items:center;gap:6px;margin-top:5px;padding-top:4px;border-top:1px dashed var(--border)}
  .sig-tile .row4 .ts{font-size:9.5px;color:var(--text-secondary);font-variant-numeric:tabular-nums;letter-spacing:.2px;font-weight:600}
  .sig-tile .row4 .ts::before{content:"⏱";margin-right:3px;opacity:.55;font-size:9px}
  .sig-tile .row4 .status-text{margin-left:auto;font-size:9px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.5px;font-weight:700}
  .sig-tile .row4 .status-text.live{color:var(--success)}
  .sig-tile .row4 .status-text.synthetic{color:var(--info)}
  .sig-tile .row4 .status-text.stale{color:var(--text-secondary)}
  .sig-tile .row4 .status-text.error{color:var(--danger)}
  .sig-tile.alert{border-left:3px solid var(--warning);background:#fffdf7}
  .sig-tile.critical{border-left:3px solid var(--danger);background:#fff8f8}
  .sig-loading{color:var(--text-secondary);font-size:11px;font-style:italic;text-align:center;padding:24px 12px;letter-spacing:.2px}
  .sig-error{padding:18px 14px;text-align:center;border:1px dashed #fecaca;background:var(--danger-bg);border-radius:var(--radius-md);margin:8px 4px;color:#7f1d1d}
  .sig-error-title{font-weight:800;font-size:12px;margin-bottom:4px;letter-spacing:.2px}
  .sig-error-sub{font-family:"SF Mono",Menlo,monospace;font-size:10px;color:#991b1b;margin-bottom:8px;word-break:break-word}
  .sig-error-hint{font-size:10.5px;color:#7f1d1d;line-height:1.4;opacity:.85}

  /* ============ SIGNAL DRILL-IN DRAWER ============ */
  #signal-drawer{position:fixed;top:64px;right:22px;bottom:22px;width:460px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);box-shadow:0 12px 40px rgba(0,0,0,.18);display:none;flex-direction:column;z-index:995;overflow:hidden}
  #signal-drawer.open{display:flex}
  #signal-drawer .sd-hdr{padding:14px 18px;border-bottom:1px solid var(--border);background:var(--nav-bg);color:#fff;display:flex;align-items:center;gap:10px;flex-shrink:0}
  #signal-drawer .sd-icon{font-size:20px;line-height:1}
  #signal-drawer .sd-title{flex:1;font-size:14px;font-weight:800;letter-spacing:.2px}
  #signal-drawer .sd-meta{font-size:10px;color:#a5b4fc;text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
  #signal-drawer .sd-close{background:rgba(255,255,255,.1);color:#fff;border:0;border-radius:var(--radius-sm);padding:5px 11px;font-family:inherit;font-size:13px;cursor:pointer}
  #signal-drawer .sd-close:hover{background:rgba(255,255,255,.2)}
  #signal-drawer .sd-link{display:inline-flex;align-items:center;gap:5px;background:var(--primary);color:#fff;text-decoration:none;border-radius:var(--radius-sm);padding:5px 11px;font-family:inherit;font-size:11.5px;font-weight:700;letter-spacing:.2px;transition:background .15s;margin-right:6px}
  #signal-drawer .sd-link:hover{background:var(--primary-dark)}
  #signal-drawer .sd-link .arrow{font-size:11px;opacity:.85}
  #signal-drawer .sd-body{flex:1;overflow:auto;padding:14px 18px;font-size:12px;color:var(--text)}
  #signal-drawer .sd-loading{padding:30px;text-align:center;color:var(--text-secondary);font-style:italic}
  #signal-drawer .sd-summary{padding:10px 14px;background:var(--page-bg);border:1px solid var(--border);border-radius:var(--radius-md);margin-bottom:12px;font-size:12px;line-height:1.5}
  #signal-drawer .sd-summary .label{font-size:10px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.5px;font-weight:800;margin-bottom:2px}
  #signal-drawer .sd-raw{background:var(--page-bg);border:1px solid var(--border);border-radius:var(--radius-md);padding:12px 14px;font-family:"SF Mono",Menlo,monospace;font-size:11px;line-height:1.55;white-space:pre-wrap;word-break:break-word;color:var(--text);max-height:calc(100vh - 280px);overflow-y:auto}

  /* ============ BRIEF PANE (CENTER) ============ */
  #brief-pane{grid-area:brief;border-left:0;border-right:0}
  #brief-content{flex:1;overflow-y:auto;padding:22px 32px}
  .brief-empty{color:var(--text-secondary);text-align:center;padding:60px 30px 80px;font-size:13px;line-height:1.6}
  .brief-empty .moon{font-size:42px;line-height:1;margin-bottom:14px;opacity:.85}
  .brief-empty .title{color:var(--text);font-weight:800;font-size:18px;margin-bottom:8px;letter-spacing:-.2px}
  .brief-empty .sub{font-size:12.5px;color:var(--text-secondary);max-width:460px;margin:0 auto 20px;line-height:1.55}
  .brief-empty .kbd{display:inline-block;font-family:"SF Mono",Menlo,monospace;font-size:11px;background:var(--page-bg);border:1px solid var(--border);border-bottom-width:2px;border-radius:var(--radius-sm);padding:1px 6px;margin:0 2px;color:var(--text)}
  .brief-empty .cta{display:inline-flex;align-items:center;gap:7px;background:var(--primary);color:#fff;border:0;padding:10px 18px;border-radius:var(--radius-md);font-family:inherit;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:.2px;box-shadow:0 4px 14px rgba(0,97,255,.25);transition:all .15s}
  .brief-empty .cta:hover{background:var(--primary-dark);transform:translateY(-1px);box-shadow:0 6px 18px rgba(0,97,255,.32)}
  .brief-empty .hint{margin-top:14px;font-size:11px;color:var(--text-secondary);opacity:.8}
  .brief-msg{margin-bottom:18px}
  .brief-msg.user{background:var(--primary-bg);border-left:3px solid var(--primary);color:var(--primary-dark);font-weight:600;padding:10px 14px;border-radius:0 var(--radius-md) var(--radius-md) 0;font-size:13px}
  .brief-msg.user::before{content:"INPUT · ";font-size:9px;font-weight:800;letter-spacing:.5px;opacity:.7}
  .brief-msg.assistant h1{font-size:20px;font-weight:800;color:var(--text);border-bottom:2px solid var(--primary);padding-bottom:8px;margin:0 0 12px;letter-spacing:-.3px}
  .brief-msg.assistant h2{font-size:14px;font-weight:800;color:var(--text);margin:18px 0 6px;text-transform:uppercase;letter-spacing:.4px;border-left:3px solid var(--primary);padding-left:8px}
  .brief-msg.assistant h3{font-size:13px;font-weight:700;color:var(--text);margin:12px 0 4px}
  .brief-msg.assistant p,.brief-msg.assistant li{font-size:13.5px;line-height:1.6;margin:5px 0;color:var(--text)}
  .brief-msg.assistant ul,.brief-msg.assistant ol{padding-left:22px}
  .brief-msg.assistant code{background:var(--primary-bg);color:var(--primary-dark);padding:1px 6px;border-radius:var(--radius-sm);font-size:12px;font-family:"SF Mono",Menlo,monospace}
  .brief-msg.assistant pre{background:var(--nav-bg);color:#e6f0ff;padding:12px 14px;border-radius:var(--radius-md);overflow-x:auto;font-size:12px;line-height:1.5}
  .brief-msg.assistant pre code{background:transparent;color:inherit;padding:0;font-size:inherit}
  .brief-msg.assistant table{border-collapse:collapse;width:100%;font-size:12.5px;margin:10px 0;border:1px solid var(--border);border-radius:var(--radius-md);overflow:hidden}
  .brief-msg.assistant th{background:var(--page-bg);color:var(--text);padding:8px 10px;text-align:left;border-bottom:1px solid var(--border);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
  .brief-msg.assistant td{padding:7px 10px;border-bottom:1px solid var(--border);font-variant-numeric:tabular-nums}
  .brief-msg.assistant tr:last-child td{border-bottom:0}
  .brief-msg.assistant blockquote{border-left:4px solid var(--warning);background:var(--warning-bg);padding:10px 14px;margin:10px 0;border-radius:0 var(--radius-md) var(--radius-md) 0;color:#78350f}
  /* Critic-FAIL banner — detected by leading ⚠️ and the literal phrase */
  .brief-msg.assistant blockquote.fail-banner{border-left-color:var(--danger);background:var(--danger-bg);color:#7f1d1d}
  .brief-msg.assistant blockquote p{margin:4px 0}
  .brief-msg.error{background:var(--danger-bg);border-left:3px solid var(--danger);color:var(--danger);padding:12px 14px;border-radius:0 var(--radius-md) var(--radius-md) 0;font-size:13px}
  .brief-thinking{color:var(--text-secondary);font-style:italic;font-size:12.5px;padding:10px 14px;background:var(--page-bg);border-radius:var(--radius-md);display:inline-flex;align-items:center;gap:8px}
  .brief-thinking::before{content:"";width:8px;height:8px;background:var(--primary);border-radius:50%;animation:pulse 1s infinite}

  /* Live Action Queue rendered inline inside the brief (replaces Composer's
     static markdown table). Status column reflects right-rail approve/reject. */
  .inline-actions{margin:10px 0 18px;border:1px solid var(--border);border-radius:var(--radius-md);overflow:hidden}
  .inline-actions table{margin:0;width:100%;border-collapse:collapse;font-size:12px}
  .inline-actions th{background:var(--page-bg);color:var(--text);padding:8px 10px;text-align:left;border-bottom:1px solid var(--border);font-weight:700;font-size:10.5px;text-transform:uppercase;letter-spacing:.4px}
  .inline-actions td{padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top}
  .inline-actions tr:last-child td{border-bottom:0}
  .inline-actions tr.s-approved{background:var(--success-bg)}
  .inline-actions tr.s-rejected{background:var(--page-bg);color:var(--text-secondary);text-decoration:line-through}
  .inline-actions tr.s-approved td:last-child,.inline-actions tr.s-rejected td:last-child{text-decoration:none}
  .inline-actions .status-pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:9.5px;font-weight:800;text-transform:uppercase;letter-spacing:.5px}
  .inline-actions .status-pill.pending{background:var(--info-bg);color:var(--info)}
  .inline-actions .status-pill.approved{background:var(--success-bg);color:#15803d}
  .inline-actions .status-pill.rejected{background:var(--danger-bg);color:#7f1d1d}
  .inline-actions .sev-cell{font-size:9.5px;font-weight:800;letter-spacing:.4px;text-transform:uppercase;color:var(--text-secondary)}
  .inline-actions .sev-cell.high{color:var(--danger)}
  .inline-actions .sev-cell.med{color:var(--warning)}
  .inline-actions .sev-cell.low{color:var(--info)}
  .inline-actions .ia-empty{padding:18px;text-align:center;color:var(--text-secondary);font-size:12px;font-style:italic}
  .ia-summary{display:flex;gap:14px;padding:8px 12px;background:var(--page-bg);border-bottom:1px solid var(--border);font-size:11px;color:var(--text-secondary)}
  .ia-summary strong{color:var(--text);font-weight:800;font-variant-numeric:tabular-nums;margin-right:4px}

  /* object/citation chips */
  .obj-chip{display:inline-flex;align-items:center;gap:4px;background:var(--primary-bg);color:var(--primary-dark);padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;cursor:pointer;margin:0 1px;border:1px solid #c7dbff;font-variant-numeric:tabular-nums;letter-spacing:.2px;vertical-align:baseline}
  .obj-chip:hover{background:var(--primary);color:#fff}
  .obj-chip::before{content:"◆";font-size:9px;opacity:.7}
  .cite-chip{display:inline-flex;align-items:center;background:var(--info-bg);color:var(--info);padding:0 5px;border-radius:var(--radius-sm);font-size:9px;font-weight:800;cursor:pointer;margin:0 1px;border:1px solid #bae6fd;letter-spacing:.3px;vertical-align:super;line-height:1.4;font-variant-numeric:tabular-nums}
  .cite-chip:hover{background:var(--info);color:#fff}

  /* ============ ACTIONS RAIL (RIGHT) ============ */
  #actions-pane{grid-area:actions}
  #actions-list{flex:1;overflow-y:auto;padding:10px}
  .action-empty{color:var(--text-secondary);font-size:11.5px;text-align:center;padding:30px 16px;line-height:1.5}
  .action-card{border:1px solid var(--border);border-radius:var(--radius-md);padding:11px 12px 11px 14px;margin-bottom:8px;background:var(--surface);position:relative;transition:opacity .3s,border-color .15s}
  .action-card:hover{border-color:#cbd5e1}
  .action-card.approved{opacity:.6;background:var(--success-bg);border-color:#86efac}
  .action-card.rejected{opacity:.45;background:var(--page-bg);text-decoration:line-through;color:var(--text-secondary)}
  .action-card .sev{position:absolute;top:0;left:0;bottom:0;width:4px;border-radius:var(--radius-md) 0 0 var(--radius-md)}
  .action-card .sev.high{background:var(--danger)}
  .action-card .sev.med{background:var(--warning)}
  .action-card .sev.low{background:var(--info)}
  .action-card .type-pill{display:inline-block;font-size:9px;font-weight:800;padding:2px 7px;border-radius:var(--radius-sm);background:var(--primary-bg);color:var(--primary-dark);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px;border:0}
  .action-card .title{font-size:12.5px;font-weight:700;color:var(--text);line-height:1.4;margin-bottom:5px}
  .action-card .detail{font-size:11.5px;color:var(--text-secondary);line-height:1.5;margin-bottom:9px}
  .action-card .btns{display:flex;gap:6px}
  .action-card button{flex:1;font-family:inherit;font-size:11px;font-weight:700;padding:5px 8px;border-radius:var(--radius-sm);cursor:pointer;border:1px solid;letter-spacing:.2px}
  .action-card button.approve{background:var(--success);color:#fff;border-color:var(--success)}
  .action-card button.approve:hover{background:#15803d}
  .action-card button.reject{background:var(--surface);color:var(--text-secondary);border-color:var(--border)}
  .action-card button.reject:hover{background:var(--danger-bg);color:var(--danger);border-color:var(--danger)}
  .action-card.approved .status-tag,.action-card.rejected .status-tag{display:block;font-size:10px;font-weight:800;text-align:center;padding:4px;background:rgba(255,255,255,.5);border-radius:var(--radius-sm);letter-spacing:.4px}
  .action-card .status-tag{display:none}

  /* ============ TIMELINE (now inside debug panel) ============
     Was a bottom-of-grid pane; now lives inside the Debug slide-out (#debug-panel).
     pushTimeline() still appends rows here so the agent's working is captured —
     just hidden behind 🔍 until the operator wants to inspect it. */
  #timeline-list{font-family:"SF Mono",Menlo,monospace;font-size:11px;line-height:1.5;background:rgba(0,0,0,.15);border-radius:var(--radius-sm);padding:8px 10px;max-height:60vh;overflow-y:auto;margin-top:6px}
  .tl-row{display:flex;gap:10px;padding:2px 0;align-items:baseline;border-bottom:1px dashed transparent}
  .tl-row:hover{background:rgba(255,255,255,.04);border-radius:var(--radius-sm)}
  .tl-row .tl-ts{color:#9ca3af;font-variant-numeric:tabular-nums;font-size:10.5px;flex-shrink:0;min-width:62px}
  .tl-row .tl-actor{font-weight:700;flex-shrink:0;min-width:130px}
  .tl-row .tl-actor.supervisor{color:#93c5fd}
  .tl-row .tl-actor.data{color:#7dd3fc}
  .tl-row .tl-actor.scout{color:#c4b5fd}
  .tl-row .tl-actor.historian{color:#fcd34d}
  .tl-row .tl-actor.composer{color:#86efac}
  .tl-row .tl-actor.critic{color:#fca5a5}
  .tl-row .tl-actor.tool{color:#d1d5db}
  .tl-row .tl-msg{color:#e5e7eb;flex:1;word-break:break-word;font-size:11px}
  .tl-row.tl-live{background:rgba(96,165,250,.12);border-radius:var(--radius-sm)}
  .tl-row.tl-live .tl-msg{color:#cbd5e1;font-style:normal;max-height:36px;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;white-space:pre-wrap}
  .tl-row.tl-live .tl-actor{animation:pulse 1.5s infinite}
  .tl-empty{color:#9ca3af;font-style:italic;text-align:center;padding:14px;font-family:"Nunito Sans",sans-serif;font-size:12px}

  /* ============ INLINE SCENARIOS + COMPOSER (BOTTOM OF BRIEF PANE) ============ */
  .brief-scenarios{padding:8px 14px;border-top:1px solid var(--border);background:var(--page-bg);display:flex;gap:6px;flex-wrap:wrap;flex-shrink:0;align-items:center}
  .brief-scenarios button{font-family:inherit;font-size:11px;padding:5px 11px;border:1px solid var(--border);background:var(--surface);border-radius:999px;cursor:pointer;color:var(--text);font-weight:600;line-height:1.3;transition:all .12s;white-space:nowrap}
  .brief-scenarios button:hover{background:var(--primary-bg);color:var(--primary-dark);border-color:var(--primary)}
  .brief-scenarios button.primary{background:var(--primary);color:#fff;border-color:var(--primary);font-weight:700}
  .brief-scenarios button.primary:hover{background:var(--primary-dark);border-color:var(--primary-dark);color:#fff}
  .brief-composer{padding:10px 14px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:flex-end;background:var(--surface);flex-shrink:0}
  .brief-composer textarea{flex:1;font-family:inherit;font-size:13px;padding:9px 12px;border:1px solid var(--border);border-radius:var(--radius-md);resize:none;min-height:42px;max-height:140px;color:var(--text);line-height:1.45}
  .brief-composer textarea:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-bg)}
  .brief-composer button{font-family:inherit;font-size:13px;font-weight:700;padding:0 22px;background:var(--primary);color:#fff;border:0;border-radius:var(--radius-md);cursor:pointer;letter-spacing:.2px;min-height:42px;transition:all .15s}
  .brief-composer button:hover{background:var(--primary-dark)}
  .brief-composer button:disabled{opacity:.5;cursor:not-allowed}
  /* Chat-style bubbles inline in the brief pane */
  .brief-msg.user-bubble{background:var(--primary-bg);border:1px solid #c7dbff;color:var(--primary-dark);font-weight:600;padding:9px 14px;border-radius:14px 14px 4px 14px;font-size:13px;max-width:80%;margin-left:auto;margin-bottom:14px;line-height:1.45;width:fit-content;clear:both}
  .brief-msg.assistant-chat{background:#fafbfc;border:1px solid var(--border);padding:10px 14px;border-radius:14px 14px 14px 4px;font-size:13px;line-height:1.55;max-width:88%;margin-bottom:14px;color:var(--text);clear:both;width:fit-content}
  .brief-msg.assistant-chat p{margin:4px 0 6px;font-size:13px;line-height:1.55}
  .brief-msg.assistant-chat p:first-child{margin-top:0}
  .brief-msg.assistant-chat p:last-child{margin-bottom:0}
  .brief-msg.assistant-chat strong{font-weight:800;color:var(--text)}
  .brief-msg.assistant-chat ul,.brief-msg.assistant-chat ol{margin:4px 0;padding-left:22px}
  .brief-msg.assistant-chat li{font-size:13px;line-height:1.55;margin:2px 0}
  .brief-msg.assistant-chat code{background:var(--primary-bg);color:var(--primary-dark);padding:1px 6px;border-radius:var(--radius-sm);font-size:11.5px;font-family:"SF Mono",Menlo,monospace}
  .brief-msg.assistant-chat h1,.brief-msg.assistant-chat h2,.brief-msg.assistant-chat h3{font-size:13.5px;font-weight:800;margin:8px 0 4px;color:var(--text)}
  .brief-msg.assistant-chat blockquote{border-left:3px solid var(--primary);background:var(--primary-bg);padding:6px 10px;margin:6px 0;color:var(--primary-dark);border-radius:0 var(--radius-sm) var(--radius-sm) 0;font-size:12.5px}
  .brief-msg.assistant-chat table{border-collapse:collapse;font-size:12px;margin:6px 0;border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden}
  .brief-msg.assistant-chat th{background:var(--page-bg);padding:5px 8px;font-weight:700;font-size:10.5px;text-transform:uppercase;letter-spacing:.3px;text-align:left;border-bottom:1px solid var(--border)}
  .brief-msg.assistant-chat td{padding:5px 8px;border-bottom:1px solid var(--border);font-variant-numeric:tabular-nums}
  .brief-msg.assistant-chat details.chat-code-details summary::marker{content:""}
  .brief-msg.assistant-chat details.chat-code-details summary{list-style:none}
  .brief-msg.assistant-chat details.chat-code-details[open] summary{margin-bottom:0}
  .brief-msg.assistant-chat details.chat-code-details[open] summary::before{content:"▼ ";font-size:9px}
  .brief-msg.thinking-inline{background:var(--page-bg);border:1px dashed var(--border);padding:10px 14px 10px 36px;border-radius:14px;font-size:12.5px;color:var(--text-secondary);margin-bottom:14px;position:relative;max-width:78%;clear:both;width:fit-content;display:flex;align-items:center;gap:10px;flex-wrap:wrap;line-height:1.5}
  .brief-msg.thinking-inline::before{content:"";position:absolute;left:12px;top:50%;transform:translateY(-50%);width:10px;height:10px;background:var(--primary);border-radius:50%;animation:pulse 1.1s infinite}
  .brief-msg.thinking-inline .thinking-actor{font-weight:800;color:var(--primary-dark);font-style:normal;letter-spacing:.2px;padding:1px 7px;background:var(--primary-bg);border-radius:999px;font-size:11px;text-transform:uppercase;border:1px solid #c7dbff}
  .brief-msg.thinking-inline .thinking-text{font-style:italic;color:var(--text);min-width:0}
  .brief-msg.thinking-inline .thinking-elapsed{font-style:normal;font-size:10.5px;color:var(--text-secondary);font-variant-numeric:tabular-nums;margin-left:auto;background:var(--surface);padding:2px 8px;border-radius:999px;border:1px solid var(--border);font-weight:700}

  /* ============ DEBUG SLIDE-OUT ============ */
  #debug-panel{display:none;position:fixed;top:0;right:0;bottom:0;width:540px;background:var(--nav-bg);color:#cbd5e1;padding:18px 20px;overflow:auto;font-family:"Nunito Sans",sans-serif;font-size:11.5px;line-height:1.4;box-shadow:-8px 0 32px rgba(0,0,0,.35);z-index:200;transform:translateX(100%);transition:transform .25s ease-out}
  #debug-panel.open{display:flex;flex-direction:column;transform:translateX(0)}
  #debug-panel .dbg-title{font-size:11px;font-weight:800;text-transform:uppercase;color:#a5b4fc;letter-spacing:.6px;margin-bottom:10px;display:flex;align-items:center;gap:8px}
  #debug-panel .dbg-title .live-dot{width:7px;height:7px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 2px rgba(34,197,94,.25);animation:pulse 2s infinite}
  #debug-panel .dbg-btns{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap;flex-shrink:0}
  #debug-panel button{font-family:inherit;font-size:11px;padding:5px 11px;border:0;border-radius:var(--radius-sm);cursor:pointer;font-weight:700;letter-spacing:.2px;transition:filter .12s}
  #debug-panel button:hover{filter:brightness(1.1)}
  #debug-panel button.active{box-shadow:0 0 0 2px rgba(255,255,255,.35) inset}
  .dbg-btn-y{background:#fcd34d;color:#0f1729}
  .dbg-btn-r{background:#fca5a5;color:#0f1729}
  .dbg-btn-b{background:#a5b4fc;color:#0f1729}
  .dbg-btn-g{background:#86efac;color:#0f1729}
  .dbg-btn-x{background:#374151;color:#fff;margin-left:auto}
  #debug-panel pre{margin:0;background:rgba(0,0,0,.15);color:#e6f0ff;padding:10px 12px;font-size:11px;border-radius:var(--radius-sm);overflow:auto;white-space:pre-wrap;flex:1;font-family:"SF Mono",Menlo,monospace;line-height:1.45}
  #debug-panel .dbg-body{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
  #debug-panel .dbg-meta{font-size:10.5px;color:#94a3b8;margin-bottom:6px;display:flex;align-items:center;gap:8px}
  #debug-panel .dbg-meta .count-pill{background:rgba(165,180,252,.15);color:#c7d2fe;padding:1px 8px;border-radius:999px;font-weight:700;font-variant-numeric:tabular-nums}

  /* ============ RESPONSIVE ============ */
  @media (max-width:1280px){
    header{gap:10px;padding:10px 16px}
    header .sev-wrap{margin-left:10px}
  }
  @media (max-width:1100px){
    .console{grid-template-columns:180px 1fr;grid-template-areas:"signals brief" "actions actions"}
    #actions-pane{max-height:160px}
    header .sev-bar{width:80px}
    header .sev-num{display:none}
    header .sev-wrap{margin-left:8px;padding:3px 10px}
  }
  @media (max-width:700px){
    .console{grid-template-columns:1fr;grid-template-areas:"brief" "signals" "actions"}
    #signals-pane{max-height:200px}
    header h1{font-size:14px}
    header .sev-label,header .sev-bar{display:none}
    header .clock-sub{display:none}
  }
</style>
</head>
<body>
<header>
  <span class="logo">🌙</span>
  <h1>The 11 PM Ops Brief</h1>
  <div class="sev-wrap" id="sev-wrap" title="Click for posture-band definitions">
    <span class="sev-label">Posture</span>
    <div class="sev-bar"><div class="sev-fill" id="sev-fill"></div></div>
    <span class="sev-pill" id="sev-pill">IDLE</span>
    <span class="sev-num" id="sev-num">—</span>
    <!-- Click-to-open popover with the 3-band ops escalation legend -->
    <div id="posture-popover" onclick="event.stopPropagation()">
      <div class="pop-title">
        <span>Ops Posture · what the bands mean</span>
        <button onclick="closePosturePopover()" title="Close">×</button>
      </div>
      <div class="pop-band">
        <span class="pop-dot" style="background:#16a34a;box-shadow:0 0 0 2px #dcfce7"></span>
        <div style="flex:1">
          <div class="pop-label"><span style="color:#15803d">NOMINAL</span><span class="pop-range">0 – 3.9</span></div>
          <div class="pop-desc">Baseline. Log only. Reviewed at the next ops standup. No paging, no Slack ping.</div>
        </div>
      </div>
      <div class="pop-band">
        <span class="pop-dot" style="background:#b45309;box-shadow:0 0 0 2px #fef3c7"></span>
        <div style="flex:1">
          <div class="pop-label"><span style="color:#92400e">ELEVATED</span><span class="pop-range">4.0 – 6.9</span></div>
          <div class="pop-desc">Slack the operations channel, monitor every two hours, brief CSMs on at-risk enterprise accounts. No SLA credits yet.</div>
        </div>
      </div>
      <div class="pop-band">
        <span class="pop-dot" style="background:#dc2626;box-shadow:0 0 0 2px #fee2e2"></span>
        <div style="flex:1">
          <div class="pop-label"><span style="color:#991b1b">CRITICAL</span><span class="pop-range">7.0 – 10.0</span></div>
          <div class="pop-desc">Page on-call, pre-authorize SLA credits, director approval required for reroutes &gt; $50K impact, executive update by 6 AM.</div>
        </div>
      </div>
      <div class="pop-foot">
        Calibration score is a 0 – 100 internal scale (shown next to the pill) — it captures both internal at-risk volume and external signal pressure. The band is what maps to ops behavior; the number is for trend lines.
      </div>
    </div>
  </div>
  <span class="spacer"></span>
  <div style="display:flex;flex-direction:column;align-items:flex-end;line-height:1.15">
    <span class="clock" id="clock">--:--:--</span>
    <span class="clock-sub" id="clock-date">Loading…</span>
  </div>
  <button class="icon-btn" id="export-md-btn"  title="Download brief snapshot as Markdown">📋 .md</button>
  <button class="icon-btn" id="export-pdf-btn" title="Download brief snapshot as PDF">📄 PDF</button>
  <button class="icon-btn" id="debug-btn" title="Health, logs, errors">🔍</button>
</header>

<div class="console">
  <!-- LEFT: SIGNALS -->
  <div class="pane" id="signals-pane">
    <div class="pane-hdr">
      <span class="live-dot"></span>
      Signal Sources
      <span class="count" id="sig-count">loading…</span>
    </div>
    <div id="signals-list">
      <div class="sig-loading">Polling 17 sources…</div>
    </div>
  </div>

  <!-- CENTER: BRIEF + CHAT (unified) -->
  <div class="pane" id="brief-pane">
    <div class="pane-hdr">
      Operational Console
      <span class="count" id="brief-status">idle</span>
    </div>
    <div id="brief-content">
      <div class="brief-empty">
        <div class="moon">🌙</div>
        <div class="title">Nothing flagged yet tonight.</div>
        <div class="sub">17 live signal sources are streaming on the left. Generate the full ops brief whenever you're ready — supervisor will fan out to data, signals, and historian in parallel.</div>
        <button class="cta" id="generate-brief-cta">🎯 Generate tonight's brief</button>
        <div class="hint">…or use one of the quick scenarios below.</div>
      </div>
    </div>
    <!-- Inline scenarios + composer (sticky to bottom of brief pane) -->
    <div class="brief-scenarios">
      <button class="primary" data-q="Generate tonight's full ops brief. Pull internal data via Genie, fetch fresh external signals (NWS, GDELT, PortWatch, Aviation PIREPs), and compose the final markdown brief with severity, themes, and actions.">🎯 Tonight's brief</button>
      <button data-q="How many shipments are currently in flight?">⚡ In-flight</button>
      <button data-q="Are there any active severe weather alerts for TN, TX, FL, or GA right now?">🌩️ NWS</button>
      <button data-q="Is FedEx Memphis operating normally tonight? Check Aviation PIREPs and NWS for any hub disruption.">✈️ MEM hub</button>
      <button data-q="Looking at the last 3 nights of ops briefs, are any disruption themes repeating? Highlight any pattern and explain why it matters.">🔁 Recurring</button>
    </div>
    <div class="brief-composer">
      <textarea id="prompt" placeholder="Ask the agent anything about tonight's ops…  (Enter to send, Shift+Enter for newline)"></textarea>
      <button id="send">Send</button>
    </div>
  </div>

  <!-- RIGHT: ACTIONS -->
  <div class="pane" id="actions-pane">
    <div class="pane-hdr">
      Pending Actions
      <span class="count" id="actions-count">0</span>
    </div>
    <div id="actions-list">
      <div class="action-empty">No pending approvals.<br>Actions surface here when the agent flags items that need human review.</div>
    </div>
  </div>

</div>

<!-- Debug slide-out — Health · Logs · Errors · Agent Timeline (the dev backstage) -->
<div id="debug-panel">
  <div class="dbg-title"><span class="live-dot"></span> Developer Console</div>
  <div class="dbg-btns">
    <button class="dbg-btn-g active" data-tab="timeline" onclick="showDebugTab('timeline')">🧵 Timeline</button>
    <button class="dbg-btn-y" data-tab="health" onclick="showDebugTab('health')">❤️ Health</button>
    <button class="dbg-btn-y" data-tab="logs" onclick="showDebugTab('logs')">📜 Logs</button>
    <button class="dbg-btn-r" data-tab="errors" onclick="showDebugTab('errors')">🚨 Errors</button>
    <button class="dbg-btn-b" onclick="copyDebug()">📋 Copy</button>
    <button class="dbg-btn-x" onclick="document.getElementById('debug-panel').classList.remove('open')">×</button>
  </div>
  <div class="dbg-body">
    <!-- TIMELINE tab — populated by pushTimeline() while the agent streams -->
    <div id="dbg-tab-timeline" class="dbg-tab">
      <div class="dbg-meta">Agent activity (supervisor + subagents)<span class="count-pill" id="tl-count">0 events</span></div>
      <div id="timeline-list">
        <div class="tl-empty">Supervisor and subagent activity will appear here once a query runs.</div>
      </div>
    </div>
    <!-- HEALTH / LOGS / ERRORS tabs share a single <pre>. -->
    <div id="dbg-tab-pre" class="dbg-tab" style="display:none">
      <div class="dbg-meta" id="dbg-pre-meta">—</div>
      <pre id="debug-content">click Health, Logs, or Errors above.</pre>
    </div>
  </div>
</div>

<script>
// ============================================================
// STATE
// ============================================================
// Each source carries a `url` to its primary upstream page so the operator
// can cross-check the agent's read against the authoritative live site.
// (Surfaced as "View at source ↗" in the drill-in drawer.)
const SOURCES = [
  {id:'nws',         label:'NWS Alerts',        icon:'🌩️', url:'https://www.weather.gov/alerts'},
  {id:'gdelt',       label:'GDELT',             icon:'📰', url:'https://www.gdeltproject.org/'},
  {id:'portwatch',   label:'IMF PortWatch',     icon:'⚓', url:'https://portwatch.imf.org/'},
  {id:'usgs',        label:'USGS Quakes',       icon:'🌐', url:'https://earthquake.usgs.gov/earthquakes/map/'},
  {id:'fema',        label:'FEMA',              icon:'🚨', url:'https://www.fema.gov/disasters'},
  {id:'eonet',       label:'NASA EONET',        icon:'🛰️', url:'https://eonet.gsfc.nasa.gov/'},
  {id:'reddit',      label:'Reddit',            icon:'💬', url:'https://www.reddit.com/r/supplychain/new/'},
  {id:'gnews',       label:'Google News',       icon:'📡', url:'https://news.google.com/search?q=supply+chain+disruption'},
  {id:'faa_tfr',     label:'FAA TFRs',          icon:'🛫', url:'https://tfr.faa.gov/tfr2/list.html'},
  {id:'cbp',         label:'CBP Borders',       icon:'🛂', url:'https://bwt.cbp.gov/'},
  {id:'hn',          label:'HackerNews',        icon:'🟧', url:'https://news.ycombinator.com/'},
  {id:'eia',         label:'EIA Fuel',          icon:'⛽', url:'https://www.eia.gov/petroleum/gasdiesel/'},
  {id:'volcano',     label:'USGS Volcano',      icon:'🌋', url:'https://volcanoes.usgs.gov/volcanoes/'},
  {id:'nhc',         label:'NOAA Hurricanes',   icon:'🌀', url:'https://www.nhc.noaa.gov/'},
  {id:'fda_recalls', label:'FDA Recalls',       icon:'⚠️', url:'https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts'},
  {id:'chokepoints', label:'Choke Points',      icon:'🚢', url:'https://portwatch.imf.org/pages/maritime-chokepoints'},
  {id:'pireps',      label:'Aviation PIREPs',   icon:'✈️', url:'https://aviationweather.gov/data/pirep/'},
];
// OpenSky removed — endpoint perpetually unreliable; Aviation PIREPs covers air-freight.
// DEMO_SIGNALS is INTENTIONALLY EMPTY in production. The previous version embedded
// hardcoded counts + 11:46 PM-style timestamps that leaked through whenever
// /signals/latest hiccuped, making us lie about source freshness. Kept the object
// itself so any rogue reference doesn't crash; signal failures now render an
// explicit "offline" state via renderSignalsErrorState() instead.
const DEMO_SIGNALS = {};
const ACTORS = {supervisor:'Supervisor',data:'DataAnalyst',scout:'SignalScout',historian:'Historian',composer:'Composer',critic:'Critic',tool:'tool'};
let sending = false;
let pendingActions = [];
let currentSeverity = null;
// Snapshot state — populated as the UI runs; used by the Export buttons.
let lastSignals = null;
let lastUserQuery = '';
let lastBriefMarkdown = '';
let lastBriefMessages = [];        // full subagent transcript (id, actor, text)
let lastBriefTimestamp = null;

// ============================================================
// HEADER: CLOCK + SEVERITY GAUGE
// ============================================================
function tickClock(){
  const now = new Date();
  const t = now.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
  const d = now.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'});
  document.getElementById('clock').textContent = t;
  document.getElementById('clock-date').textContent = d;
}
tickClock(); setInterval(tickClock,1000);

// Posture = 3-band ops escalation level.
//   NOMINAL  (score < 4)   → logged only, reviewed at standup
//   ELEVATED (4 ≤ score < 7) → Slack the team, monitor q2h, CSM outreach
//   CRITICAL (score ≥ 7)   → page on-call, pre-auth SLA credits, director approval
// The 0-10 number is kept as an internal calibration scale; the user-facing
// label is the band, because the band is what maps to ops behavior.
function postureFromScore(s){
  if (s == null || isNaN(s))      return {label:'IDLE',     color:'#9ca3af', bg:'rgba(255,255,255,.12)', txt:'#fff'};
  if (s < 4)                       return {label:'NOMINAL',  color:'#16a34a', bg:'#dcfce7',               txt:'#15803d'};
  if (s < 7)                       return {label:'ELEVATED', color:'#b45309', bg:'#fef3c7',               txt:'#92400e'};
                                   return {label:'CRITICAL', color:'#dc2626', bg:'#fee2e2',               txt:'#991b1b'};
}

function setSeverity(score){
  const fill = document.getElementById('sev-fill');
  const num  = document.getElementById('sev-num');
  const pill = document.getElementById('sev-pill');
  if (score == null || isNaN(score)) {
    fill.style.width = '0%';
    num.textContent = '—';
    pill.textContent = 'IDLE';
    pill.style.background = 'rgba(255,255,255,.12)';
    pill.style.color = '#fff';
    return;
  }
  const s = Math.max(0, Math.min(10, Number(score)));
  const p = postureFromScore(s);
  fill.style.width = (s*10) + '%';
  fill.style.background = p.color;
  // num shows the underlying calibration score for transparency; pill is the band label.
  num.textContent = Math.round(s * 10) + '/100';
  pill.textContent = p.label;
  pill.style.background = p.bg;
  pill.style.color = p.txt;
  currentSeverity = s;
}

// Posture popover — explains what NOMINAL/ELEVATED/CRITICAL actually mean
// in terms of ops behavior. The header pill is otherwise a mystery to a
// first-time judge/operator. Click anywhere on the .sev-wrap to toggle.
function openPosturePopover(){
  const pop = document.getElementById('posture-popover');
  if (pop) pop.classList.add('open');
}
function closePosturePopover(){
  const pop = document.getElementById('posture-popover');
  if (pop) pop.classList.remove('open');
}
window.openPosturePopover  = openPosturePopover;
window.closePosturePopover = closePosturePopover;
document.getElementById('sev-wrap').addEventListener('click', (e) => {
  // Don't toggle when the click is inside the popover itself.
  if (e.target.closest('#posture-popover')) return;
  const pop = document.getElementById('posture-popover');
  pop.classList.toggle('open');
});
// Click-outside closes the popover.
document.addEventListener('click', (e) => {
  const pop = document.getElementById('posture-popover');
  if (!pop || !pop.classList.contains('open')) return;
  if (e.target.closest('#sev-wrap')) return;
  pop.classList.remove('open');
});

function setSeverityState(state){
  // state ∈ {'running','error'} — visual states without a numeric score.
  const fill = document.getElementById('sev-fill');
  const num  = document.getElementById('sev-num');
  const pill = document.getElementById('sev-pill');
  if (state === 'running') {
    fill.style.width = '100%';
    fill.style.background = 'linear-gradient(90deg,#0061ff 0%,#3b82f6 50%,#0061ff 100%)';
    num.textContent = 'running…';
    pill.textContent = 'BUSY';
    pill.style.background = '#e0f2fe';
    pill.style.color = '#0369a1';
  } else if (state === 'error') {
    fill.style.width = '100%';
    fill.style.background = '#dc2626';
    num.textContent = 'ERROR';
    pill.textContent = 'ERROR';
    pill.style.background = '#fee2e2';
    pill.style.color = '#991b1b';
  }
}

// ============================================================
// SIGNAL TILES (LEFT RAIL)
// ============================================================
// Convert a server-stamped "HH:MM UTC" string into the operator's local time
// (e.g. "4:18 PM"). Falls through on unparseable input so we never blank out a tile.
function localizeUtcStamp(raw){
  if (!raw) return '';
  const m = String(raw).match(/^(\d{1,2}):(\d{2})\s*UTC$/i);
  if (!m) return raw;
  const now = new Date();
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(),
                              parseInt(m[1], 10), parseInt(m[2], 10), 0));
  return d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
}

function renderSignals(data){
  const list = document.getElementById('signals-list');
  list.innerHTML = '';
  let totalAlerts = 0;
  let liveCount = 0;
  for (const src of SOURCES){
    const s = (data && data[src.id]) || {count:0,status:'stale',ts:'—',detail:'(no data this cycle)'};
    totalAlerts += (s.count || 0);
    if (s.status === 'live') liveCount += 1;
    const tile = document.createElement('div');
    let cls = 'sig-tile';
    if (s.count >= 20) cls += ' critical';
    else if (s.count >= 10) cls += ' alert';
    tile.className = cls;
    tile.title = `${src.label} — click to see raw alerts  (fetched ${s.ts || '—'})`;
    tile.onclick = () => openSignalDrawer(src.id, src.label, src.icon);
    const tsLocal = localizeUtcStamp(s.ts) || '—';
    const status = s.status || 'stale';
    tile.innerHTML = `
      <div class="row1">
        <span class="src-icon">${src.icon}</span>
        <span class="src-name">${escapeHtml(src.label)}</span>
        <span class="status-dot ${status}" title="${escapeAttr(status)}"></span>
      </div>
      <div class="row2">
        <span class="count ${s.count===0?'zero':''}">${s.count ?? '—'}</span>
        <span class="unit">${s.count===1?'alert':'alerts'}</span>
      </div>
      <div class="row3">${escapeHtml(s.detail||'')}</div>
      <div class="row4">
        <span class="ts" title="Fetched ${escapeAttr(s.ts||'—')}">${escapeHtml(tsLocal)}</span>
        <span class="status-text ${status}">${escapeHtml(status)}</span>
      </div>
    `;
    list.appendChild(tile);
  }
  // Compact format — "6/17 · 107 alerts" — to keep the badge on one line.
  // The lowercase + missing "LIVE" keyword is intentional: the pulsing green
  // dot to the left of "Signal Sources" already conveys live status.
  document.getElementById('sig-count').textContent =
    `${liveCount}/${SOURCES.length} · ${totalAlerts} alerts`;
}

let _signalsErrorBackoff = 0;
async function refreshSignals(){
  try {
    const res = await fetch('/signals/latest', {cache:'no-store'});
    if (!res.ok) throw new Error('http ' + res.status);
    const j = await res.json();
    // accept either {sources:[{id,...}]} or {nws:{...}, gdelt:{...}}
    const map = {};
    if (Array.isArray(j.sources)) {
      for (const it of j.sources) map[it.id] = it;
    } else {
      Object.assign(map, j);
    }
    lastSignals = map;
    _signalsErrorBackoff = 0;
    renderSignals(map);
  } catch (e) {
    // DO NOT silently fall back to DEMO_SIGNALS in production — that leaks
    // hardcoded timestamps + counts and lies about source health. Render an
    // honest offline state instead. Demo data only when no signals have ever
    // arrived (so the empty UI doesn't look broken in pure-static preview).
    _signalsErrorBackoff += 1;
    if (lastSignals) {
      // Stale-while-revalidating — keep last good render, just flag header.
      document.getElementById('sig-count').textContent = 'stale · retrying';
      return;
    }
    renderSignalsErrorState(e.message || 'fetch failed');
  }
}

// ----------- Signal drill-in drawer (click a sig-tile → see raw alerts) -----------
function ensureSignalDrawer(){
  let d = document.getElementById('signal-drawer');
  if (d) return d;
  d = document.createElement('div');
  d.id = 'signal-drawer';
  d.innerHTML = `
    <div class="sd-hdr">
      <span class="sd-icon" id="sd-icon">📡</span>
      <div style="flex:1">
        <div class="sd-title" id="sd-title">Source</div>
        <div class="sd-meta" id="sd-meta">—</div>
      </div>
      <!-- "View at source" — opens the upstream primary site so the operator
           can cross-check what the agent saw against what the agency shows. -->
      <a class="sd-link" id="sd-link" href="#" target="_blank" rel="noopener noreferrer" title="Open the source's primary site in a new tab">
        Open source <span class="arrow">↗</span>
      </a>
      <button class="sd-close" onclick="closeSignalDrawer()" title="Close">×</button>
    </div>
    <div class="sd-body" id="sd-body"></div>`;
  document.body.appendChild(d);
  return d;
}

async function openSignalDrawer(sourceId, label, icon){
  const d = ensureSignalDrawer();
  document.getElementById('sd-icon').textContent = icon || '📡';
  document.getElementById('sd-title').textContent = label || sourceId;
  document.getElementById('sd-meta').textContent = 'fetching live alerts…';
  document.getElementById('sd-body').innerHTML = '<div class="sd-loading">Loading raw source output…</div>';
  // Wire the upstream "Open source ↗" link to whichever SOURCES entry this is.
  const srcCfg = SOURCES.find(s => s.id === sourceId);
  const linkEl = document.getElementById('sd-link');
  if (linkEl) {
    if (srcCfg && srcCfg.url) {
      linkEl.href = srcCfg.url;
      linkEl.style.display = '';
      linkEl.title = `Open ${srcCfg.label} at ${srcCfg.url}`;
    } else {
      linkEl.style.display = 'none';
    }
  }
  d.classList.add('open');
  try {
    const res = await fetch('/signals/' + encodeURIComponent(sourceId) + '/raw', {cache:'no-store'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const j = await res.json();
    const tsLocal = localizeUtcStamp(j.ts) || j.ts || '—';
    document.getElementById('sd-meta').textContent =
      `${(j.status || 'unknown').toUpperCase()} · ${j.count != null ? j.count : '—'} alerts · fetched ${tsLocal}`;
    const summary = j.detail ? `
      <div class="sd-summary">
        <div class="label">Headline</div>
        <div>${escapeHtml(j.detail)}</div>
      </div>` : '';
    document.getElementById('sd-body').innerHTML = summary +
      '<div class="label" style="font-size:10px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.5px;font-weight:800;margin:0 0 6px">Raw source output</div>' +
      '<div class="sd-raw">' + escapeHtml(j.raw || '(empty)') + '</div>';
  } catch (e) {
    document.getElementById('sd-meta').textContent = 'error';
    document.getElementById('sd-body').innerHTML =
      '<div class="sig-error"><div class="sig-error-title">⚠️ Could not load raw output</div>' +
      '<div class="sig-error-sub">' + escapeHtml(e.message || String(e)) + '</div></div>';
  }
}

function closeSignalDrawer(){
  const d = document.getElementById('signal-drawer');
  if (d) d.classList.remove('open');
}
window.openSignalDrawer = openSignalDrawer;
window.closeSignalDrawer = closeSignalDrawer;

function renderSignalsErrorState(msg){
  const list = document.getElementById('signals-list');
  list.innerHTML = `
    <div class="sig-error">
      <div class="sig-error-title">⚠️ Signals offline</div>
      <div class="sig-error-sub">${escapeHtml(msg)}</div>
      <div class="sig-error-hint">Retrying every 60s. Brief generation still works — it fetches signals on demand via SignalScout.</div>
    </div>`;
  document.getElementById('sig-count').textContent = 'offline';
}

// ============================================================
// BRIEF PANE (CENTER)
// ============================================================
const briefContent = document.getElementById('brief-content');
const briefStatus  = document.getElementById('brief-status');

function chipifyMarkdown(md){
  // [OBJ:FC-3] -> object chip
  md = md.replace(/\[OBJ:([^\]]+)\]/g, (_,id) =>
    `<span class="obj-chip" data-obj="${escapeAttr(id)}" onclick="onObjClick('${escapeAttr(id)}')">${escapeHtml(id)}</span>`);
  // [CITE:abc] -> citation chip
  md = md.replace(/\[CITE:([^\]]+)\]/g, (_,id) =>
    `<span class="cite-chip" data-cite="${escapeAttr(id)}" title="Provenance: ${escapeAttr(id)}">${escapeHtml(id)}</span>`);
  return md;
}

function clearBriefEmpty(){
  const e = briefContent.querySelector('.brief-empty');
  if (e) briefContent.innerHTML = '';
}

function renderBriefMsg(role, content){
  clearBriefEmpty();
  const el = document.createElement('div');
  el.className = 'brief-msg ' + role;
  if (role === 'assistant') {
    el.innerHTML = marked.parse(chipifyMarkdown(content));
    // Tag Critic-FAIL banner blockquotes so they render red, not yellow.
    el.querySelectorAll('blockquote').forEach(bq => {
      const t = bq.textContent || '';
      if (/Critic FAIL|Quality flag/i.test(t)) bq.classList.add('fail-banner');
    });
  } else if (role === 'error') {
    el.innerHTML = '';
    el.appendChild(document.createTextNode(content));
  } else {
    el.textContent = content;
  }
  briefContent.appendChild(el);
  briefContent.scrollTop = briefContent.scrollHeight;
  return el;
}

// Map agent name → user-facing "what they're doing right now" phrase.
// Shown inside the persistent thinking bubble; updates as the stream's
// active actor changes.
const ACTOR_THINKING_TEXT = {
  Supervisor:  'Planning the dispatch and routing to subagents…',
  DataAnalyst: 'Querying Genie for tonight’s at-risk shipments and revenue exposure…',
  SignalScout: 'Fanning out across 17 external signal sources (NWS, GDELT, PortWatch, FDA, etc.)…',
  Historian:   'Recalling similar past briefs and recurring disruption themes…',
  Composer:    'Synthesizing the final brief — severity, themes, actions…',
  Critic:      'Auditing the draft for groundedness, label precision, and posture calibration…',
  Publisher:   'Publishing the brief…',
};
const DEFAULT_THINKING_TEXT = 'Working on it…';

// Single persistent thinking bubble — lives in the center pane from the
// moment a query is submitted until the final brief renders (or an error).
// It updates its actor + text as the stream switches subagents. This is
// Option A: a conversation participant that "speaks" what it's doing, not
// a log dump. Replaces the prior disappearing rotating placeholder.
let _thinkingEl = null;
let _thinkingStart = 0;
let _thinkingActor = '';
let _thinkingLastChange = 0;  // ms timestamp of last actor change — drives "still working" hint
let _thinkingTimer = null;

// Fallback "stage" hints — shown when actor hasn't changed in N seconds so
// the user knows the agent is alive even if our actor-detection missed.
// Each stage adds a subtle subtext to the bubble.
function _thinkingFallbackHint(secondsSinceActorChange){
  if (secondsSinceActorChange < 12) return '';
  if (secondsSinceActorChange < 25) return 'still working…';
  if (secondsSinceActorChange < 45) return 'large query — hang tight…';
  if (secondsSinceActorChange < 75) return 'taking longer than usual — multi-step reasoning…';
  if (secondsSinceActorChange < 120) return 'final stretch — synthesizing…';
  return 'this is unusual — agent may be wedged. Check 🔍 → Logs.';
}

function renderPersistentThinking(){
  clearBriefEmpty();
  // Tear down any prior bubble + interval (defensive — should already be gone).
  removePersistentThinking();
  const el = document.createElement('div');
  el.className = 'brief-msg thinking-inline';
  el.id = 'brief-thinking';
  el.innerHTML = `
    <span class="thinking-actor" id="thinking-actor">Supervisor</span>
    <span class="thinking-text" id="thinking-text">${escapeHtml(DEFAULT_THINKING_TEXT)}</span>
    <span class="thinking-hint"  id="thinking-hint" style="font-size:11px;color:var(--text-secondary);font-style:italic"></span>
    <span class="thinking-elapsed" id="thinking-elapsed">0s</span>`;
  briefContent.appendChild(el);
  briefContent.scrollTop = briefContent.scrollHeight;
  _thinkingEl = el;
  _thinkingStart = Date.now();
  _thinkingLastChange = Date.now();
  _thinkingActor = 'Supervisor';
  // Tick the elapsed counter + hint every second.
  _thinkingTimer = setInterval(() => {
    if (!_thinkingEl || !document.body.contains(_thinkingEl)) {
      clearInterval(_thinkingTimer); _thinkingTimer = null; return;
    }
    const elapsed = Math.floor((Date.now() - _thinkingStart) / 1000);
    const m = Math.floor(elapsed / 60), s = elapsed % 60;
    const txt = m > 0 ? `${m}m ${String(s).padStart(2,'0')}s` : `${s}s`;
    const e = document.getElementById('thinking-elapsed');
    if (e) e.textContent = txt;
    // Fallback hint — based on how long since the actor last changed.
    const sinceChange = Math.floor((Date.now() - _thinkingLastChange) / 1000);
    const hintEl = document.getElementById('thinking-hint');
    if (hintEl) {
      const hint = _thinkingFallbackHint(sinceChange);
      hintEl.textContent = hint ? `· ${hint}` : '';
    }
  }, 1000);
  return el;
}

// Switch the persistent bubble to a new actor. Idempotent (no flicker if
// the same actor is already active). Updating the actor also resets the
// "stale" timer so the fallback hint restarts.
function updatePersistentThinking(actor){
  if (!_thinkingEl || !actor || actor === _thinkingActor) return;
  _thinkingActor = actor;
  _thinkingLastChange = Date.now();
  const actorEl = document.getElementById('thinking-actor');
  const textEl  = document.getElementById('thinking-text');
  const hintEl  = document.getElementById('thinking-hint');
  if (actorEl) actorEl.textContent = actor;
  if (textEl)  textEl.textContent  = ACTOR_THINKING_TEXT[actor] || `${actor} is working…`;
  if (hintEl)  hintEl.textContent  = '';  // clear stale hint immediately
}

function removePersistentThinking(){
  if (_thinkingTimer) { clearInterval(_thinkingTimer); _thinkingTimer = null; }
  if (_thinkingEl && _thinkingEl.parentNode) _thinkingEl.remove();
  _thinkingEl = null;
  _thinkingActor = '';
  _thinkingStart = 0;
}

// Backwards-compat alias — the rest of the code still calls renderBriefThinking().
function renderBriefThinking(){ return renderPersistentThinking(); }

async function onObjClick(id){
  // Open the investigation panel and fetch the object card from /objects/lookup.
  const panel = ensureInvestigationPanel();
  panel.style.display = 'flex';
  panel.querySelector('.inv-title').textContent = id;
  panel.querySelector('.inv-body').innerHTML = '<div class="inv-loading">Loading…</div>';
  try {
    const res = await fetch('/objects/lookup?id=' + encodeURIComponent(id));
    const data = await res.json();
    panel.querySelector('.inv-body').innerHTML = renderInvestigationCard(data);
  } catch(e){
    panel.querySelector('.inv-body').innerHTML = '<div class="inv-error">Lookup failed: ' + escapeHtml(e.message) + '</div>';
  }
}
window.onObjClick = onObjClick;

function ensureInvestigationPanel(){
  let p = document.getElementById('investigation-panel');
  if (p) return p;
  p = document.createElement('div');
  p.id = 'investigation-panel';
  p.style.cssText = 'position:fixed;top:64px;right:22px;bottom:22px;width:380px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:0;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.18);z-index:990;display:none;font-family:inherit;flex-direction:column';
  p.innerHTML = `
    <div class="inv-header" style="padding:14px 18px;border-bottom:1px solid var(--border);background:var(--nav-bg);color:#fff;display:flex;align-items:center;gap:10px">
      <span style="font-size:16px">◆</span>
      <div style="flex:1">
        <div style="font-size:10.5px;text-transform:uppercase;opacity:.7;letter-spacing:.5px">Investigation</div>
        <div class="inv-title" style="font-size:14px;font-weight:700">—</div>
      </div>
      <button onclick="document.getElementById('investigation-panel').style.display='none'" style="background:rgba(255,255,255,.1);color:#fff;border:0;border-radius:4px;padding:4px 10px;font-family:inherit;font-size:12px;cursor:pointer">×</button>
    </div>
    <div class="inv-body" style="flex:1;overflow:auto;padding:18px;font-size:13px;color:var(--text)"></div>`;
  document.body.appendChild(p);
  return p;
}

function renderInvestigationCard(data){
  if (data.error) return '<div class="inv-error" style="color:var(--danger);font-weight:600">' + escapeHtml(data.error) + '</div>';
  let html = '';
  if (data.label) {
    html += '<div style="font-size:15px;font-weight:800;color:var(--primary-dark);margin-bottom:10px">' + escapeHtml(data.label) + '</div>';
  }
  if (data.properties && Object.keys(data.properties).length) {
    html += '<table style="width:100%;font-size:12px;border-collapse:collapse;margin-bottom:14px"><tbody>';
    for (const [k, v] of Object.entries(data.properties)) {
      html += '<tr><td style="padding:5px 0;color:var(--text-secondary);width:40%;border-bottom:1px solid var(--border)">' + escapeHtml(k) + '</td>' +
              '<td style="padding:5px 0;font-weight:600;font-variant-numeric:tabular-nums;border-bottom:1px solid var(--border)">' + escapeHtml(String(v)) + '</td></tr>';
    }
    html += '</tbody></table>';
  }
  if (data.links) {
    for (const [linkLabel, items] of Object.entries(data.links)) {
      if (!items || !items.length) continue;
      html += '<div style="font-size:11px;text-transform:uppercase;color:var(--text-secondary);letter-spacing:.5px;margin:14px 0 6px;font-weight:700">' + escapeHtml(linkLabel) + '</div>';
      html += '<div style="display:flex;flex-wrap:wrap;gap:6px">';
      items.forEach(it => {
        if (typeof it === 'string') {
          html += '<span class="obj-chip" onclick="onObjClick(\'' + escapeAttr(it) + '\')">' + escapeHtml(it) + '</span>';
        } else if (it.id) {
          const extras = Object.entries(it).filter(([k]) => k !== 'id').map(([k, v]) => k + ':' + v).join(' · ');
          html += '<span class="obj-chip" onclick="onObjClick(\'' + escapeAttr(it.id) + '\')" title="' + escapeAttr(extras) + '">' + escapeHtml(it.id) + '</span>';
        }
      });
      html += '</div>';
    }
  }
  return html || '<div class="inv-empty" style="color:var(--text-secondary)">(no data)</div>';
}

// Parse severity, pending_actions, etc. out of the assistant text.
// Convention: a trailing fenced ```json block with {severity, pending_actions}
// OR a plain inline JSON block. Both are stripped from the displayed text.
function extractSidecar(text){
  const out = {severity:null, pending_actions:[], cleanText:text};
  // 1. fenced json
  const fence = text.match(/```json\s*([\s\S]*?)\s*```/);
  if (fence) {
    try {
      const j = JSON.parse(fence[1]);
      if (j.severity != null) out.severity = j.severity;
      if (Array.isArray(j.pending_actions)) out.pending_actions = j.pending_actions;
      out.cleanText = text.replace(fence[0], '').trim();
      return out;
    } catch(e){ /* fall through */ }
  }
  // 2. inline "Severity: 7.2"
  const sev = text.match(/severity[:\s]+(\d+(?:\.\d+)?)/i);
  if (sev) out.severity = parseFloat(sev[1]);
  return out;
}

// ============================================================
// ACTIONS RAIL (RIGHT)
// ============================================================
function renderActions(){
  const list = document.getElementById('actions-list');
  // The right-rail count is *unactioned* (pending) only — approved/rejected
  // items still render in the rail (for context) but stop counting.
  const unactionedCount = pendingActions.filter(a => !a._state).length;
  document.getElementById('actions-count').textContent = unactionedCount;
  // Keep the inline center-pane Action Queue in sync with the rail.
  renderInlineActionQueue();
  if (!pendingActions.length) {
    list.innerHTML = '<div class="action-empty">No pending approvals.<br>Actions surface here when the agent flags items that need human review.</div>';
    return;
  }
  list.innerHTML = '';
  pendingActions.forEach((a, idx) => {
    const card = document.createElement('div');
    card.className = 'action-card' + (a._state ? ' ' + a._state : '');
    const sev = (a.severity || 'low').toLowerCase();
    // Map both legacy (high/med/low) and new (critical/elevated/nominal) labels.
    const sevCls = (sev.startsWith('crit') || sev.startsWith('h')) ? 'high'
                 : (sev.startsWith('elev') || sev.startsWith('m')) ? 'med'
                 : 'low';
    card.innerHTML = `
      <div class="sev ${sevCls}"></div>
      <div class="type-pill">${escapeHtml(a.type || 'action')}</div>
      <div class="title">${escapeHtml(a.title || 'Untitled action')}</div>
      <div class="detail">${escapeHtml(a.detail || a.description || '')}</div>
      <div class="btns">
        <button class="approve" onclick="approveAction(${idx})">Approve</button>
        <button class="reject"  onclick="rejectAction(${idx})">Reject</button>
      </div>
      <div class="status-tag">${a._state === 'approved' ? '✓ APPROVED' : a._state === 'rejected' ? 'REJECTED' : ''}</div>
    `;
    list.appendChild(card);
  });
}

async function approveAction(idx){
  const a = pendingActions[idx];
  if (!a || a._state) return;
  a._state = 'approved';
  a._actionedAt = new Date();
  renderActions();  // re-renders BOTH right rail and inline center pane
  try {
    await fetch('/actions/approve', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action_id: a.id || ('idx-' + idx), approver: ''})
    });
  } catch(e){ /* swallow — UI already marked */ }
}
async function rejectAction(idx){
  const a = pendingActions[idx];
  if (!a || a._state) return;
  a._state = 'rejected';
  a._actionedAt = new Date();
  renderActions();  // re-renders BOTH right rail and inline center pane
  try {
    await fetch('/actions/reject', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action_id: a.id || ('idx-' + idx), approver: '', reason: ''})
    });
  } catch(e){ /* swallow — UI already marked */ }
}
window.approveAction = approveAction;
window.rejectAction  = rejectAction;

// Live mirror of the right-rail Action Queue, rendered inline INSIDE the
// brief pane. Composer's prose Action Queue table is replaced with a mount
// (#brief-actions-mount) after brief render; this function populates that
// mount and keeps it in sync as the operator approves/rejects.
function renderInlineActionQueue(){
  const mount = document.getElementById('brief-actions-mount');
  if (!mount) return;
  if (!pendingActions.length) {
    mount.innerHTML = '<div class="inline-actions"><div class="ia-empty">No actions proposed for this brief.</div></div>';
    return;
  }
  const pending  = pendingActions.filter(a => !a._state).length;
  const approved = pendingActions.filter(a => a._state === 'approved').length;
  const rejected = pendingActions.filter(a => a._state === 'rejected').length;
  let rows = '';
  pendingActions.forEach((a, i) => {
    const sev = (a.severity || 'low').toLowerCase();
    const sevCls = (sev.startsWith('crit') || sev.startsWith('h')) ? 'high'
                 : (sev.startsWith('elev') || sev.startsWith('m')) ? 'med'
                 : 'low';
    const state = a._state || 'pending';
    const stateLabel = state === 'approved' ? '✓ Approved'
                     : state === 'rejected' ? '✕ Rejected'
                     : 'Awaiting review';
    rows += `
      <tr class="s-${state}">
        <td style="font-weight:700;width:36px;color:var(--text-secondary);font-variant-numeric:tabular-nums">${i+1}</td>
        <td><div style="font-weight:700;color:var(--text);margin-bottom:2px">${escapeHtml(a.title || '(untitled)')}</div>
            <div style="color:var(--text-secondary);font-size:11px;line-height:1.45">${escapeHtml(a.detail || a.description || '')}</div></td>
        <td style="white-space:nowrap"><span class="sev-cell ${sevCls}">${escapeHtml((a.severity||'low'))}</span></td>
        <td style="white-space:nowrap"><span class="status-pill ${state}">${stateLabel}</span></td>
      </tr>`;
  });
  mount.innerHTML = `
    <div class="inline-actions">
      <div class="ia-summary">
        <span><strong>${pending}</strong> pending</span>
        <span><strong>${approved}</strong> approved</span>
        <span><strong>${rejected}</strong> rejected</span>
        <span style="margin-left:auto;font-style:italic">Approve / reject from the right rail →</span>
      </div>
      <table>
        <thead><tr>
          <th style="width:36px">#</th>
          <th>Action</th>
          <th>Severity</th>
          <th>Status</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}
window.renderInlineActionQueue = renderInlineActionQueue;

// After Composer's markdown is rendered, find any "Action Queue" / "Pending
// Actions" / "Proposed Actions" heading + the table that follows, and replace
// the whole block with a live mount (#brief-actions-mount). The mount is then
// rendered by renderInlineActionQueue() from pendingActions[].
function installInlineActionQueueMount(messageEl){
  if (!messageEl) return;
  // Demote any earlier brief's mount so the new brief becomes the live one.
  // Multiple briefs in a session would otherwise collide on the same ID.
  document.querySelectorAll('#brief-actions-mount').forEach(el => {
    el.removeAttribute('id');
    el.classList.add('actions-mount-archived');
  });
  const headings = messageEl.querySelectorAll('h2, h3');
  const re = /^\s*(pending\s+actions|action\s+queue|proposed\s+actions|recommended\s+actions|actions)\s*(\(.*\))?\s*$/i;
  let target = null;
  for (const h of headings) {
    if (re.test(h.textContent || '')) { target = h; break; }
  }
  if (!target) {
    // No heading matched — Composer may have skipped the section. Append the
    // mount at the end of the brief so the operator can still see the queue.
    const mount = document.createElement('h2');
    mount.textContent = 'Pending Actions';
    messageEl.appendChild(mount);
    const div = document.createElement('div');
    div.id = 'brief-actions-mount';
    messageEl.appendChild(div);
    return;
  }
  // Remove every sibling between `target` and the next heading (exclusive).
  const toRemove = [];
  let n = target.nextElementSibling;
  while (n && !/^H[1-3]$/.test(n.tagName)) {
    toRemove.push(n);
    n = n.nextElementSibling;
  }
  toRemove.forEach(el => el.remove());
  // Insert the mount right after the heading.
  const div = document.createElement('div');
  div.id = 'brief-actions-mount';
  target.after(div);
}

// ============================================================
// TIMELINE — now lives inside the Debug slide-out (🔍 in header).
// Render target = #timeline-list. pushTimeline() keeps writing rows
// even when the debug panel is closed, so the timeline is fully
// captured by the time the user opens it.
// ============================================================
const tlList = document.getElementById('timeline-list');
let tlEvents = [];

function classifyActor(actorName){
  const a = (actorName || '').toLowerCase();
  if (a.includes('supervisor')) return 'supervisor';
  if (a.includes('data'))       return 'data';
  if (a.includes('scout'))      return 'scout';
  if (a.includes('historian'))  return 'historian';
  if (a.includes('composer'))   return 'composer';
  if (a.includes('critic'))     return 'critic';
  return 'tool';
}

function pushTimeline(actor, msg, ts){
  if (!tlEvents.length) tlList.innerHTML = '';
  tlEvents.push({actor, msg, ts: ts || timestampNow()});
  const cls = classifyActor(actor);
  const row = document.createElement('div');
  row.className = 'tl-row';
  row.innerHTML = `
    <span class="tl-ts">${escapeHtml(ts || timestampNow())}</span>
    <span class="tl-actor ${cls}">${escapeHtml(actor)}</span>
    <span class="tl-msg">${escapeHtml(msg)}</span>
  `;
  tlList.appendChild(row);
  tlList.scrollTop = tlList.scrollHeight;
  document.getElementById('tl-count').textContent = `${tlEvents.length} event${tlEvents.length===1?'':'s'}`;
}

function timestampNow(){
  const d = new Date();
  return d.toLocaleTimeString([], {hour12:false}) + '.' + String(d.getMilliseconds()).padStart(3,'0').slice(0,2);
}

// (timeline-pane collapse toggle removed — timeline now lives in the debug slide-out)

// After a request, try /debug/logs to harvest subagent transitions
async function harvestTimelineFromLogs(){
  try {
    const res = await fetch('/debug/logs?limit=50', {cache:'no-store'});
    if (!res.ok) return;
    const j = await res.json();
    const lines = (j.lines || j.logs || (typeof j === 'string' ? j.split('\n') : []));
    const patterns = [
      /supervisor\s*[→\->]+\s*([A-Za-z]+)/i,
      /→\s*(DataAnalyst|SignalScout|Historian|Composer|Critic)/,
      /(DataAnalyst|SignalScout|Historian|Composer|Critic)\s+returned/i,
    ];
    for (const ln of lines.slice(-40)) {
      for (const p of patterns) {
        const m = String(ln).match(p);
        if (m) { pushTimeline('Supervisor', String(ln).slice(0, 140)); break; }
      }
    }
  } catch(e){ /* ignore */ }
}

// ============================================================
// CHAT INVOCATION
// ============================================================
const promptEl  = document.getElementById('prompt');
const sendBtn   = document.getElementById('send');

// chPush renders a chat-style bubble inline in the brief pane (the unified
// console surface). Replaces the old floating-chat history. Maps roles:
//   'user'              → right-aligned blue bubble
//   'assistant'         → left-aligned light bubble (short conversational answers)
//   'assistant thinking'→ animated "working…" bubble (removed when reply arrives)
//   'error'             → red bubble
function chPush(role, content, isHtml=false){
  clearBriefEmpty();
  const el = document.createElement('div');
  if (role === 'user') el.className = 'brief-msg user-bubble';
  else if (role === 'assistant thinking') el.className = 'brief-msg thinking-inline';
  else if (role === 'assistant') el.className = 'brief-msg assistant-chat';
  else if (role === 'error') el.className = 'brief-msg error';
  else el.className = 'brief-msg ' + role;
  if (isHtml) {
    el.innerHTML = content;
  } else if (role === 'assistant') {
    // Assistant chat answers come back as markdown — render via marked so
    // newlines, lists, bold render correctly. Collapse any ```sql / ```python
    // blocks into expandable <details> elements so a one-line data answer
    // isn't drowned in a 1KB SQL dump.
    el.innerHTML = renderChatMarkdown(content);
  } else {
    el.textContent = content;
  }
  briefContent.appendChild(el);
  briefContent.scrollTop = briefContent.scrollHeight;
  return el;
}

// Render an assistant chat reply: parse markdown, then convert each fenced
// code block (sql, python, json) into a <details> element so the visible
// bubble stays terse but the source is one click away.
function renderChatMarkdown(md){
  if (!md) return '';
  // 1. Strip the "[DataAnalyst → Supervisor]" actor prefix the subagent
  //    output prepends — it's useful for tracing but ugly in the chat bubble.
  let work = String(md).replace(/^\[[A-Za-z]+\s*→\s*[A-Za-z]+\]\s*\n?/, '');
  // 2. Pull out fenced code blocks BEFORE marked.js sees them so untagged
  //    ``` blocks don't render as monospace and known-language blocks become
  //    collapsible <details>. Placeholder is an HTML comment so marked passes
  //    it through unchanged (no bold/italic, no <p> wrap, no escaping).
  const KNOWN_CODE_LANGS = new Set(['sql','python','json','yaml','bash','shell','sh','js','javascript','ts','typescript']);
  const blocks = [];
  const placeholder = (i) => `<!--CHATCODEBLOCK${i}-->`;
  const fence = /```([A-Za-z0-9]*)\n?([\s\S]*?)```/g;
  const pulled = work.replace(fence, (_, lang, body) => {
    const langLower = (lang || '').toLowerCase();
    const isCode = langLower && KNOWN_CODE_LANGS.has(langLower);
    blocks.push({lang: langLower, body: (body || '').replace(/^\n+|\n+$/g, ''), isCode});
    return placeholder(blocks.length - 1);
  });
  let html = marked.parse(pulled);
  // 3. Re-insert each block. Known code lang → collapsible details.
  //    Untagged or "plain"/"text" → unwrap and render as normal prose
  //    (this is what fixes the "everything is monospace" bug — DataAnalyst
  //    wraps its summary in untagged ``` to mark it as "the literal output").
  blocks.forEach((b, i) => {
    let replacement;
    if (b.isCode) {
      const summary = b.lang === 'sql'    ? 'Show the SQL'
                    : b.lang === 'python' ? 'Show the Python'
                    : b.lang === 'json'   ? 'Show the JSON'
                    : `Show the ${b.lang}`;
      const lineCount = b.body.split('\n').length;
      replacement =
        `<details class="chat-code-details" style="margin:8px 0">` +
        `<summary style="cursor:pointer;font-size:11px;font-weight:700;color:var(--primary-dark);background:var(--primary-bg);padding:5px 10px;border-radius:var(--radius-sm);display:inline-block;letter-spacing:.2px;border:1px solid #c7dbff">▸ ${escapeHtml(summary)} (${lineCount} line${lineCount===1?'':'s'})</summary>` +
        `<pre style="background:var(--nav-bg);color:#e6f0ff;padding:10px 12px;border-radius:var(--radius-md);font-family:'SF Mono',Menlo,monospace;font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word;margin:6px 0 0;overflow-x:auto"><code>${escapeHtml(b.body)}</code></pre>` +
        `</details>`;
    } else {
      replacement = marked.parse(b.body);
    }
    html = html.split(placeholder(i)).join(replacement);
  });
  return html;
}

// --------------- streaming infra -----------------
function actorFromMessageText(t){
  // Each subagent's AIMessage starts with "[DataAnalyst → Supervisor]\n..."
  // Publisher's message has no such prefix (it's the canonical final brief).
  const m = (t || '').match(/^\[([A-Za-z]+)\s*→/);
  return m ? m[1] : null;
}
function nodeLabelFromActor(a){
  // Used only by the *live* bubble label while streaming — fall back to
  // "…streaming" so we don't prematurely brand un-prefixed messages as
  // "Publisher" (the actual canonical actor is resolved at end-of-stream
  // by resolveActorByIndex / pickFinalText).
  if (!a) return '…';
  return a;
}

// Resolve an actor label for a message given its position in the stream.
// Priority: explicit "[X →" prefix wins; else idx 0 = Supervisor's plan
// announcement; else the LAST message in the stream = Publisher's brief;
// else fall back to a generic "(subagent #N)" so un-prefixed intermediates
// (rate-limit errors, raw subagent output) don't masquerade as Publisher.
function resolveActorByIndex(msg, idx, total){
  const prefixed = actorFromMessageText(msg && msg.text);
  if (prefixed) return prefixed;
  if (idx === 0) return 'Supervisor';
  if (idx === total - 1) return 'Publisher';
  return '(subagent #' + idx + ')';
}

// Live subagent text now streams into the TIMELINE pane (bottom), not the
// brief pane. This matches operator intuition: the center pane shows the
// canonical deliverable, the timeline shows the agent's working. Each
// in-flight message gets one expandable timeline row with a live preview.
function appendLiveBubble(id){
  // Ensure we have a timeline row for this message id; if not, create one
  // tagged for streaming. Returns the inner text element to write deltas into.
  let row = document.querySelector('.tl-row[data-msg-id="' + CSS.escape(id) + '"]');
  if (!row) {
    if (!tlEvents.length) tlList.innerHTML = '';  // clear "empty" placeholder
    row = document.createElement('div');
    row.className = 'tl-row tl-live';
    row.setAttribute('data-msg-id', id);
    row.innerHTML = `
      <span class="tl-ts">${escapeHtml(timestampNow())}</span>
      <span class="tl-actor tool">…streaming</span>
      <span class="tl-msg tl-live-text"></span>`;
    tlList.appendChild(row);
    tlList.scrollTop = tlList.scrollHeight;
  }
  return row.querySelector('.tl-live-text');
}

function setLiveBubbleActor(id, actor){
  const row = document.querySelector('.tl-row[data-msg-id="' + CSS.escape(id) + '"]');
  if (!row) return;
  const a = row.querySelector('.tl-actor');
  if (a) {
    a.textContent = nodeLabelFromActor(actor);
    a.className = 'tl-actor ' + classifyActor(actor);
  }
}

function pickFinalText(allMessages){
  // Prefer the last message that is NOT a "[Subagent → Supervisor]" intermediate.
  for (let i = allMessages.length - 1; i >= 0; i--) {
    const t = allMessages[i].text || '';
    if (t.trim() && !actorFromMessageText(t)) return t;
  }
  // Fallback: any non-empty message
  for (let i = allMessages.length - 1; i >= 0; i--) {
    const t = (allMessages[i].text || '').trim();
    if (t) return allMessages[i].text;
  }
  return '';
}

// Detect whether a user query is asking for the full operational brief
// (vs an ad-hoc Q&A like "how many shipments in flight"). Brief queries go
// to the center pane; everything else stays in the chat panel only.
function isBriefIntent(text){
  const t = (text || '').toLowerCase();
  return /\bbrief\b|tonight'?s\s+(full\s+)?(ops|operational|brief)|generate\s+.*brief|nightly\s+(ops|report)/i.test(t);
}

async function ask(text){
  if (sending || !text.trim()) return;
  sending = true;
  sendBtn.disabled = true;
  promptEl.value = '';
  // Capture prompt + reset snapshot trail for this run so export reflects THIS brief.
  lastUserQuery = text;
  const briefMode = isBriefIntent(text);
  if (briefMode) {
    // Fresh brief — reset the snapshot trail and the brief pane.
    lastBriefMarkdown = '';
    lastBriefMessages = [];
    lastBriefTimestamp = new Date().toISOString();
  }

  // User message + thinking placeholder both render inline in the brief pane.
  // Brief-mode shows a rotating thinking message; chat-mode shows a short one.
  // Either way, prior conversation stays visible (no wiping the pane).
  chPush('user', text);
  // Single persistent thinking bubble for BOTH chat and brief modes — stays
  // alive in the center pane until the final reply renders. As the stream
  // switches subagents, the bubble's text updates ("Supervisor planning" →
  // "DataAnalyst querying" → ...) so the operator has a heartbeat + visibility
  // into which phase is running, without the log noise of inline streaming.
  renderPersistentThinking();
  if (briefMode) {
    briefStatus.textContent = 'Streaming…';
    setSeverityState('running');
  }
  pushTimeline('Supervisor', 'Received query: ' + text.slice(0, 80) + (text.length>80?'…':''));

  // Per-stream state
  const allMessages = [];               // [{id, text, actor}]
  const msgIndexById = {};              // id -> idx in allMessages
  const liveTextElById = {};            // id -> DOM node (live transcript)
  const toolNameById = {};              // function_call item_id -> name

  function ensureTranscriptStarted(){
    // No-op now — the persistent thinking bubble stays in the center pane
    // throughout the stream and gets removed when the final brief lands.
    // Live subagent text still streams into the Debug-panel timeline.
  }

  function startMessage(id){
    if (msgIndexById[id] != null) return;
    msgIndexById[id] = allMessages.length;
    allMessages.push({id, text: '', actor: null});
    ensureTranscriptStarted();
    liveTextElById[id] = appendLiveBubble(id);
    // Bump the "last activity" timestamp so the stale-progress hint doesn't
    // fire spuriously while real work is happening (each new message = progress).
    _thinkingLastChange = Date.now();
  }

  function appendDelta(id, delta){
    if (msgIndexById[id] == null) startMessage(id);
    const idx = msgIndexById[id];
    allMessages[idx].text += delta;
    const el = liveTextElById[id];
    if (el) {
      el.textContent = allMessages[idx].text;
      briefContent.scrollTop = briefContent.scrollHeight;
    }
    // Detect actor on first non-empty content. When detected, also flip the
    // persistent thinking bubble to reflect what's currently happening so
    // the operator sees a heartbeat in the center pane.
    if (!allMessages[idx].actor) {
      const a = actorFromMessageText(allMessages[idx].text);
      if (a) {
        allMessages[idx].actor = a;
        setLiveBubbleActor(id, a);
        updatePersistentThinking(a);
      }
    }
  }

  let _finalizedMessageCount = 0;
  function finalizeMessage(id, fullText){
    if (msgIndexById[id] == null) startMessage(id);
    const idx = msgIndexById[id];
    if (fullText != null && fullText.length > (allMessages[idx].text || '').length) {
      allMessages[idx].text = fullText;
      const el = liveTextElById[id];
      if (el) el.textContent = fullText;
    }
    // Only commit an actor if we have a real prefix; otherwise leave it
    // unresolved and let the end-of-stream pass (resolveActorByIndex) decide,
    // so intermediate un-prefixed messages are not auto-branded "Publisher".
    const prefixed = actorFromMessageText(allMessages[idx].text);
    if (prefixed) {
      allMessages[idx].actor = prefixed;
      setLiveBubbleActor(id, prefixed);
      updatePersistentThinking(prefixed);
    }
    // Pick a timeline label that mirrors export's resolveActorByIndex heuristic:
    //   1. Explicit "[X →" prefix wins.
    //   2. First finalized message → "Supervisor" (the plan announcement).
    //   3. Anything else un-prefixed → "(subagent)" so we never write the literal "message".
    // The final cross-position pass (export) still relabels the last entry as "Publisher".
    let label = allMessages[idx].actor;
    if (!label) {
      label = (_finalizedMessageCount === 0) ? 'Supervisor' : '(subagent)';
    }
    _finalizedMessageCount += 1;
    pushTimeline(label, '✓ message complete (' + (allMessages[idx].text || '').length + ' chars)');
  }

  function handleEvent(evt){
    const t = evt.type || '';
    if (t === 'response.created') {
      pushTimeline('Supervisor', 'Stream open. Dispatching plan.');
      return;
    }
    if (t === 'response.output_item.added' && evt.item) {
      if (evt.item.type === 'message') {
        startMessage(evt.item.id);
      } else if (evt.item.type === 'function_call') {
        const nm = evt.item.name || 'tool';
        toolNameById[evt.item.id] = nm;
        pushTimeline(nm, '→ calling…');
      }
      return;
    }
    if (t === 'response.function_call_arguments.delta') {
      // skip — we log on done
      return;
    }
    if (t === 'response.output_text.delta' && evt.item_id) {
      appendDelta(evt.item_id, evt.delta || '');
      return;
    }
    if (t === 'response.content_part.done') {
      // text content finalized — pull the full text from part
      if (evt.item_id && evt.part && (evt.part.text != null)) {
        finalizeMessage(evt.item_id, evt.part.text);
      }
      return;
    }
    if (t === 'response.output_item.done' && evt.item) {
      if (evt.item.type === 'function_call') {
        const nm = evt.item.name || toolNameById[evt.item.id] || 'tool';
        pushTimeline(nm, '→ ' + truncate(evt.item.arguments, 110));
      } else if (evt.item.type === 'function_call_output') {
        pushTimeline('↩ tool', truncate(evt.item.output, 120));
      } else if (evt.item.type === 'message') {
        const parts = evt.item.content || [];
        let full = '';
        for (const c of parts) if (c && (c.text || c.output_text)) full = c.text || c.output_text;
        finalizeMessage(evt.item.id, full);
      }
      return;
    }
    if (t === 'response.completed') {
      // intermediate completion (per react agent turn) — just note it
      return;
    }
    if (evt.error) {
      pushTimeline('Supervisor', 'STREAM ERROR: ' + evt.error);
      throw new Error(evt.error);
    }
  }

  try {
    pushTimeline('Supervisor', 'POST /invocations  →  stream=true');
    const res = await fetch('/invocations', {
      method:'POST',
      headers:{'Content-Type':'application/json','Accept':'text/event-stream'},
      body: JSON.stringify({input: [{role:'user', content: text}], stream: true})
    });
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + (await res.text()).slice(0, 400));
    if (!res.body) throw new Error('No response stream (browser/proxy buffering?)');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream:true});
      // SSE event delimiter: blank line
      let sep;
      while ((sep = buf.indexOf('\n\n')) !== -1) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        const lines = block.split('\n');
        let dataParts = [];
        for (const ln of lines) {
          if (ln.startsWith('data:')) dataParts.push(ln.slice(5).replace(/^ /,''));
        }
        if (!dataParts.length) continue;
        const data = dataParts.join('\n');
        if (data === '[DONE]') continue;
        let evt;
        try { evt = JSON.parse(data); } catch(e){ continue; }
        try { handleEvent(evt); } catch(e){ throw e; }
      }
    }

    ensureTranscriptStarted();

    // Pick the canonical final message and render it.
    const finalText = pickFinalText(allMessages);
    // Resolve actors with full stream context so first=Supervisor, last=Publisher,
    // prefixed-by-"[X →" wins, otherwise label "(subagent #N)" instead of faking Publisher.
    const resolvedMessages = allMessages.map((m, i) => ({
      id: m.id,
      actor: m.actor || resolveActorByIndex(m, i, allMessages.length),
      text: m.text || ''
    }));
    if (briefMode) lastBriefMessages = resolvedMessages;

    if (finalText) {
      const side = extractSidecar(finalText);
      // Final classification: did this actually produce a brief? Use intent AND
      // shape — a query with brief intent that yields no pending_actions is a
      // failed brief; a chat query that somehow yields pending_actions promotes.
      const hasBriefShape = (side.pending_actions && side.pending_actions.length > 0) ||
                            (side.severity != null);
      const renderAsBrief = briefMode || hasBriefShape;

      if (renderAsBrief) {
        lastBriefMarkdown = side.cleanText;
        // Final brief arrived — tear down the persistent thinking bubble,
        // then append the full brief. Prior conversation stays visible above.
        removePersistentThinking();
        const briefMsgEl = renderBriefMsg('assistant', side.cleanText);
        // Replace Composer's static Action Queue block with a live DOM mount;
        // pendingActions[] drives both this and the right rail, so approve/
        // reject from either updates both simultaneously.
        installInlineActionQueueMount(briefMsgEl);
        if (side.severity != null) setSeverity(side.severity);
        else setSeverity(null);
        if (side.pending_actions && side.pending_actions.length) {
          pendingActions = side.pending_actions;
        } else {
          pendingActions = [];
        }
        renderActions();  // first render of both rail + inline mount
        briefStatus.textContent = 'Updated ' + new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
      } else {
        // Chat-style answer: tear down the persistent thinking bubble and
        // append the assistant's short answer as a chat-bubble reply.
        removePersistentThinking();
        chPush('assistant', side.cleanText);
        // Reset header state — chat queries don't change posture.
        setSeverity(currentSeverity);  // re-render whatever was already set
      }
    } else {
      removePersistentThinking();
      if (briefMode) {
        renderBriefMsg('error', 'Stream ended with no usable final message.');
        briefStatus.textContent = 'Error';
        setSeverityState('error');
      } else {
        chPush('error', 'Empty stream from agent.');
      }
    }

    // Finalize all live timeline rows — fade out the streaming pulse, keep
    // them in the timeline as a record of what each subagent did.
    document.querySelectorAll('.tl-row.tl-live').forEach(row => {
      row.classList.remove('tl-live');
      row.style.background = '';
    });

  } catch (e) {
    removePersistentThinking();
    const ctx = {prompt:text, error:e.message, stack:e.stack||'', ts:new Date().toISOString()};
    const errHtml = `<div style="margin-bottom:6px">❌ ${escapeHtml(e.message)}</div>
      <details><summary style="cursor:pointer;font-weight:700">▸ Full failure context</summary>
      <pre style="background:var(--nav-bg);color:#fca5a5;padding:8px;border-radius:4px;font-size:10.5px;margin-top:5px;white-space:pre-wrap">${escapeHtml(JSON.stringify(ctx,null,2))}</pre>
      <button onclick="copyErr(this)" style="margin-top:5px;font-size:10.5px;padding:3px 8px;background:var(--primary);color:#fff;border:0;border-radius:4px;cursor:pointer;font-family:inherit">📋 Copy error</button>
      </details>`;
    if (briefMode) {
      setSeverityState('error');
      const errEl = renderBriefMsg('error', '');
      if (errEl) errEl.innerHTML = errHtml;
      briefStatus.textContent = 'Error';
    }
    // Surface in chat too so the user sees it where they asked.
    chPush('error', '❌ ' + e.message);
    pushTimeline('Supervisor', 'FAILED: ' + e.message);
  } finally {
    sending = false;
    sendBtn.disabled = false;
    promptEl.focus();
  }
}

function copyErr(btn){
  const pre = btn.previousElementSibling;
  navigator.clipboard.writeText(pre.textContent).then(()=>{ btn.textContent = '✓ Copied'; });
}
window.copyErr = copyErr;

// ============================================================
// EXPORT (snapshot → .md  +  print-view → PDF)
// ============================================================
async function buildSnapshotMarkdown(){
  const now = new Date();
  const ts  = now.toISOString();
  const posture = currentSeverity != null ? postureFromScore(currentSeverity).label : 'IDLE';
  const sevDetail = currentSeverity != null ? `${posture} (calibration score ${currentSeverity.toFixed(1)} / 10)` : 'IDLE';
  const briefMd = lastBriefMarkdown || '_(no brief generated yet — try a scenario from the Agent Console)_';
  const out = [];
  out.push('# Ops Brief Snapshot');
  out.push('');
  out.push('- **Captured:** ' + now.toLocaleString() + ' (' + ts + ')');
  out.push('- **Posture:** ' + sevDetail);
  out.push('- **App:** ' + window.location.host);
  if (lastUserQuery) {
    out.push('');
    out.push('## Original Prompt');
    out.push('');
    out.push('> ' + lastUserQuery.split('\n').join('\n> '));
  }
  out.push('');
  out.push('## Operational Brief');
  out.push('');
  out.push(briefMd);
  out.push('');
  out.push('## Pending Actions (' + pendingActions.length + ')');
  out.push('');
  if (!pendingActions.length) {
    out.push('_No pending actions._');
  } else {
    out.push('| # | Title | Type | Severity | State | Detail |');
    out.push('|---|-------|------|----------|-------|--------|');
    pendingActions.forEach((a, i) => {
      const state = a._state ? a._state.toUpperCase() : 'PENDING';
      const cells = [
        String(i+1),
        (a.title || '(untitled)').replace(/\|/g,'\\|'),
        a.type || '',
        a.severity || '',
        state,
        (a.detail || a.description || '').replace(/\|/g,'\\|').replace(/\n/g,' '),
      ];
      out.push('| ' + cells.join(' | ') + ' |');
    });
  }
  out.push('');
  out.push('## Signal Sources');
  out.push('');
  if (lastSignals) {
    const sigs = SOURCES.map(s => lastSignals[s.id]).filter(Boolean);
    const live = sigs.filter(s => s.status === 'live').length;
    const alerts = sigs.reduce((a, s) => a + (s.count || 0), 0);
    out.push('**' + live + '/' + SOURCES.length + ' live · ' + alerts + ' total alerts**');
    out.push('');
    out.push('| Source | Status | Count | Detail |');
    out.push('|--------|--------|------:|--------|');
    SOURCES.forEach(src => {
      const s = lastSignals[src.id] || {status:'stale', count:0, detail:'(no data)'};
      out.push('| ' + src.label + ' | ' + s.status + ' | ' + (s.count||0) + ' | ' +
        String(s.detail||'').replace(/\|/g,'\\|').replace(/\n/g,' ') + ' |');
    });
  } else {
    out.push('_Signals not yet loaded._');
  }
  out.push('');
  // Pull decisions log fresh
  let decisions = [];
  try {
    const r = await fetch('/decisions?limit=20', {cache:'no-store'});
    if (r.ok) { const j = await r.json(); decisions = j.decisions || []; }
  } catch(e) { /* ignore */ }
  out.push('## Decision Log (' + decisions.length + ')');
  out.push('');
  if (!decisions.length) out.push('_No decisions recorded._');
  else decisions.forEach(d => {
    // action_tools.log_decision writes {logged_at, headline, severity_score, themes, rationale}
    const t = d.logged_at || d.timestamp || d.ts || '';
    const s = d.headline || d.summary || d.decision || JSON.stringify(d).slice(0, 220);
    out.push('- **' + t + '** — ' + s);
  });
  out.push('');
  out.push('## Agent Timeline (' + tlEvents.length + ' events)');
  out.push('');
  out.push('```');
  tlEvents.slice(-120).forEach(e => {
    const actor = (e.actor || '').padEnd(22);
    out.push(e.ts + '  ' + actor + '  ' + e.msg);
  });
  out.push('```');
  if (lastBriefMessages.length) {
    out.push('');
    out.push('## Subagent Transcript');
    out.push('');
    // De-dup: if Publisher's body is the canonical brief (already rendered
    // under "## Operational Brief"), skip it here to avoid a multi-KB dupe.
    const briefHead = (lastBriefMarkdown || '').trim().slice(0, 200);
    lastBriefMessages.forEach((m, i) => {
      const actor = m.actor || resolveActorByIndex(m, i, lastBriefMessages.length);
      const text = m.text || '';
      const trimmed = text.trim();
      const isPublisher = actor === 'Publisher';
      const looksLikeBrief = briefHead && (
        trimmed === (lastBriefMarkdown || '').trim() ||
        (trimmed.length > 200 && trimmed.slice(0, 200) === briefHead)
      );
      if (isPublisher && looksLikeBrief) {
        out.push('### ' + actor);
        out.push('');
        out.push('_(canonical final brief — see "Operational Brief" section above)_');
        out.push('');
        return;
      }
      out.push('### ' + actor);
      out.push('');
      out.push(text);
      out.push('');
    });
  }
  return out.join('\n');
}

function downloadFile(filename, content, mime){
  const blob = new Blob([content], {type: mime || 'text/markdown;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 800);
}

async function exportSnapshotMd(){
  const btn = document.getElementById('export-md-btn');
  const orig = btn.textContent;
  btn.textContent = '⏳ …';
  try {
    const md = await buildSnapshotMarkdown();
    const stamp = new Date().toISOString().replace(/[:.]/g,'-').slice(0,19);
    downloadFile('ops_brief_' + stamp + '.md', md);
    btn.textContent = '✓ Downloaded';
    setTimeout(()=>{ btn.textContent = orig; }, 1800);
  } catch(e){
    alert('Export failed: ' + e.message);
    btn.textContent = orig;
  }
}

async function exportSnapshotPdf(){
  const btn = document.getElementById('export-pdf-btn');
  const orig = btn.textContent;
  btn.textContent = '⏳ Building…';
  try {
    // No need to pre-build markdown — downloadSnapshotPdf() captures the
    // live center-pane DOM directly (including chat + brief + action queue).
    await downloadSnapshotPdf();
    btn.textContent = '✓ Downloaded';
    setTimeout(()=>{ btn.textContent = orig; }, 2400);
  } catch(e){
    alert('PDF build failed: ' + e.message);
    btn.textContent = orig;
  }
}

async function downloadSnapshotPdf(){
  if (typeof html2pdf === 'undefined') {
    throw new Error('html2pdf library not loaded');
  }
  // The PDF mirrors what the operator is looking at: the entire center pane
  // (chat history + brief(s) + live Action Queue), plus a synthesized header
  // (posture + clock + signal snapshot) and footer (signal sources + decisions).
  // Previously we re-rendered from buildSnapshotMarkdown() which dropped chat
  // history, object chips (rendered as plain text), and the live action states.
  const now = new Date();
  const stamp = now.toISOString().replace(/[:.]/g,'-').slice(0,19);
  const posture = currentSeverity != null ? postureFromScore(currentSeverity).label : 'IDLE';
  const sevDetail = currentSeverity != null
    ? `${posture} (${(currentSeverity*10).toFixed(0)} / 100)`
    : 'IDLE';

  // Live signal snapshot
  let sigBlock = '<em>Signals not yet loaded.</em>';
  if (lastSignals) {
    const sigs = SOURCES.map(s => ({src:s, d:lastSignals[s.id]})).filter(x => x.d);
    const live = sigs.filter(x => x.d.status === 'live').length;
    const alerts = sigs.reduce((a, x) => a + (x.d.count || 0), 0);
    const rows = sigs.map(x => `<tr>
      <td>${escapeHtml(x.src.label)}</td>
      <td style="text-transform:uppercase;font-weight:700;font-size:9px">${escapeHtml(x.d.status||'—')}</td>
      <td style="text-align:right">${x.d.count ?? 0}</td>
      <td style="color:#6b7280">${escapeHtml(x.d.detail||'')}</td>
    </tr>`).join('');
    sigBlock = `
      <div style="margin-bottom:8px;font-size:11px;color:#374151"><strong>${live}/${SOURCES.length}</strong> live · <strong>${alerts}</strong> total alerts</div>
      <table>
        <thead><tr><th>Source</th><th>Status</th><th style="text-align:right">Count</th><th>Detail</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  // Decisions log (fetched live, async)
  let decisionsBlock = '<em>No decisions logged for this session.</em>';
  try {
    const r = await fetch('/decisions?limit=20', {cache:'no-store'});
    if (r.ok) {
      const j = await r.json();
      const decisions = j.decisions || [];
      if (decisions.length) {
        decisionsBlock = '<ul>' + decisions.map(d => {
          const t = d.logged_at || d.timestamp || d.ts || '';
          const s = d.headline || d.summary || d.decision || JSON.stringify(d).slice(0,180);
          return `<li><strong>${escapeHtml(t)}</strong> — ${escapeHtml(s)}</li>`;
        }).join('') + '</ul>';
      }
    }
  } catch(e) { /* ignore */ }

  // Clone the entire center pane (briefContent) so chat + brief(s) + live
  // action queue come along. Strip the transient thinking bubble and the
  // empty-state hero. (We're cloning so the user-facing pane isn't mutated.)
  const paneClone = briefContent.cloneNode(true);
  paneClone.querySelectorAll('#brief-thinking, .thinking-inline, .brief-empty').forEach(el => el.remove());

  // Render into an on-screen-but-invisible container sized to match A4's
  // printable area at 96 DPI. A4 = 210mm wide; with 10mm L/R margins the
  // printable width is 190mm ≈ 718px. We use 720 for a touch of buffer.
  // Previously this was 820px and html2pdf would scale-down + shift content
  // off the left edge of the page (~15% clipped). Locking container width =
  // windowWidth = 720 means the canvas maps 1:1 to the printable area.
  const PDF_WIDTH_PX = 720;
  const container = document.createElement('div');
  container.style.cssText = [
    'position:fixed','top:0','left:0',`width:${PDF_WIDTH_PX}px`,'background:#ffffff',
    'opacity:0','pointer-events:none','z-index:-1','overflow:hidden'
  ].join(';');
  container.innerHTML = `
    <style>
      /* pdf-root padding is ZERO — jsPDF's page margins ([10,10,12,10] mm)
         provide the printable inset. Any additional left/right padding here
         would push content beyond the printable area and clip on the left. */
      .pdf-root { font-family:"Nunito Sans",-apple-system,sans-serif; padding:0; color:#111827; line-height:1.55; font-size:12px; background:#fff; box-sizing:border-box; width:${PDF_WIDTH_PX}px; max-width:${PDF_WIDTH_PX}px; overflow:hidden; word-wrap:break-word; overflow-wrap:break-word; }
      .pdf-root * { box-sizing:border-box; max-width:100%; }
      .pdf-root h1 { font-size:20px; border-bottom:3px solid #0061ff; padding-bottom:8px; margin:0 0 12px; letter-spacing:-.3px; color:#0049c7 }
      .pdf-root h2 { font-size:13px; text-transform:uppercase; letter-spacing:.5px; color:#0049c7; border-left:3px solid #0061ff; padding-left:10px; margin:22px 0 8px; page-break-after:avoid }
      .pdf-root h3 { font-size:12px; color:#111827; margin:12px 0 6px; page-break-after:avoid }
      .pdf-root table { border-collapse:collapse; width:100%; font-size:10px; margin:6px 0 12px; table-layout:fixed; }
      .pdf-root th, .pdf-root td { padding:5px 8px; border-bottom:1px solid #e5e7eb; text-align:left; vertical-align:top; word-wrap:break-word; overflow-wrap:break-word; }
      .pdf-root th { background:#f9fafb; font-weight:700; text-transform:uppercase; letter-spacing:.3px; font-size:9px; color:#6b7280; }
      .pdf-root code { font-family:"SF Mono",Menlo,monospace; font-size:10px; background:#e6f0ff; color:#0049c7; padding:1px 4px; border-radius:3px; word-break:break-all; }
      .pdf-root pre { background:#0f1729; color:#e6f0ff; padding:10px 12px; border-radius:6px; font-family:"SF Mono",Menlo,monospace; font-size:9.5px; line-height:1.5; white-space:pre-wrap; word-break:break-word; }
      .pdf-root pre code { background:transparent; color:inherit; padding:0; }
      .pdf-root blockquote { border-left:4px solid #fcd34d; background:#fef3c7; padding:8px 12px; margin:8px 0; border-radius:0 6px 6px 0; color:#92400e; }
      .pdf-root ul, .pdf-root ol { padding-left:20px; }
      .pdf-root li { margin:3px 0; }

      /* Inline chips render as readable text in the PDF (background colors
         look noisy on paper, but we keep the obj-chip ◆ glyph so origins are clear). */
      .pdf-root .obj-chip { display:inline-block; background:#e6f0ff; color:#0049c7; padding:0 6px; border-radius:999px; font-size:10px; font-weight:700; border:1px solid #c7dbff; }
      .pdf-root .obj-chip::before { content:"◆ "; font-size:8px }
      .pdf-root .cite-chip { display:inline-block; background:#e0f2fe; color:#0369a1; padding:0 4px; border-radius:3px; font-size:8.5px; font-weight:800; vertical-align:super; }

      /* User question bubble, assistant chat bubble — keep their lightweight
         chat-bubble framing in the PDF so the back-and-forth is visible. */
      .pdf-root .user-bubble { background:#e6f0ff; border:1px solid #c7dbff; color:#0049c7; padding:8px 12px; border-radius:10px 10px 2px 10px; margin:6px 0 6px auto; max-width:75%; width:fit-content; font-weight:600; font-size:11px }
      .pdf-root .user-bubble::before { content:"USER: "; font-size:8px; opacity:.7; font-weight:800; letter-spacing:.5px }
      .pdf-root .assistant-chat { background:#fafbfc; border:1px solid #e5e7eb; padding:8px 12px; border-radius:10px 10px 10px 2px; margin:6px 0; max-width:90%; width:fit-content; font-size:11px }
      .pdf-root .assistant-chat::before { content:"AGENT: "; font-size:8px; opacity:.7; font-weight:800; letter-spacing:.5px; color:#6b7280 }

      /* Inline Action Queue: keep colored rows, but compress padding for PDF */
      .pdf-root .inline-actions { border:1px solid #e5e7eb; border-radius:6px; overflow:hidden; margin:8px 0 12px }
      .pdf-root .inline-actions .ia-summary { background:#f9fafb; padding:6px 10px; font-size:10px; color:#6b7280; border-bottom:1px solid #e5e7eb; display:flex; gap:12px }
      .pdf-root .inline-actions table { margin:0; font-size:10px }
      .pdf-root .inline-actions tr.s-approved { background:#dcfce7 }
      .pdf-root .inline-actions tr.s-rejected { background:#f9fafb; color:#6b7280; text-decoration:line-through }
      .pdf-root .inline-actions tr.s-rejected td:last-child { text-decoration:none }
      .pdf-root .status-pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:8.5px; font-weight:800; text-transform:uppercase }
      .pdf-root .status-pill.pending  { background:#e0f2fe; color:#0369a1 }
      .pdf-root .status-pill.approved { background:#dcfce7; color:#15803d }
      .pdf-root .status-pill.rejected { background:#fee2e2; color:#7f1d1d }

      /* Cover meta-card at the top */
      .pdf-cover { border:1px solid #e5e7eb; background:#f9fafb; border-radius:8px; padding:12px 16px; margin-bottom:18px; font-size:11px }
      .pdf-cover .row { display:flex; gap:10px; margin:2px 0 }
      .pdf-cover .key { color:#6b7280; font-weight:800; text-transform:uppercase; font-size:9px; letter-spacing:.4px; min-width:90px }
      .pdf-cover .val { color:#111827; font-weight:600 }
      .pdf-cover .posture-pill { padding:2px 10px; border-radius:999px; font-weight:800; font-size:10px; text-transform:uppercase; letter-spacing:.5px }
      .pdf-cover .posture-pill.NOMINAL  { background:#dcfce7; color:#15803d }
      .pdf-cover .posture-pill.ELEVATED { background:#fef3c7; color:#92400e }
      .pdf-cover .posture-pill.CRITICAL { background:#fee2e2; color:#991b1b }
      .pdf-cover .posture-pill.IDLE     { background:#e5e7eb; color:#374151 }

      .pdf-foot { margin-top:22px; padding-top:10px; border-top:1px solid #e5e7eb; font-size:9.5px; color:#6b7280; text-align:center }
    </style>
    <div class="pdf-root">
      <h1>🌙 The 11 PM Ops Brief</h1>
      <div class="pdf-cover">
        <div class="row"><span class="key">Captured</span><span class="val">${escapeHtml(now.toLocaleString())}</span></div>
        <div class="row"><span class="key">Posture</span><span class="val"><span class="posture-pill ${posture}">${escapeHtml(posture)}</span> &nbsp; ${escapeHtml(sevDetail)}</span></div>
        ${lastUserQuery ? `<div class="row"><span class="key">Prompt</span><span class="val">${escapeHtml(lastUserQuery)}</span></div>` : ''}
        <div class="row"><span class="key">Pending</span><span class="val">${pendingActions.filter(a=>!a._state).length} pending · ${pendingActions.filter(a=>a._state==='approved').length} approved · ${pendingActions.filter(a=>a._state==='rejected').length} rejected</span></div>
      </div>
      <h2>Operational Console</h2>
      <div id="pdf-pane-mount"></div>
      <h2>Signal Sources</h2>
      ${sigBlock}
      <h2>Decision Log</h2>
      ${decisionsBlock}
      <div class="pdf-foot">Generated by The 11 PM Ops Brief · ${escapeHtml(window.location.host)}</div>
    </div>
  `;
  document.body.appendChild(container);
  // Splice the cloned center-pane DOM into the mount.
  container.querySelector('#pdf-pane-mount').appendChild(paneClone);

  // Give the browser a frame to lay out the new DOM before html2canvas snapshots it.
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  try {
    const target = container.querySelector('.pdf-root');
    await html2pdf().set({
      margin:       [10, 10, 12, 10],
      filename:     `ops_brief_${stamp}.pdf`,
      image:        { type: 'jpeg', quality: 0.95 },
      // windowWidth must match the container width so html2canvas captures
      // at 1:1 to the A4 printable area. Mismatch = left-edge clipping.
      html2canvas:  { scale: 2, useCORS: true, backgroundColor: '#ffffff', logging: false, windowWidth: PDF_WIDTH_PX, width: PDF_WIDTH_PX, x: 0, y: 0 },
      jsPDF:        { unit: 'mm', format: 'a4', orientation: 'portrait' },
      pagebreak:    { mode: ['css', 'legacy'] }
    }).from(target).save();
  } finally {
    document.body.removeChild(container);
  }
}

document.getElementById('export-md-btn').onclick  = exportSnapshotMd;
document.getElementById('export-pdf-btn').onclick = exportSnapshotPdf;

// ============================================================
// COMPOSER WIRING (inline at the bottom of the brief pane)
// ============================================================
sendBtn.onclick = () => ask(promptEl.value);
promptEl.onkeydown = (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(promptEl.value); }
};
document.querySelectorAll('.brief-scenarios button').forEach(b => {
  b.onclick = () => ask(b.getAttribute('data-q'));
});

// Empty-state CTA — generate the full ops brief without opening chat first.
const TONIGHTS_BRIEF_QUERY = "Generate tonight's full ops brief. Pull internal data via Genie, fetch fresh external signals (NWS, GDELT, PortWatch, Aviation PIREPs), and compose the final markdown brief with severity, themes, and actions.";
function wireCta(){
  const cta = document.getElementById('generate-brief-cta');
  if (cta) cta.onclick = () => ask(TONIGHTS_BRIEF_QUERY);
}
wireCta();

// ============================================================
// DEBUG PANEL — Timeline · Health · Logs · Errors  (🔍 in header)
// Timeline is the default tab so opening the panel shows the agent
// trace immediately. Health/Logs/Errors share a single <pre>.
// ============================================================
const debugPanel  = document.getElementById('debug-panel');
const debugBtn    = document.getElementById('debug-btn');
const debugContent= document.getElementById('debug-content');
const debugMeta   = document.getElementById('dbg-pre-meta');
const dbgTabTimeline = document.getElementById('dbg-tab-timeline');
const dbgTabPre      = document.getElementById('dbg-tab-pre');

debugBtn.onclick = () => {
  const opened = !debugPanel.classList.contains('open');
  debugPanel.classList.toggle('open');
  if (opened) {
    // Default to Timeline so the agent trace is the first thing the operator sees.
    showDebugTab('timeline');
  }
};

function setActiveDbgBtn(tab){
  debugPanel.querySelectorAll('.dbg-btns button[data-tab]').forEach(b => {
    if (b.getAttribute('data-tab') === tab) b.classList.add('active');
    else b.classList.remove('active');
  });
}

function showDebugTab(tab){
  setActiveDbgBtn(tab);
  if (tab === 'timeline') {
    dbgTabTimeline.style.display = '';
    dbgTabPre.style.display = 'none';
    return;
  }
  dbgTabTimeline.style.display = 'none';
  dbgTabPre.style.display = '';
  if (tab === 'health')  return loadHealth();
  if (tab === 'logs')    return loadLogs();
  if (tab === 'errors')  return loadLogs('ERROR');
}
window.showDebugTab = showDebugTab;

async function loadHealth(){
  debugMeta.innerHTML = 'GET <code>/health</code> · platform diagnostics';
  debugContent.textContent = 'loading …';
  try {
    const r = await fetch('/health');
    debugContent.textContent = `HTTP ${r.status}\n\n` + JSON.stringify(await r.json(), null, 2);
  } catch(e){ debugContent.textContent = '❌ ' + e.message; }
}
async function loadLogs(level){
  debugMeta.innerHTML = level
    ? `GET <code>/debug/logs?level=${level}</code> · last 80 errors`
    : 'GET <code>/debug/logs</code> · last 80 events';
  debugContent.textContent = 'loading …';
  try {
    const url = '/debug/logs?limit=80' + (level ? '&level=' + level : '');
    const r = await fetch(url);
    const j = await r.json().catch(() => null);
    debugContent.textContent = j ? (Array.isArray(j.lines)?j.lines.join('\n'):JSON.stringify(j,null,2)) : await r.text();
  } catch(e){ debugContent.textContent = '❌ ' + e.message; }
}
function copyDebug(){
  // Copy whichever tab is active.
  const text = (dbgTabTimeline.style.display !== 'none')
    ? tlEvents.map(e => `${e.ts}  ${e.actor}  ${e.msg}`).join('\n')
    : (debugContent.textContent || '');
  navigator.clipboard.writeText(text).then(() => {
    const btns = debugPanel.querySelectorAll('.dbg-btn-b');
    btns.forEach(b => { const old = b.textContent; b.textContent = '✓ Copied'; setTimeout(()=>b.textContent=old, 1200); });
  });
}
window.loadHealth = loadHealth;
window.loadLogs   = loadLogs;
window.copyDebug  = copyDebug;

// ============================================================
// UTIL
// ============================================================
function escapeHtml(s){ return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escapeAttr(s){ return escapeHtml(s).replace(/`/g, '&#96;'); }
function truncate(s, n){ s = String(s ?? ''); return s.length > n ? s.slice(0, n) + '…' : s; }

// ============================================================
// BOOT
// ============================================================
// Don't pre-render with DEMO_SIGNALS — the placeholder HTML already shows
// "Polling 17 sources…" which is honest about the loading state. The first
// refreshSignals() will replace it with real data (or the error state).
setSeverity(null);
refreshSignals();
setInterval(refreshSignals, 60000);
// Focus the composer on load so the operator can just start typing.
if (promptEl) promptEl.focus();
</script>
</body>
</html>"""


def install_chat_ui(app: FastAPI) -> None:
    """Mount the embedded chat UI on GET / of the agent app."""
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def _index():
        return HTMLResponse(content=INDEX_HTML)
