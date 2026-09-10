"""Self-contained warning-retention comparison; model strings are inert text."""
from __future__ import annotations

import json


def render_html(summary, completed):
    data = {"summary": summary, "runs": []}
    for run in completed:
        calc = run["calculated"]
        # The complete prompt remains in immutable raw JSONL; no selected row is hidden.
        data["runs"].append({"run_id": run["run_id"], "seed": run["seed"], "condition": run["condition"],
            "source_sha": run["source_sha"], "config_sha256": run["config_sha256"],
            "config": run["config"], "phases": calc["agent_phases"], "agents": calc["agents"],
            "steps": calc["geometry"]["agent_steps"], "speech": calc["speech"],
            "receptions": calc["receptions"], "warning_outputs": calc["geometry"]["warning"]["outputs"]})
    # Script-data encoding, never interpolation into executable code or markup.
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__DATA__", payload).encode("utf-8")


TEMPLATE = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Warning retention · A/B observations</title>
<style>
:root{font-family:system-ui,sans-serif;color:#183132;background:#f4f4ed}*{box-sizing:border-box}
body{margin:0}header,main{max-width:1440px;margin:auto;padding:24px}header{border-bottom:1px solid #c8d2cc}
h1{font-size:30px;font-weight:650;margin:6px 0 12px}h2{font-size:20px;margin:0 0 12px}h3{font-size:14px;margin:4px 0 10px}
p{line-height:1.5}.muted{font-size:13px;color:#526664}.controls{display:flex;gap:24px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:#f4f4edf5;padding:14px 0;z-index:2}
label{font-size:13px;display:flex;gap:9px;align-items:center}select,input,button{font:inherit;accent-color:#176768}select,button{padding:6px 10px;border:1px solid #96ada5;border-radius:6px;background:white}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}.panel{background:#fff;border:1px solid #cad5cc;border-radius:12px;padding:18px;min-width:0}
.chain{display:grid;gap:10px;margin:14px 0}.card{border-left:3px solid #418583;background:#f3f7f5;padding:11px 14px;overflow-wrap:anywhere}
.card p{margin:5px 0;white-space:pre-wrap}.status{font-size:13px;margin:8px 0}canvas{width:100%;height:auto;background:#f7f8f4;border:1px solid #bfcfc5;border-radius:8px}
pre{white-space:pre-wrap;font:12px/1.5 ui-monospace,monospace;overflow-wrap:anywhere;margin:10px 0}details{font-size:12px;margin:8px 0}summary{cursor:pointer;color:#175e65}a{color:#175e65}
table{border-collapse:collapse;width:100%;font-size:13px;margin:12px 0}td,th{padding:8px;text-align:left;border-bottom:1px solid #d5ded6}th{font-weight:600}
.scroll{overflow:auto}.tag{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#436b65}.null{color:#647371}footer{padding:26px 0;border-top:1px solid #cad5cc;margin-top:26px;font-size:12px;line-height:1.7}
@media(max-width:800px){.pair{grid-template-columns:1fr}header,main{padding:16px}.controls{gap:12px}h1{font-size:25px}}
</style>
<header><div class="tag">Exploratory · 3 paired seeds · all 24 agents</div><h1>Where does the warning remain in the input?</h1>
<p>A uses the five latest received items. B retains the directly received official item and at most four latest peer items.</p>
<p class="muted">Reception → request input → own speech → actual destinations → later-step reuse → movement. These are separate observations. B replaces one peer slot; token counts are not equalized.</p></header>
<main><div class="controls"><label>Seed <select id="seed"></select></label><label>Agent <select id="agent"></select></label><label>Step <input id="step" type="range" min="1" max="60" value="10"><output id="stepvalue">10</output></label><button id="prev">Previous</button><button id="next">Next</button></div>
<div id="pairsummary" class="scroll"></div><div class="pair"><section id="recent" class="panel"></section><section id="retained" class="panel"></section></div>
<details><summary>All planned runs and eligibility</summary><div id="planned" class="scroll"></div></details>
<footer>All agent and step choices remain available, including nulls. A request-input observation does not establish model attention. Exact identifier matching does not capture arbitrary paraphrases; lexical flags are fixed review aids, not semantic reuse. Refuge geometry is shown in every prompt, so movement alone does not attribute an effect to the warning. Model-generated explanation fields do not expose internal reasoning. Compare the three paired runs descriptively; agents are not independent replicates. Raw links work when this derived directory stays inside the repository.</footer>
</main><script type="application/json" id="data">__DATA__</script><script>
'use strict';
const data=JSON.parse(document.getElementById('data').textContent),byId=x=>document.getElementById(x);
const seeds=[...new Set(data.summary.runs.map(r=>r.seed))].sort((a,b)=>a-b);
function el(tag,text,parent,cls){const n=document.createElement(tag);if(text!==undefined&&text!==null)n.textContent=String(text);if(cls)n.className=cls;if(parent)parent.append(n);return n}
function shown(v){return v===null||v===undefined?'null':typeof v==='object'?JSON.stringify(v):String(v)}
function pretty(v){return JSON.stringify(v,null,2)}
function detail(parent,title,value){const d=el('details',null,parent);el('summary',title,d);el('pre',pretty(value),d);return d}
function ref(parent,r){if(!r)return;const p=el('p',null,parent,'muted');const a=el('a',r.file+':'+r.line_number,p);a.href='../../runs/output_'+encodeURIComponent(r.run_id)+'/'+encodeURIComponent(r.file);a.target='_blank';a.rel='noopener';el('span',' · SHA256 '+r.line_sha256,p)}
function card(parent,title){const c=el('div',null,parent,'card');el('h3',title,c);return c}
function table(parent,headers,rows){const t=el('table',null,parent),h=el('tr',null,el('thead',null,t));headers.forEach(v=>el('th',v,h));const b=el('tbody',null,t);rows.forEach(vs=>{const r=el('tr',null,b);vs.forEach(v=>el('td',shown(v),r))});return t}
for(const s of seeds){const o=el('option',s,byId('seed'));o.value=s}
for(let a=0;a<24;a++){const o=el('option',a,byId('agent'));o.value=a}byId('agent').value='1';
for(const r of data.runs){r.pi=new Map(r.phases.map(x=>[x.step+':'+x.phase+':'+x.agent_id,x]));r.si=new Map(r.speech.map(x=>[x.step+':'+x.agent_id,x]));r.gi=new Map(r.steps.map(x=>[x.step+':'+x.agent_id,x]));}
table(byId('planned'),['Run','Status','Eligible','Calls'],data.summary.runs.map(r=>[r.run_id,r.status,r.eligible,r.calls]));
function drawMap(canvas,run,step,agent){const c=canvas.getContext('2d'),sz=580,pad=30,S=run.config.simulation.half_space_size,scale=(sz-2*pad)/(2*S+1);c.clearRect(0,0,sz,sz);const x=v=>pad+(v+S+.5)*scale,y=v=>sz-pad-(v+S+.5)*scale;
function rect(r,color){c.fillStyle=color;c.fillRect(x(r.x_min)-scale/2,y(r.y_max)-scale/2,(r.x_max-r.x_min+1)*scale,(r.y_max-r.y_min+1)*scale)}
const stages=run.config.scenario.hazard.stages.filter(r=>r.start_step<=step);if(stages.length)stages.at(-1).rectangles.forEach(r=>rect(r,'#f1d2bc'));
run.config.scenario.refuges.forEach(r=>rect(r.rectangle,'#b1dcca'));c.strokeStyle='#cfdbd2';c.lineWidth=1;c.strokeRect(pad,pad,sz-2*pad,sz-2*pad);
c.beginPath();for(let s=0;s<=step;s++){const g=run.gi.get(s+':'+agent);if(!g)continue;s?c.lineTo(x(g.position[0]),y(g.position[1])):c.moveTo(x(g.position[0]),y(g.position[1]))}c.strokeStyle='#226a70';c.lineWidth=2;c.stroke();
run.agents.forEach(a=>{const g=run.gi.get(step+':'+a.agent_id);if(!g)return;c.fillStyle=a.agent_id===agent?'#102f35':a.initial_official_recipient?'#9d6240':'#93aaa4';c.beginPath();c.arc(x(g.position[0]),y(g.position[1]),a.agent_id===agent?7:3.5,0,Math.PI*2);c.fill();if(a.agent_id===agent){c.font='bold 14px system-ui';c.fillText(String(agent),x(g.position[0])+10,y(g.position[1])-6)}});
c.font='12px system-ui';c.fillStyle='#43615e';c.fillText('-'+S,pad,sz-10);c.fillText(String(S),sz-pad-12,sz-10);c.fillText('+'+S,2,pad+5);c.fillText('-'+S,2,sz-pad);}
function renderPanel(condition,seed,agent,step){const node=byId(condition);node.replaceChildren();el('h2',condition==='recent'?'A · Latest five items':'B · Official item retained',node);const status=data.summary.runs.find(r=>r.seed===seed&&r.condition===condition),r=data.runs.find(r=>r.seed===seed&&r.condition===condition);
if(!r){el('p','No eligible completed observations: '+shown(status&&status.status),node,'null');el('p','Selected agent '+agent+' · step '+step+' · measurements null',node);return}
const endpoint=r.agents.find(x=>x.agent_id===agent),g=r.gi.get(step+':'+agent),sp=r.si.get(step+':'+agent),p1=r.pi.get(step+':phase1:'+agent),p3=r.pi.get(step+':phase3:'+agent);
el('p',endpoint.model+' · '+(endpoint.initial_official_recipient?'initial official recipient':'noninitial recipient'),node,'muted');const canvas=el('canvas',null,node);canvas.width=580;canvas.height=580;drawMap(canvas,r,step,agent);el('p','Green: refuge · peach: hazard · brown: initial recipients · dark path: selected agent',node,'muted');
const chain=el('div',null,node,'chain'),rec=r.receptions.filter(x=>x.recipient_id===agent&&x.step<=step),now=rec.filter(x=>x.step===step);let c=card(chain,'1 · Warning reception');el('p','Cumulative exact-ID exposure events: '+rec.length+' · this step: '+now.length,c);el('p','Official source received: '+shown(p1.official_received_before_phase),c);detail(c,'Reception records and raw references',rec);
c=card(chain,'2 · Phase 1 request input');el('p','Official item selected: '+p1.official_selected+' · identifier anywhere in prompt: '+shown(p1.exact_id_in_actual_prompt)+' · selected items: '+p1.selected_message_count,c);detail(c,'All selected Phase 1 items',p1.selected_messages);ref(c,p1.prompt_input_reference);
c=card(chain,'3 · Own Phase 1 speech');el('p',sp.message===''?'(empty message)':sp.message,c);el('p','Exact ID: '+sp.exact_warning_id+' · hazard-word flag: '+sp.hazard_word+' · refuge-word flag: '+sp.refuge_word,c,'muted');ref(c,sp.speech_reference);
c=card(chain,'4 · Actual destinations and own later reuse');el('p','Receiver IDs: '+shown(sp.receiver_ids),c);const reuse=r.warning_outputs.filter(x=>x.agent_id===agent&&x.step<=step&&x.exact_warning_id_carrier&&x.later_step_reuse_eligible);el('p','Selected agent own later-step exact-ID output count through this step: '+reuse.length,c);ref(c,sp.delivery_reference);detail(c,'V2 later-step reuse rows',reuse);
c=card(chain,'5 · Phase 3 request input');el('p','Official item selected: '+p3.official_selected+' · identifier anywhere in prompt: '+shown(p3.exact_id_in_actual_prompt)+' · selected items: '+p3.selected_message_count,c);detail(c,'All selected Phase 3 items',p3.selected_messages);ref(c,p3.prompt_input_reference);
c=card(chain,'6 · Action and post-movement geometry');el('p',g.action+' '+shown(g.direction)+' → '+shown(g.position)+' · displacement '+shown(g.displacement),c);el('p','Distance to refuge: '+g.distance+' · refuge: '+shown(g.refuge_id)+' · hazardous: '+g.hazardous,c);ref(c,g.action_reference);ref(c,g.position_reference);detail(c,'Final endpoint and first-arrival censoring',endpoint);
detail(node,'Run provenance',{run_id:r.run_id,source_commit:r.source_sha,config_sha256:r.config_sha256,metric_version:data.summary.metric_version,config:r.config});}
function render(){const seed=Number(byId('seed').value),agent=Number(byId('agent').value),step=Number(byId('step').value);byId('stepvalue').textContent=step;const ps=byId('pairsummary');ps.replaceChildren();const p=data.summary.pairs.find(x=>x.seed===seed);if(p&&p.eligible){const keys=['phase1_official_selected','phase3_official_selected','noninitial_exact_id_peer_recipient_count','noninitial_later_step_reused_agents','arrived_agent_count','final_refuge_occupancy','mean_final_distance','hazard_agent_steps'];table(ps,['Run-level outcome','A','B','B minus A'],keys.map(k=>[k.replaceAll('_',' '),p.recent[k],p.retained[k],p.retained_minus_recent[k]]))}else el('p','Pair unavailable/ineligible; differences are null.',ps);renderPanel('recent',seed,agent,step);renderPanel('retained',seed,agent,step)}
['seed','agent','step'].forEach(id=>byId(id).addEventListener('input',render));byId('prev').addEventListener('click',()=>{byId('step').value=Math.max(1,Number(byId('step').value)-1);render()});byId('next').addEventListener('click',()=>{byId('step').value=Math.min(60,Number(byId('step').value)+1);render()});render();
</script></html>'''
