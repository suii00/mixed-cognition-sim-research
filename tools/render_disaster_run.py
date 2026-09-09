#!/usr/bin/env python3
"""Read-only, phase-aware disaster replay; publish new derived artifacts only."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.disaster import Rectangle, contains_warning_identifier  # noqa: E402
from engine.provenance import compute_config_hash  # noqa: E402
from tools.artifact_boundaries import (  # noqa: E402
    find_immutable_artifact_ancestor, is_allowed_derived_output_root, is_within,
)
from tools.run_disaster_behavior_pilot import public_tree_safe  # noqa: E402
from tools.scan_publication import scan_text  # noqa: E402
from tools.validate_run import validate_run  # noqa: E402

VERSION = "disaster-run-replay-v1.0.0"
FILES = ("positions.jsonl", "world_events.jsonl", "warning_events.jsonl",
         "phase1_raw.jsonl", "messages.jsonl", "memory_reasoning.jsonl")
COLORS = ("#2469a0", "#ae493d", "#7a4dab", "#94700d", "#087b7c", "#c4568a")
SEMANTICS = {
    "viewpoint": "Observer view; global hazard geometry is not an agent observation.",
    "agent_information": "Agents receive refuge coordinates and current-cell hazard status; viewer model labels are not prompts.",
    "communication": "After Phase 1 delivery, before Phase 3 movement; positions are step minus one.",
    "end": "End of completed step; positions are post_movement. No delivery arrows at moved positions.",
    "reuse": "Exact warning identifier in phase1.parsed.message at a numeric step later than the agent's first exposure; not understanding or adoption.",
    "distance": "Minimum Manhattan distance to an inclusive refuge rectangle, in one-cell movement opportunities.",
    "remaining": "Planned duration minus snapshot position step. Communication includes the current step's move; end excludes it.",
    "margin": "Remaining minus distance. Negative means even a shortest path cannot arrive by the declared horizon; nonnegative is not a prediction.",
    "arrival": "Ever observed inside a refuge at an initial or completed post-movement position; not a claim of warning-caused evacuation.",
    "action": "A move command can have zero displacement at a world boundary.",
    "partial": "Only fully completed steps are replayed; aborted or partial terminal status remains visible.",
}


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if path.exists():
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return False


def reject_link_ancestry(path: Path) -> None:
    if any(is_link(p) for p in (path, *path.parents)):
        raise ValueError("symbolic links or reparse points are not accepted")


def require_output_path(output_dir: Path, run_dir: Path) -> Path:
    reject_link_ancestry(output_dir)
    output = output_dir.resolve(strict=False)
    if output_dir.exists():
        raise FileExistsError("derived output must be new")
    if not re.fullmatch(re.escape(VERSION) + r"_\d{8}T\d{6}Z(?:_[A-Za-z0-9_-]+)?", output.name):
        raise ValueError("output leaf must contain the tool version and UTC timestamp")
    if (is_within(output, run_dir.resolve()) or "derived" not in output.parts
            or not is_allowed_derived_output_root(output, repo_root=REPO_ROOT)
            or find_immutable_artifact_ancestor(output) is not None):
        raise ValueError("output must be a fresh derived directory outside immutable artifacts")
    return output


def snapshot_tree(run_dir: Path) -> dict:
    reject_link_ancestry(run_dir)
    if not run_dir.is_dir():
        raise ValueError("run input must be a directory")
    result = {}
    for path in sorted(run_dir.rglob("*")):
        if is_link(path) or not (path.is_file() or path.is_dir()):
            raise ValueError("run input contains a nonregular entry")
        if path.is_file():
            payload = path.read_bytes()
            result[path.relative_to(run_dir).as_posix()] = {"sha256": digest(payload), "bytes": len(payload)}
    return result


def read_records(run_dir: Path) -> dict:
    records = {}
    for name in FILES:
        rows = []
        for line_number, line in enumerate((run_dir / name).read_bytes().splitlines(keepends=True), 1):
            if not line.strip():
                continue
            rows.append({"row": json.loads(line), "reference": {
                "file": name, "line": line_number, "sha256": digest(line),
            }})
        records[name] = rows
    return records


def build_replay(meta: dict, records: dict) -> dict:
    """Pure frame derivation. File callers must first perform strict validation."""
    config = meta["config"]
    if config["scenario"]["type"] != "disaster_v1":
        raise ValueError("only disaster_v1 is supported")
    duration = config["simulation"]["duration"]
    completed = meta["completed_steps"]
    if not 0 <= completed <= duration:
        raise ValueError("invalid completed horizon")
    positions = {(r["row"]["step"], r["row"]["agent_id"]): r for r in records["positions.jsonl"]}
    if len(positions) != len(records["positions.jsonl"]):
        raise ValueError("duplicate position key")
    ids = sorted(agent for step, agent in positions if step == 0)
    if not ids or any((s, a) not in positions for s in range(completed + 1) for a in ids):
        raise ValueError("incomplete recorded positions")
    agents = [{"id": a, "model": positions[(0, a)]["row"]["model"]} for a in ids]
    models = sorted({a["model"] for a in agents})
    if len(models) > len(COLORS):
        raise ValueError("viewer supports up to six distinct model labels")
    model_colors = dict(zip(models, COLORS))
    refuges = config["scenario"]["refuges"]
    rectangles = [Rectangle(**r["rectangle"]) for r in refuges]
    hazards = {r["row"]["step"]: r for r in records["world_events.jsonl"]
               if r["row"]["event_type"] == "hazard_state"}
    if any(s not in hazards for s in range(1, completed + 1)):
        raise ValueError("missing recorded hazard state")
    warning_id = config["scenario"]["official_warning"]["warning_id"]
    issued = [r for r in records["warning_events.jsonl"] if r["row"]["event_type"] == "warning_issued"]
    exposures = [r for r in records["warning_events.jsonl"] if r["row"]["event_type"] == "warning_exposure"]
    first_exposure = {}
    for record in exposures:
        row = record["row"]
        if row["warning_id"] == warning_id:
            aid = row["recipient_id"]
            first_exposure[aid] = min(first_exposure.get(aid, row["step"]), row["step"])
    phase1 = {(r["row"]["step"], r["row"]["agent_id"]): r for r in records["phase1_raw.jsonl"]
              if r["row"]["step"] <= completed}
    actions = {(r["row"]["step"], r["row"]["agent_id"]): r for r in records["memory_reasoning.jsonl"]}
    reuses = {a: [] for a in ids}
    for (step, aid), record in sorted(phase1.items()):
        text = record["row"].get("parsed", {}).get("message")
        if aid in first_exposure and step > first_exposure[aid] and contains_warning_identifier(text, warning_id):
            reuses[aid].append(step)
    frames = []
    for step in range(completed + 1):
        for phase in (("initial",) if step == 0 else ("communication", "end")):
            position_step = step - 1 if phase == "communication" else step
            remaining = duration - position_step
            deliveries = []
            for record in records["messages.jsonl"]:
                row = record["row"]
                if row["step"] != step:
                    continue
                receivers = row["receiver_ids"]
                if row["sender_id"] not in ids or any(a not in ids for a in receivers):
                    raise ValueError("delivery references an unknown agent")
                if receivers:
                    deliveries.append({"sender": row["sender_id"], "receivers": receivers,
                        "text": row["message"], "warning_carrier": contains_warning_identifier(row["message"], warning_id),
                        "reference": record["reference"]})
            frame_agents = []
            for agent in agents:
                aid = agent["id"]
                position = positions[(position_step, aid)]
                point = position["row"]["position"]
                distance = min(rect.manhattan_distance(*point) for rect in rectangles)
                action = actions.get((step, aid)) if phase == "end" else None
                output = phase1.get((step, aid)) if step else None
                displacement = None
                if action:
                    before = positions[(step - 1, aid)]["row"]["position"]
                    displacement = [point[0] - before[0], point[1] - before[1]]
                seen_exposure = first_exposure.get(aid)
                frame_agents.append({**agent, "position": point, "position_reference": position["reference"],
                    "distance": distance, "remaining": remaining, "margin": remaining - distance,
                    "in_refuge": distance == 0,
                    "ever_arrived": any(positions[(s, aid)]["row"]["refuge_id"] is not None for s in range(position_step + 1)),
                    "exposure_step": seen_exposure if seen_exposure is not None and seen_exposure <= step else None,
                    "reuse_now": step in reuses[aid], "reuse_history": [s for s in reuses[aid] if s <= step],
                    "phase1": output, "action": action, "displacement": displacement})
            frames.append({"step": step, "phase": phase, "position_step": position_step,
                "hazard": hazards[step]["row"]["rectangles"] if step else [],
                "hazard_reference": hazards[step]["reference"] if step else None,
                "official_now": [r for r in issued if r["row"]["step"] == step],
                "official_history": [r for r in issued if r["row"]["step"] <= step],
                "exposures_now": [r for r in exposures if r["row"]["step"] == step],
                "deliveries": deliveries, "draw_delivery_arrows": phase == "communication",
                "agents": frame_agents, "arrival_count": sum(a["ever_arrived"] for a in frame_agents)})
    return {"version": VERSION, "run_id": meta["run_id"], "status": meta["status"],
        "completed_steps": completed, "duration": duration, "source_git_sha": meta["git_sha"],
        "config_sha256": compute_config_hash(config), "seed": config["simulation"]["seed"],
        "half_space_size": config["simulation"]["half_space_size"],
        "communication_radius": config["agents"]["communication_radius"],
        "refuges": refuges, "warning_id": warning_id, "model_colors": model_colors,
        "semantics": SEMANTICS, "frames": frames}


def script_json(value) -> str:
    # Encoding for a JSON data element, not a transformed public copy of raw logs.
    return canonical(value).decode("utf-8").replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


HTML = r'''<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>Disaster run · 観測者視点</title>
<style>
:root{font-family:system-ui,sans-serif;color:#203243;background:#edf2f5}*{box-sizing:border-box}body{margin:0;padding:24px;max-width:1700px;margin-inline:auto}h1{font-size:27px;margin:5px 0}h2{font-size:17px}p{line-height:1.6}header p{margin:5px 0;color:#536578}.eyebrow{font-size:12px;letter-spacing:.15em;color:#39675a;font-weight:700}.mono{font:12px ui-monospace,monospace;overflow-wrap:anywhere}.card{background:white;border:1px solid #d7e1e7;border-radius:12px;padding:18px}.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:20px 0}button,select{padding:8px 12px;border:1px solid #b7c7d2;border-radius:6px;background:white;color:#203243}button{cursor:pointer}input[type=range]{flex:1;min-width:160px}.grid{display:grid;grid-template-columns:minmax(470px,1.25fr) minmax(360px,1fr);gap:20px}canvas{width:100%;height:auto;display:block}.banner{padding:10px;border-radius:6px;background:#fff4d7;min-height:45px}.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;margin:12px 0}.swatch{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}.small{font-size:12px;color:#516575}.tablewrap{max-height:465px;overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:8px 5px;border-bottom:1px solid #e7edf0;white-space:nowrap}th{position:sticky;top:0;background:#f2f6f8}tr.selected{background:#eaf5f9}tr{cursor:pointer}.negative{color:#ae3024;font-weight:700}.badge{font-size:10px;display:inline-block;padding:2px 4px;background:#fff1cc;border-radius:4px;margin:1px}.reuse{background:#e8dcf7}.events{max-height:290px;overflow:auto}.event{white-space:pre-wrap;overflow-wrap:anywhere;border-top:1px solid #e1e9ee;padding:9px 0;font-size:12px}details{margin-top:18px}summary{cursor:pointer}.stat{font-size:18px;font-weight:700;margin:12px 0}@media(max-width:950px){body{padding:12px}.grid{grid-template-columns:1fr}}
</style>
<header><div class="eyebrow">RECORDED DISASTER RUN / OBSERVER VIEW</div><h1>災害runの再生 · 観測者視点</h1><p id="run" class="mono"></p><p id="status"></p><p class="small">避難所の座標・現在セルの危険判定はagentへの提示情報です。全体の危険区域とモデル名は観測者向けの表示です。</p></header>
<div class="toolbar card"><button id="play">再生</button><button id="prev">−1 step</button><input id="step" type="range" min="0" value="0" aria-label="step"><button id="next">+1 step</button><select id="phase" aria-label="表示時点"><option value="communication">通信後・移動前</option><option value="end">step終了</option></select><span id="time"></span></div>
<div class="grid"><section class="card"><div id="banner" class="banner"></div><div id="legend" class="legend"></div><canvas id="map" width="850" height="850" aria-label="記録位置・避難所・危険区域の地図"></canvas><div class="legend"><span>緑枠：避難所（境界を含む）</span><span>橙面：当stepの危険区域</span><span>矢印：実際の配送（紫は警報IDを含む）</span></div><label class="small"><input type="checkbox" id="radius"> 選択agentだけ通信半径を表示</label><p class="small">IDを選ぶと記録を表示します。移動前の配送を、移動後の位置に結び付けません。モデル色はviewerのみの識別です。</p></section>
<section class="card"><h2>位置・時間内到達可能性</h2><div id="stat" class="stat"></div><div class="tablewrap"><table><thead><tr><th>ID / model</th><th>位置</th><th>距離 d</th><th>残り</th><th>余裕</th><th>観測履歴</th></tr></thead><tbody id="agents"></tbody></table></div><p class="small">d＝最寄り避難所までの最短マンハッタン距離。残り＝この位置から予定終了までの移動回数。余裕＝残り−d。負なら最短経路でも時間内到達不可。非負は到達予測ではありません。</p><h2 id="selected">agent記録</h2><div id="agentlog" class="events"></div><h2>当stepの配送・公式警報</h2><div id="events" class="events"></div></section></div>
<details class="card"><summary>表示規則と出典</summary><p class="small">「警報受信歴」と「後続stepでの警報ID再使用」を区別します。再使用はPhase 1のmessageに完全一致する警報IDが出現した記録で、理解・採用を意味しません。move命令でも境界では実変位0になる場合があります。到達は初期／各step終了の記録位置で一度でも避難所内に入ったagent数です。中止runでは完了stepまでだけ再生し、残りは実行予定の上限から計算します。モデルのreasoningは説明欄であり、内部思考ではありません。</p><pre id="provenance" class="mono"></pre></details>
<script id="data" type="application/json">__DATA__</script>
<script>
'use strict';
const D=JSON.parse(document.getElementById('data').textContent),el=id=>document.getElementById(id),ctx=el('map').getContext('2d');
let selected=D.frames[0].agents[0].id,timer=null;
el('run').textContent=D.run_id;el('status').textContent=`${D.status} · 完了 ${D.completed_steps} / 予定 ${D.duration} step · seed ${D.seed}`;el('step').max=D.completed_steps;
el('provenance').textContent=JSON.stringify({version:D.version,source_git_sha:D.source_git_sha,config_sha256:D.config_sha256,raw_files:D.raw_files,strict_unverifiable:D.strict_unverifiable,semantics:D.semantics},null,2);
function node(tag,text,className){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;return n;}
Object.entries(D.model_colors).forEach(([model,color])=>{const n=node('span'),s=node('i',undefined,'swatch');s.style.background=color;n.append(s,document.createTextNode(model));el('legend').append(n);});
const S=710/(D.half_space_size*2+1),lo=-D.half_space_size-.5,hi=D.half_space_size+.5;
const px=x=>70+(x-lo)*S,py=y=>780-(y-lo)*S;
function rect(r,fill,stroke){ctx.fillStyle=fill;ctx.strokeStyle=stroke;const x=px(r.x_min-.5),y=py(r.y_max+.5),w=(r.x_max-r.x_min+1)*S,h=(r.y_max-r.y_min+1)*S;if(fill)ctx.fillRect(x,y,w,h);if(stroke){ctx.lineWidth=2;ctx.strokeRect(x,y,w,h);}}
function line(a,b,color){const x=px(a[0]),y=py(a[1]),xx=px(b[0]),yy=py(b[1]),angle=Math.atan2(yy-y,xx-x);ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=1.8;ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(xx,yy);ctx.stroke();ctx.beginPath();ctx.moveTo(xx,yy);ctx.lineTo(xx-9*Math.cos(angle-.35),yy-9*Math.sin(angle-.35));ctx.lineTo(xx-9*Math.cos(angle+.35),yy-9*Math.sin(angle+.35));ctx.closePath();ctx.fill();}
function draw(f){ctx.clearRect(0,0,850,850);ctx.fillStyle='#fafcfd';ctx.fillRect(70,70,710,710);f.hazard.forEach(r=>rect(r,'#efb66d77',null));D.refuges.forEach(r=>{rect(r.rectangle,null,'#267057');ctx.fillStyle='#225f4d';ctx.font='12px system-ui';ctx.textAlign='center';ctx.fillText(r.refuge_id,px((r.rectangle.x_min+r.rectangle.x_max)/2),py(r.rectangle.y_max+.5)-7);});
ctx.lineWidth=1;ctx.strokeStyle='#b7c6d0';ctx.strokeRect(70,70,710,710);ctx.font='12px system-ui';ctx.fillStyle='#455f71';for(let v=Math.ceil(lo/10)*10;v<=hi;v+=10){ctx.strokeStyle='#aac0ce55';ctx.beginPath();ctx.moveTo(px(v),70);ctx.lineTo(px(v),780);ctx.moveTo(70,py(v));ctx.lineTo(780,py(v));ctx.stroke();ctx.textAlign='center';ctx.fillText(v,px(v),803);ctx.textAlign='right';ctx.fillText(v,60,py(v)+4);}ctx.textAlign='center';ctx.fillText('X',425,829);ctx.fillText('Y',30,425);
const byId=new Map(f.agents.map(a=>[a.id,a]));if(el('radius').checked){const p=byId.get(selected).position;ctx.strokeStyle='#637e95';ctx.setLineDash([5,5]);ctx.beginPath();ctx.arc(px(p[0]),py(p[1]),D.communication_radius*S,0,2*Math.PI);ctx.stroke();ctx.setLineDash([]);}
if(f.draw_delivery_arrows)f.deliveries.forEach(m=>m.receivers.forEach(r=>line(byId.get(m.sender).position,byId.get(r).position,m.warning_carrier?'#884cba':'#738b9b99')));
f.agents.forEach(a=>{const x=px(a.position[0]),y=py(a.position[1]);ctx.fillStyle=D.model_colors[a.model];ctx.beginPath();ctx.arc(x,y,a.id===selected?7:5,0,2*Math.PI);ctx.fill();if(a.exposure_step!==null){ctx.strokeStyle='#b17e00';ctx.lineWidth=2;ctx.beginPath();ctx.arc(x,y,10,0,2*Math.PI);ctx.stroke();}if(a.reuse_now){ctx.strokeStyle='#884cba';ctx.lineWidth=3;ctx.strokeRect(x-13,y-13,26,26);}ctx.font='bold 12px system-ui';ctx.textAlign='left';ctx.lineWidth=3;ctx.strokeStyle='#fff';ctx.strokeText(String(a.id),x+9,y-8);ctx.fillStyle='#142f45';ctx.fillText(String(a.id),x+9,y-8);});}
function event(container,text,ref){const n=node('div',text,'event');if(ref)n.append(node('div',`${ref.file}:${ref.line} · sha256 ${ref.sha256}`,'mono'));container.append(n);}
function update(){const step=Number(el('step').value),phase=step===0?'initial':el('phase').value,f=D.frames.find(x=>x.step===step&&x.phase===phase);el('time').textContent=`step ${step} · 位置記録 ${f.position_step}`;
el('banner').textContent=f.official_now.length?f.official_now.map(x=>`公式警報 ${x.row.warning_id} → agent ${x.row.recipient_ids.join(', ')}`).join(' / '):f.official_history.length?`公式警報発令済み（step ${f.official_history.map(x=>x.row.step).join(', ')}）。当stepに再発令なし。`:'公式警報は未発令';
el('stat').textContent=`避難所到達 ${f.arrival_count} / ${f.agents.length} · 現在避難所内 ${f.agents.filter(a=>a.in_refuge).length}`;el('agents').replaceChildren();
f.agents.forEach(a=>{const row=node('tr',undefined,a.id===selected?'selected':'');row.onclick=()=>{selected=a.id;update();};[`${a.id} · ${a.model}`,a.position.join(', '),a.distance,a.remaining,a.margin].forEach((v,i)=>row.append(node('td',String(v),i===4&&a.margin<0?'negative':'')));const badges=node('td');if(a.exposure_step!==null)badges.append(node('span',`受信歴 s${a.exposure_step}`,'badge'));if(a.reuse_history.length)badges.append(node('span',a.reuse_now?'ID再使用 当step':`ID再使用歴 ${a.reuse_history.length}件`,'badge reuse'));row.append(badges);el('agents').append(row);});
const a=f.agents.find(x=>x.id===selected);el('selected').textContent=`agent ${a.id} · ${a.model}`;el('agentlog').replaceChildren();event(el('agentlog'),`位置 (${a.position.join(', ')})`,a.position_reference);if(a.phase1)event(el('agentlog'),`Phase 1 message: ${JSON.stringify(a.phase1.row.parsed.message)}`,a.phase1.reference);if(a.action)event(el('agentlog'),`Phase 3: ${a.action.row.action} ${a.action.row.direction??''}\n実変位: (${a.displacement.join(', ')})\nmemory: ${a.action.row.memory}\nreasoning（説明欄）: ${a.action.row.reasoning}`,a.action.reference);else if(step)event(el('agentlog'),'Phase 3はこの表示時点の後です。');
el('events').replaceChildren();f.official_now.forEach(x=>event(el('events'),`公式警報: ${JSON.stringify(x.row.payload)}`,x.reference));f.deliveries.forEach(m=>event(el('events'),`${m.sender} → ${m.receivers.join(', ')}${m.warning_carrier?' [警報IDを含む]':''}\n${JSON.stringify(m.text)}`,m.reference));if(!f.deliveries.length)event(el('events'),'当stepのagent間配送 0件');draw(f);}
function stop(){clearInterval(timer);timer=null;el('play').textContent='再生';}el('play').onclick=()=>{if(timer){stop();return;}if(Number(el('step').value)>=D.completed_steps)el('step').value=0;el('play').textContent='停止';timer=setInterval(()=>{el('step').value=Math.min(D.completed_steps,Number(el('step').value)+1);update();if(Number(el('step').value)>=D.completed_steps)stop();},500);};el('prev').onclick=()=>{stop();el('step').value=Math.max(0,Number(el('step').value)-1);update();};el('next').onclick=()=>{stop();el('step').value=Math.min(D.completed_steps,Number(el('step').value)+1);update();};el('step').oninput=()=>{stop();update();};el('phase').onchange=update;el('radius').onchange=update;update();
</script></html>
'''


def render_html(replay: dict) -> bytes:
    return HTML.replace("__DATA__", script_json(replay)).encode("utf-8")


def render_png(replay: dict) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Rectangle as PatchRectangle

    final = replay["frames"][-1]
    fig, (axis, chart) = plt.subplots(1, 2, figsize=(14, 8), gridspec_kw={"width_ratios": [1.3, 1]})
    for rect in final["hazard"]:
        axis.add_patch(PatchRectangle((rect["x_min"]-.5, rect["y_min"]-.5), rect["x_max"]-rect["x_min"]+1,
            rect["y_max"]-rect["y_min"]+1, color="#efb66d", alpha=.35))
    for refuge in replay["refuges"]:
        rect = refuge["rectangle"]
        axis.add_patch(PatchRectangle((rect["x_min"]-.5, rect["y_min"]-.5), rect["x_max"]-rect["x_min"]+1,
            rect["y_max"]-rect["y_min"]+1, fill=False, edgecolor="#267057", linewidth=2))
        axis.text((rect["x_min"]+rect["x_max"])/2, rect["y_max"]+1.2, refuge["refuge_id"], ha="center", fontsize=8)
    end_frames = [f for f in replay["frames"] if f["phase"] in ("initial", "end")]
    for a in final["agents"]:
        points = [next(p["position"] for p in f["agents"] if p["id"] == a["id"]) for f in end_frames]
        color = replay["model_colors"][a["model"]]
        axis.plot([p[0] for p in points], [p[1] for p in points], color=color, alpha=.55, linewidth=1)
        axis.scatter(*points[0], marker="^", s=18, color=color)
        axis.scatter(*points[-1], marker="s", s=24, color=color)
        axis.annotate(str(a["id"]), points[-1], xytext=(3, 4), textcoords="offset points", fontsize=7)
    bound = replay["half_space_size"] + .5
    axis.set(xlim=(-bound, bound), ylim=(-bound, bound), aspect="equal", xlabel="X", ylabel="Y",
             title=f"Recorded trajectories; hazard at step {final['step']}")
    axis.grid(alpha=.15)
    legend = [Line2D([0], [0], color=c, label=m) for m, c in replay["model_colors"].items()]
    axis.legend(handles=legend, loc="lower center", bbox_to_anchor=(.5, -.23), fontsize=8, frameon=False)
    for aid in [a["id"] for a in final["agents"]]:
        history = [next(a for a in f["agents"] if a["id"] == aid) for f in end_frames]
        chart.plot([f["position_step"] for f in end_frames], [a["margin"] for a in history],
                   color=replay["model_colors"][history[0]["model"]], alpha=.55, linewidth=1)
    chart.axhline(0, color="#ae3024", linestyle="--", linewidth=1)
    chart.set(xlabel="Completed movement step", ylabel="Remaining moves - nearest refuge distance",
              title="Time-to-refuge feasibility margin", xlim=(0, max(1, replay["duration"])))
    chart.grid(alpha=.15)
    fig.suptitle(f"Disaster run | OBSERVER VIEW | seed {replay['seed']}", fontsize=17, y=.97)
    fig.text(.5, .915, f"{replay['status']} | {replay['completed_steps']}/{replay['duration']} steps | "
        f"ever arrived: {final['arrival_count']}/{len(final['agents'])} | "
        f"warning-exposed: {sum(a['exposure_step'] is not None for a in final['agents'])} | "
        f"later exact-ID reuse: {sum(bool(a['reuse_history']) for a in final['agents'])}", ha="center", fontsize=10)
    fig.text(.5, .055, "Triangles: initial; squares + IDs: final. Lines connect recorded positions; a move command may yield zero displacement.\n"
        "Green outlines: inclusive refuges. Orange: final-step hazard only. Global hazard geometry is an observer view.\n"
        "Negative margin: shortest-path arrival by the planned horizon is impossible. Nonnegative margin does not predict arrival.\n"
        "Warning exposure and later exact-ID reuse do not establish understanding, adoption, or warning-caused evacuation.",
        ha="center", fontsize=8, linespacing=1.4)
    fig.subplots_adjust(left=.06, right=.97, top=.84, bottom=.26, wspace=.3)
    stream = io.BytesIO()
    fig.savefig(stream, format="png", dpi=170, metadata={"Software": VERSION})
    plt.close(fig)
    return stream.getvalue()


def create_replay(run_dir: Path, output_dir: Path) -> Path:
    output = require_output_path(output_dir, run_dir)
    before = snapshot_tree(run_dir)
    if not public_tree_safe(run_dir, []):
        raise ValueError("run failed read-only publication boundary including decoded bodies")
    validation = validate_run(run_dir, strict=True)
    if not validation.valid:
        raise ValueError("run failed strict validation")
    meta = json.loads((run_dir / "run_meta.json").read_bytes())
    replay = build_replay(meta, read_records(run_dir))
    replay["raw_files"] = before
    replay["strict_unverifiable"] = validation.unverifiable
    payloads = {"replay.html": render_html(replay), "summary.png": render_png(replay)}
    manifest = {"version": VERSION, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": meta["run_id"], "source_git_sha": meta["git_sha"], "config": meta["config"],
        "config_sha256": replay["config_sha256"], "run_status": meta["status"],
        "completed_steps": meta["completed_steps"], "raw_files": before,
        "strict_validation": {"valid": True, "unverifiable": validation.unverifiable},
        "publication_boundary": "passed including decoded response bodies; no runtime bindings supplied",
        "semantics": SEMANTICS,
        "implementation": {name: digest((REPO_ROOT / name).read_bytes()) for name in (
            "tools/render_disaster_run.py", "tools/validate_run.py", "tools/scan_publication.py",
            "tools/run_disaster_behavior_pilot.py", "engine/disaster.py", "tools/artifact_boundaries.py")},
        "artifacts": {name: {"sha256": digest(data), "bytes": len(data)} for name, data in payloads.items()},
        "raw_snapshot_unchanged": True}
    manifest_bytes = canonical(manifest)
    for name, payload in {"input_manifest.json": manifest_bytes, "replay.html": payloads["replay.html"]}.items():
        if scan_text(name, payload.decode("utf-8")):
            raise ValueError("derived content failed publication boundary before output creation")
    if snapshot_tree(run_dir) != before:
        raise ValueError("run changed during rendering; output not created")
    require_output_path(output, run_dir)
    output.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        with (output / name).open("xb") as handle:
            handle.write(payload)
    if snapshot_tree(run_dir) != before:
        raise ValueError("run changed during publication; no completion manifest created")
    with (output / "input_manifest.json").open("xb") as handle:
        handle.write(manifest_bytes)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        output = create_replay(args.run_dir, args.output_dir)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"FAIL: disaster replay rejected ({type(error).__name__})", file=sys.stderr)
        return 1
    print(f"PASS: disaster replay written to {output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
