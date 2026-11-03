/** Read-only measured calculation history. Missing progress never becomes a fictional first plan. */
import "./chart_theme.js";
import {when, money, percentage, localDate, useHomeAssistantTimezone} from "./display.js";
import {bindSettingsActions} from "./settings-actions.js";
bindSettingsActions();
const find = id => document.getElementById(id);
const base = document.querySelector('meta[name="oep-api-base"]').content;
const seconds = value => Number.isFinite(value) ? `${value.toLocaleString(undefined,{maximumFractionDigits:1})} s` : "Not recorded";
const clock = value => when(value,{hour:"2-digit",minute:"2-digit",timeZoneName:"short"});
const bottomLegend = {id:"performanceBottomLegend",beforeLayout(chart){chart.options.plugins.legend.position="bottom";}};
let token = "", chart = null, detailChart = null, rows = [], requestVersion = 0, detailVersion = 0;
/** Keep credentials in memory and never display raw server errors. */
async function api(path) {
 const response = await fetch(base + path,{headers:token ? {Authorization:`Bearer ${token}`} : {},redirect:"error",cache:"no-store"});
 if(response.status===401)find("connect").hidden=false;
 if (!response.ok) throw new Error(response.status === 401 ? "Connect to read calculation history." : "Calculation history could not be loaded. Try Refresh.");
 return response.json();
}
/** State facts only. Solver termination does not establish the cause of a slow calculation. */
function outcome(row) {
 if (row.reason === "published") return row.stop_reason === "time_limit" ? "Plan available: Time budget reached" : "Plan available";
 if (row.reason.includes("changed") || row.reason.includes("expired") || row.reason.includes("unavailable")) return "Not published: Inputs changed or became unavailable";
 return row.stop_reason?.includes("time_limit") || row.stop_reason === "worker_timeout" ? "No plan published: Time budget reached" : "No plan published";
}
/** Native dialog preserves focus, Escape dismissal and background inertness. */
async function detail(row) {
 const version = ++detailVersion, dialog = find("calculation-detail");
 detailChart?.destroy(); detailChart=null;
 find("calculation-summary").textContent="Loading calculation details.";
 find("calculation-time").textContent=when(row.started_at,{timeZoneName:"short"});
 for(const id of ["calculation-facts","calculation-cost-note","calculation-proof","calculation-samples"])find(id).replaceChildren();
 if(!dialog.open)dialog.showModal();
 try { row = await api(`v1/planning-performance/${encodeURIComponent(row.id)}`); }
 catch(error){if(version===detailVersion && dialog.open)find("calculation-summary").textContent=error.message;return;}
 if(version!==detailVersion || !dialog.open)return;
 const samples = row.progress || [], first = samples[0], last = samples.at(-1);
 find("calculation-time").textContent = when(row.started_at,{timeZoneName:"short"});
 find("calculation-summary").textContent = `${outcome(row)}: Total ${seconds(row.duration_seconds)}. ${row.first_candidate_seconds != null ? `First candidate observed after ${seconds(row.first_candidate_seconds)}.` : "First candidate time was not recorded."}`;
 const facts = find("calculation-facts"); facts.replaceChildren();
 for (const [label,value] of [["Preparation",seconds(row.build_seconds)],["Service priorities",row.financial_started_seconds == null ? "Not separately recorded" : seconds(Math.max(0,row.financial_started_seconds - row.build_seconds))],["Strategy",row.strategy === "minimize consumption" ? "Minimize consumption" : "Minimize cost"],["Calculation intervals",row.intervals],["Solver time budget",seconds(row.time_limit_seconds)],["Result",outcome(row)]]) {
  const group = document.createElement("div"), term = document.createElement("dt"), description = document.createElement("dd");
  term.textContent = label; description.textContent = value; group.append(term,description); facts.append(group);
 }
 const unit = row.currency || "contract currency";
 find("calculation-cost-note").textContent = samples.length > 1 ? `Observed improvement within this calculation: ${money(Math.max(0,first.objective-last.objective))} ${unit}.` : "No sequence of improvements was recorded. A single point does not establish when the first candidate was found.";
 const bound = last?.best_bound, absoluteGap = bound == null ? null : Math.max(0,last.objective-bound);
 find("calculation-proof").textContent = absoluteGap == null ? "Remaining possible improvement was not recorded." : `Remaining possible improvement according to the model: Up to ${money(absoluteGap)} ${unit}${Math.abs(last.objective) > 0 ? ` (${percentage(100*absoluteGap/Math.abs(last.objective))})` : ""}. This is a quality bound, not percentage completed. Physical checks remain separate.`;
 detailChart?.destroy();
 detailChart = new Chart(find("improvement-chart"),{plugins:[bottomLegend],type:"line",data:{datasets:[{label:`Candidate cost (${unit})`,data:samples.map(sample=>({x:sample.elapsed_seconds,y:sample.objective})),borderColor:"#087f8c",backgroundColor:"#087f8c",stepped:"before",pointRadius:2},{label:`Best possible bound (${unit})`,data:samples.map(sample=>({x:sample.elapsed_seconds,y:sample.best_bound})),borderColor:"#b7791f",borderDash:[6,4],stepped:"before",pointRadius:0,spanGaps:false}]},options:{responsive:true,maintainAspectRatio:false,animation:false,scales:{x:{type:"linear",min:0,title:{display:true,text:"Elapsed seconds"}},y:{ticks:{callback:money}}},plugins:{tooltip:{callbacks:{label:item=>`${item.dataset.label}: ${money(item.parsed.y)}`}}}}});
 const table = find("calculation-samples"); table.replaceChildren();
 for (const sample of samples) {const item = document.createElement("tr"); for (const value of [seconds(sample.elapsed_seconds),money(sample.objective),money(sample.best_bound)]) {const cell = document.createElement("td"); cell.textContent = value; item.append(cell);} table.append(item);}
}
/** Render the same values as an accessible list and a time-scaled duration chart. */
function render() {
 chart?.destroy();
 find("performance-summary").textContent = rows.length ? `${rows.length} completed calculations: Longest ${seconds(Math.max(...rows.map(row=>row.duration_seconds)))}.` : "No completed calculations were recorded for this day. History begins after this feature is installed.";
 const series = [["First candidate observed","#087f8c",row=>row.first_candidate_seconds],["Further calculation","#8cb9d5",row=>row.first_candidate_seconds == null ? null : Math.max(0,row.duration_seconds-row.first_candidate_seconds)],["First time unknown","#64748b",row=>row.first_candidate_seconds == null ? row.duration_seconds : null]];
 chart = new Chart(find("performance-chart"),{plugins:[bottomLegend],type:"bar",data:{datasets:series.map(([label,color,value])=>({label,backgroundColor:color,data:rows.map(row=>({x:Date.parse(row.started_at),y:value(row)})),maxBarThickness:24}))},options:{responsive:true,maintainAspectRatio:false,animation:false,onClick:(_event,elements)=>{if(elements[0])detail(rows[elements[0].index]);},scales:{x:{type:"linear",stacked:true,ticks:{maxTicksLimit:4,maxRotation:0,minRotation:0,callback:value=>when(value,{hour:"2-digit",minute:"2-digit",hourCycle:"h23"})}},y:{stacked:true,min:0,title:{display:true,text:"Seconds"}}},plugins:{tooltip:{callbacks:{title:items=>when(rows[items[0].dataIndex].started_at,{timeZoneName:"short"}),afterBody:items=>rows[items[0].dataIndex].first_candidate_seconds == null ? "First candidate time not recorded. The full bar shows total duration." : outcome(rows[items[0].dataIndex])}}}}});
 const budgets = [...new Set(rows.map(row=>row.time_limit_seconds).filter(Number.isFinite))];
 if (budgets.length === 1) {
  const budget = budgets[0];
  chart.data.datasets.push({type:"line",label:`Solver budget: ${seconds(budget)}`,data:rows.map(row=>({x:Date.parse(row.started_at),y:budget})),borderColor:"#6b7785",borderWidth:1,borderDash:[4,4],pointRadius:0,stack:"budget"});
  chart.update();
 }
 const list = find("performance-list"); list.replaceChildren();
 for (const row of rows) {const item = document.createElement("div"), identity = document.createElement("p"), duration = document.createElement("p"), action = document.createElement("button"); item.className="performance-row"; duration.className="performance-duration"; identity.textContent=`${clock(row.started_at)}: ${outcome(row)}`; duration.textContent=seconds(row.duration_seconds); action.type="button"; action.className="secondary"; action.textContent="View calculation"; action.setAttribute("aria-label",`View calculation at ${clock(row.started_at)}`); action.addEventListener("click",()=>detail(row)); item.append(identity,duration,action); list.append(item);}
}
/** Ignore late responses after a day change. Failed reads clear stale data rather than misdate it. */
async function load() {
 const version = ++requestVersion; find("performance-status").textContent="Loading calculation history.";
 try {const selected=find("performance-day").value; const data=await api(`v1/planning-performance${selected ? `?day=${encodeURIComponent(selected)}` : ""}`); if(version!==requestVersion)return; useHomeAssistantTimezone(data.timezone); find("performance-day").value=data.day; rows=data.calculations; render(); find("performance-status").textContent="";
 const address=new URL(location.href), selectedId=address.searchParams.get("calculation"); if(selectedId){address.searchParams.delete("calculation");history.replaceState(null,"",address);const selectedRow=rows.find(row=>row.id===selectedId);if(selectedRow&&!find("calculation-detail").open)detail(selectedRow);}
 } catch(error){if(version!==requestVersion)return; rows=[]; render(); find("performance-summary").textContent="Calculation history is unavailable for the selected day."; find("performance-status").textContent=error.message;}
}
find("calculation-detail").addEventListener("close",()=>{detailVersion++;detailChart?.destroy();detailChart=null;});
find("calculation-close").addEventListener("click",()=>find("calculation-detail").close());
find("performance-day").addEventListener("change",load);
find("performance-refresh").addEventListener("click",load);
find("performance-today").addEventListener("click",()=>{find("performance-day").value=localDate();load();});
find("connect").addEventListener("submit",event=>{event.preventDefault();token=find("token").value;find("token").value="";find("connect").hidden=true;load();});
api("setup/session").then(session=>{useHomeAssistantTimezone(session.timezone);find("performance-day").value=new URLSearchParams(location.search).get("day")||localDate();find("connect").hidden=Boolean(session.ingress);if(session.ingress)load();else find("performance-status").textContent="Connect to read calculation history.";}).catch(error=>{find("performance-status").textContent=error.message;});
