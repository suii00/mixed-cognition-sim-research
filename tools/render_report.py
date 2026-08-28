"""Render a single-file interactive HTML report for a run.

Usage: python tools/render_report.py <run_dir> [--out DIR]
The HTML embeds the run data (positions, actions, messages, reasoning) and needs no network.
"""
import argparse
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.viz_common import load_run, per_step_bloc_counts, timestamped_out_dir  # noqa: E402

TEMPLATE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#222;--mut:#666;--line:#ddd;--panel:#f7f7f8}
@media(prefers-color-scheme:dark){:root{--bg:#16171a;--fg:#e6e6e6;--mut:#9a9a9a;--line:#333;--panel:#1f2024}}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI","Noto Sans JP",sans-serif;background:var(--bg);color:var(--fg);font-size:14px}
header{padding:12px 20px;border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:18px}header .m{color:var(--mut);font-size:12px;margin-top:4px;font-family:ui-monospace,monospace}
.ctl{display:flex;gap:12px;align-items:center;padding:10px 20px;border-bottom:1px solid var(--line)}
.ctl input[type=range]{flex:1}
.ctl button{padding:4px 12px}
main{display:grid;grid-template-columns:minmax(420px,1fr) minmax(360px,1fr);gap:16px;padding:16px 20px}
svg{width:100%;height:auto;background:var(--panel);border:1px solid var(--line);border-radius:6px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;max-height:80vh;overflow:auto}
.panel h2{font-size:14px;margin:0 0 8px}
.card{border-left:4px solid var(--c,#999);padding:6px 10px;margin-bottom:8px;background:var(--bg);border-radius:4px}
.card .h{font-weight:600;font-size:13px}.card .k{color:var(--mut);font-size:11px;margin-top:4px}
.card .v{white-space:pre-wrap;margin:2px 0 4px}
.card.sel{outline:2px solid var(--c)}
table{border-collapse:collapse;width:100%;margin-top:8px}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:right;font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
.legend span{display:inline-block;margin-right:14px}.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}
.bars{display:block;width:100%;height:90px;background:var(--panel);border:1px solid var(--line);border-radius:6px;margin-top:8px}
.note{color:var(--mut);font-size:12px;margin-top:6px}
</style></head><body>
<header><h1>__TITLE__</h1><div class="m">__METALINE__</div></header>
<div class="ctl">
  <button id="play">▶ 再生</button>
  <input type="range" id="step" min="0" max="__MAXIDX__" value="0">
  <span id="lbl" style="font-family:ui-monospace,monospace;min-width:110px"></span>
  <span class="legend" id="legend"></span>
</div>
<main>
  <div>
    <svg id="map" viewBox="0 0 600 600"></svg>
    <svg class="bars" id="sent" viewBox="0 0 600 90" preserveAspectRatio="none"></svg>
    <div class="note">上のバー：各ステップで messages.jsonl に送信記録のあるエージェント数（bloc 別、積み上げ）。黒線＝現在ステップ。クリックでそのステップへ。</div>
    <div class="note">地図：● = action=move、○(白抜き) = action=stay、外側リング = このステップでメッセージ送信。薄い線は直近 6 ステップの軌跡。点をクリックで右に詳細。</div>
    <div class="panel" style="margin-top:12px"><h2>bloc 別サマリ（全ステップ）</h2><table id="sum"></table></div>
  </div>
  <div class="panel" id="detail"><h2 id="dh">ステップ内のエージェント</h2><div id="cards"></div></div>
</main>
<script>
const D = __DATA__;
const TRAIL = 6, S = 600, H = D.half, PAD = 20;
const sx = x => PAD + (x + H) / (2 * H) * (S - 2 * PAD), sy = y => S - PAD - (y + H) / (2 * H) * (S - 2 * PAD);
const map = document.getElementById('map'), sentSvg = document.getElementById('sent');
const slider = document.getElementById('step'), lbl = document.getElementById('lbl'), cards = document.getElementById('cards');
let idx = 0, sel = null, timer = null;
document.getElementById('legend').innerHTML = D.blocs.map(b => `<span><i style="background:${D.colors[b]}"></i>${b} (${D.models[b]})</span>`).join('');

function esc(s){return (s ?? '').toString().replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

function drawMap(){
  const st = D.steps[idx]; let g = '';
  for (let v = -H; v <= H; v += 5) { g += `<line x1="${sx(v)}" y1="${PAD}" x2="${sx(v)}" y2="${S-PAD}" stroke="#8884" /><line x1="${PAD}" y1="${sy(v)}" x2="${S-PAD}" y2="${sy(v)}" stroke="#8884" />`; }
  for (const p of D.places) { g += `<rect x="${sx(p.cx-p.hs)}" y="${sy(p.cy+p.hs)}" width="${sx(p.cx+p.hs)-sx(p.cx-p.hs)}" height="${sy(p.cy-p.hs)-sy(p.cy+p.hs)}" fill="#8883" stroke="#888"/><text x="${sx(p.cx)}" y="${sy(p.cy+p.hs)-4}" font-size="10" text-anchor="middle" fill="#888">${esc(p.name)}</text>`; }
  for (const aid of D.agents) {
    const c = D.colors[D.bloc[aid]]; const pts = [];
    for (let i = Math.max(0, idx - TRAIL); i <= idx; i++) { const r = D.steps[i].a[aid]; if (r) pts.push(`${sx(r.p[0])},${sy(r.p[1])}`); }
    if (pts.length > 1) g += `<polyline points="${pts.join(' ')}" fill="none" stroke="${c}" stroke-opacity=".35" stroke-width="2"/>`;
  }
  for (const aid of D.agents) {
    const r = st.a[aid]; if (!r) continue; const c = D.colors[D.bloc[aid]];
    const x = sx(r.p[0]), y = sy(r.p[1]), stay = r.ac === 'stay';
    if (st.m[aid]) g += `<circle cx="${x}" cy="${y}" r="14" fill="none" stroke="${c}" stroke-width="2.5"/>`;
    g += `<circle class="ag" data-aid="${aid}" cx="${x}" cy="${y}" r="${stay?6:8}" fill="${stay?'#fff':c}" stroke="${sel===aid?'#000':c}" stroke-width="${sel===aid?3:1.5}" style="cursor:pointer"/><text x="${x+9}" y="${y-8}" font-size="10" fill="currentColor">${aid}</text>`;
  }
  map.innerHTML = g;
  map.querySelectorAll('.ag').forEach(el => el.onclick = () => { sel = +el.dataset.aid; render(); document.getElementById('c'+sel)?.scrollIntoView({block:'nearest'}); });
}

function drawBars(){
  const n = D.steps.length, w = 600 / n, N = D.agents.length; let g = '';
  D.steps.forEach((st, i) => { let y = 88; for (const b of D.blocs) { const v = st.c[b].sent; const h = v / N * 86; y -= h; g += `<rect x="${i*w}" y="${y}" width="${w*0.9}" height="${h}" fill="${D.colors[b]}"/>`; } g += `<rect x="${i*w}" y="0" width="${w}" height="90" fill="transparent" data-i="${i}" style="cursor:pointer"/>`; });
  g += `<line x1="${idx*w + w/2}" y1="0" x2="${idx*w + w/2}" y2="90" stroke="currentColor" stroke-width="1.5"/>`;
  sentSvg.innerHTML = g;
  sentSvg.querySelectorAll('[data-i]').forEach(el => el.onclick = () => { idx = +el.dataset.i; slider.value = idx; render(); });
}

function drawCards(){
  const st = D.steps[idx];
  document.getElementById('dh').textContent = `step ${st.s} のエージェント（${Object.keys(st.a).length} 件）`;
  cards.innerHTML = D.agents.map(aid => { const r = st.a[aid]; if (!r) return ''; const m = st.m[aid]; const c = D.colors[D.bloc[aid]];
    return `<div class="card ${sel===aid?'sel':''}" id="c${aid}" style="--c:${c}"><div class="h">#${aid} ${D.bloc[aid]} · ${D.model[aid]} · pos (${r.p[0]}, ${r.p[1]}) · ${esc(r.ac)}${r.d?' '+esc(r.d):''}</div>
<div class="k">message${m?` → ${m.rc} 名`:''}</div><div class="v">${m?esc(m.msg):'<span style="color:var(--mut)">（このステップの送信記録なし）</span>'}</div>
${m?`<div class="k">message reasoning</div><div class="v">${esc(m.rs)}</div>`:''}
<div class="k">memory</div><div class="v">${esc(r.mem)}</div><div class="k">action reasoning</div><div class="v">${esc(r.rs)}</div></div>`; }).join('');
  cards.querySelectorAll('.card').forEach(el => el.onclick = () => { sel = +el.id.slice(1); render(); });
}

function drawSummary(){
  const rows = D.blocs.map(b => { let ag=0, se=0, mv=0, stn=0; for (const st of D.steps) { ag+=st.c[b].agents; se+=st.c[b].sent; mv+=st.c[b].move; stn+=st.c[b].stay; }
    return `<tr><td><i style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${D.colors[b]};margin-right:4px"></i>${b}</td><td>${D.models[b]}</td><td>${ag}</td><td>${se} (${(100*se/ag).toFixed(1)}%)</td><td>${mv}</td><td>${stn}</td></tr>`; });
  document.getElementById('sum').innerHTML = `<tr><th>bloc</th><th>model</th><th>agent-steps</th><th>送信あり</th><th>move</th><th>stay</th></tr>${rows.join('')}`;
}

function render(){ lbl.textContent = `step ${D.steps[idx].s} / ${D.steps[D.steps.length-1].s}`; drawMap(); drawBars(); drawCards(); }
slider.oninput = () => { idx = +slider.value; render(); };
document.getElementById('play').onclick = function(){
  if (timer) { clearInterval(timer); timer = null; this.textContent = '▶ 再生'; return; }
  this.textContent = '⏸ 停止';
  timer = setInterval(() => { idx = (idx + 1) % D.steps.length; slider.value = idx; render(); }, 600);
};
document.addEventListener('keydown', e => { if (e.key==='ArrowRight' && idx < D.steps.length-1) { idx++; slider.value = idx; render(); } if (e.key==='ArrowLeft' && idx > 0) { idx--; slider.value = idx; render(); } });
drawSummary(); render();
</script></body></html>
"""


def build_data(run):
    counts = per_step_bloc_counts(run)
    steps = []
    for s in run["step_ids"]:
        a = {}
        for aid, r in run["steps"][s].items():
            a[aid] = {"p": r["position"], "ac": r.get("action"), "d": r.get("direction"),
                      "mem": r.get("memory"), "rs": r.get("reasoning")}
        m = {}
        for aid, rec in run["msgs_by_step"].get(s, {}).items():
            m[aid] = {"msg": rec["message"], "rs": rec.get("reasoning"),
                      "rc": len(rec.get("receiver_ids", []))}
        steps.append({"s": s, "a": a, "m": m, "c": counts[s]})
    return {
        "half": run["half"],
        "places": [{"cx": p["center_x"], "cy": p["center_y"], "hs": p["half_size"],
                    "name": p["name"]} for p in run["places"]],
        "blocs": run["bloc_names"], "models": run["bloc_models"], "colors": run["colors"],
        "agents": run["agent_ids"], "bloc": run["agent_bloc"], "model": run["agent_model"],
        "steps": steps,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument(
        "--out",
        default=None,
        help=(
            "output dir (default: <run-parent>/derived/<run-name>/"
            "viz-v1.0.0-<timestamp>)"
        ),
    )
    args = ap.parse_args()

    run = load_run(args.run_dir)
    if not run["step_ids"]:
        sys.exit("no steps in memory_reasoning.jsonl")
    meta = run["meta"]
    sim = meta["config"]["simulation"]
    metaline = (f"status={meta.get('status')} · steps={meta.get('completed_steps')}/"
                f"{meta.get('expected_steps')} · agents={meta.get('observed_agents')} · "
                f"llm_calls={meta.get('logical_llm_calls')} · "
                f"transport_failures={meta.get('transport_failures')} · "
                f"syntax_parse_failures={meta.get('syntax_parse_failures')} · "
                f"schema_validation_failures={meta.get('schema_validation_failures')} · "
                f"parse_errors.jsonl={len(run['parse_errors'])} 行 · "
                f"seed={sim.get('seed')} · git_sha={str(meta.get('git_sha'))[:12]} · "
                f"{meta.get('start_time_utc')} → {meta.get('end_time_utc')}")
    data = json.dumps(build_data(run), ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    page = (TEMPLATE.replace("__TITLE__", html.escape(run["run_id"]))
            .replace("__METALINE__", html.escape(metaline))
            .replace("__MAXIDX__", str(len(run["step_ids"]) - 1))
            .replace("__DATA__", data))
    out_dir = args.out or timestamped_out_dir(args.run_dir, "viz")
    os.makedirs(out_dir, exist_ok=False)
    out = os.path.join(out_dir, f"{run['run_id']}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
