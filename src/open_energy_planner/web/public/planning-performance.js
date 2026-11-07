/** Read-only measured calculation history. Missing progress never becomes a fictional first plan. */
import "./chart_theme.js";
import {when, money, percentage, localDate, nextDate, useHomeAssistantTimezone} from "./display.js";
import {calendarDayStart} from "./chart_days.js";
import {bindSettingsActions} from "./settings-actions.js";
bindSettingsActions();
const find = id => document.getElementById(id);
const base = document.querySelector('meta[name="oep-api-base"]').content;
const seconds = value => Number.isFinite(value) ? `${value.toLocaleString(undefined,{maximumFractionDigits:1})} s` : "Not recorded";
const MINUTE_MILLISECONDS = 60 * 1000;
let clockOccurrences = new Map();
/** Distinguish repeated installation-local minutes without displaying timezone names. */
const occurrence = value => clockOccurrences.get(Math.floor(new Date(value).getTime()/MINUTE_MILLISECONDS)) || "";
const clock = value => when(value,{hour:"2-digit",minute:"2-digit"}) + occurrence(value);
const bottomLegend = {id:"performanceBottomLegend",beforeLayout(chart){chart.options.plugins.legend.position="bottom";}};
const QUARTER_MILLISECONDS = 15 * MINUTE_MILLISECONDS;
const MAX_TIME_LABELS = 8;
let token = "", chart = null, detailChart = null, rows = [], requestVersion = 0, detailVersion = 0, plottedRows = [], loading = false;
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
 if (row.reason === "candidate_rejected" || row.reason === "consumption_objective_rejected") return "No plan published";
 return row.stop_reason?.includes("time_limit") || row.stop_reason === "worker_timeout" ? "No plan published: Time budget reached" : "No plan published";
}
/** Native dialog preserves focus, Escape dismissal and background inertness. */
async function detail(row) {
 const version = ++detailVersion, dialog = find("calculation-detail");
 detailChart?.destroy(); detailChart=null;
 find("calculation-summary").textContent="Loading calculation details.";
 find("calculation-time").textContent=when(row.started_at) + occurrence(row.started_at);
 for(const id of ["calculation-facts","calculation-cost-note","calculation-proof","calculation-samples"])find(id).replaceChildren();
 if(!dialog.open)dialog.showModal();
 try { row = await api(`v1/planning-performance/${encodeURIComponent(row.id)}`); }
 catch(error){if(version===detailVersion && dialog.open)find("calculation-summary").textContent=error.message;return;}
 if(version!==detailVersion || !dialog.open)return;
 const samples = row.progress || [], first = samples[0], last = samples.at(-1);
 find("calculation-time").textContent = when(row.started_at) + occurrence(row.started_at);
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
 detailChart = new Chart(find("improvement-chart"),{plugins:[bottomLegend],type:"line",data:{datasets:[{label:`Candidate cost (${unit})`,data:samples.map(sample=>({x:sample.elapsed_seconds,y:sample.objective})),borderColor:"#087f8c",backgroundColor:"#087f8c",stepped:"before",pointRadius:2},{label:`Best possible bound (${unit})`,data:samples.map(sample=>({x:sample.elapsed_seconds,y:sample.best_bound})),borderColor:"#b7791f",borderDash:[6,4],stepped:"before",pointRadius:0,spanGaps:false}]},options:{responsive:true,maintainAspectRatio:false,animation:false,scales:{x:{type:"linear",min:0,title:{display:true,text:"Elapsed seconds"}},y:{ticks:{callback:money}}},plugins:{tooltip:{callbacks:{title:items=>seconds(items[0].parsed.x),label:item=>`${item.dataset.label}: ${money(item.parsed.y)}`}}}}});
 const table = find("calculation-samples"); table.replaceChildren();
 for (const sample of samples) {const item = document.createElement("tr"); for (const value of [seconds(sample.elapsed_seconds),money(sample.objective),money(sample.best_bound)]) {const cell = document.createElement("td"); cell.textContent = value; item.append(cell);} table.append(item);}
}
/** Keep one native keyboard selection rather than duplicate the chart as a visible history list. */
function renderSelection() {
 const selection = find("performance-selection"), previous = selection.dataset.calculationId;
 selection.max = Math.max(0, plottedRows.length - 1);
 selection.value = Math.max(0, plottedRows.findIndex(row => row.id === previous));
 selection.disabled = loading || !plottedRows.length;
 find("performance-open").disabled = selection.disabled;
 find("performance-chart").tabIndex = selection.disabled ? -1 : 0;
 const row = plottedRows[selection.valueAsNumber];
 selection.dataset.calculationId = row?.id || "";
 find("performance-open").setAttribute("aria-label", row ? `View calculation at ${clock(row.started_at)}` : "View calculation");
 selection.setAttribute("aria-valuetext", row ? `Calculation ${selection.valueAsNumber + 1} of ${plottedRows.length}: ${clock(row.started_at)}: ${outcome(row)}: ${seconds(row.duration_seconds)}` : "No calculation selected.");
 find("performance-selected").textContent = row ? `${when(row.started_at)}${occurrence(row.started_at)}: ${outcome(row)}: Total ${seconds(row.duration_seconds)}: ${row.first_candidate_seconds == null ? "First candidate time not recorded." : `First candidate ${seconds(row.first_candidate_seconds)}: Further calculation ${seconds(Math.max(0,row.duration_seconds-row.first_candidate_seconds))}`}` : "No calculation selected.";
}
/** Highlight the selected bar and expose the same grouped tooltip without changing chart evidence. */
function showSelection() {
 if (loading || !chart || !plottedRows.length) return;
 const selection = find("performance-selection"), index = selection.valueAsNumber;
 selection.dataset.calculationId = plottedRows[index]?.id || "";
 renderSelection();
 const active = chart.getSortedVisibleDatasetMetas().filter(meta => meta.type === "bar" && Number.isFinite(chart.data.datasets[meta.index].data[index]?.y)).map(meta => ({datasetIndex:meta.index, index}));
 const point = active.length ? chart.getDatasetMeta(active[0].datasetIndex).data[index] : null;
 chart.setActiveElements(active);
 chart.tooltip.setActiveElements(active, {x:point?.x || 0, y:point?.y || 0});
 chart.update();
}
/** Open exactly the selected recorded calculation, including when all plotted series are hidden. */
function openSelection() {
 const row = plottedRows[find("performance-selection").valueAsNumber];
 if (!loading && row) detail(row);
}
/** Render the complete selected day and retain its original archived calculation identities. */
function render() {
 chart?.destroy();
 clockOccurrences = new Map();
 const clocks = new Map();
 for (const minute of [...new Set(rows.map(row=>Math.floor(Date.parse(row.started_at)/MINUTE_MILLISECONDS)))].sort((left,right)=>left-right)) {
  const label = clock(minute * MINUTE_MILLISECONDS), instants = clocks.get(label) || [];
  instants.push(minute); clocks.set(label,instants);
 }
 for (const instants of clocks.values()) if(instants.length>1) instants.forEach((minute,index)=>clockOccurrences.set(minute,` (${index+1} of ${instants.length})`));
 const day = find("performance-day").value || localDate();
 const minimum = calendarDayStart(day), maximum = calendarDayStart(nextDate(day));
 if (maximum <= minimum) {
  chart=null;
  plottedRows = []; renderSelection(); find("performance-legend").replaceChildren();
  find("performance-summary").textContent="The selected calendar day does not exist in the Home Assistant installation timezone.";
  return;
 }
 // Recorded starts identify quarter-minute runs, not their unrecorded trigger cause.
 plottedRows = rows.filter(row => find("performance-extra").checked || new Date(row.started_at).getUTCMinutes() % 15 === 0);
 find("performance-summary").textContent = rows.length ? `${rows.length} completed calculations: Longest ${seconds(Math.max(...rows.map(row=>row.duration_seconds)))}.${plottedRows.length ? "" : " Enable additional calculations to view this day's recorded runs."}` : "No completed calculations were recorded for this day. History begins after this feature is installed.";
 const tooltip = {
  mode:"index", intersect:true,
  filter:item=>item.dataset.type!=="line" && Number.isFinite(item.parsed.y),
  callbacks:{
   title:items=>items.length ? when(items[0].parsed.x) + occurrence(items[0].parsed.x) : "",
   label:item=>`${item.dataset.label}: ${seconds(item.parsed.y)}`,
   afterBody:items=>{
    if (!items.length) return [];
    const row = plottedRows[items[0].dataIndex];
    return [`Total calculation: ${seconds(row.duration_seconds)}`, ...(row.first_candidate_seconds == null ? ["First candidate time not recorded."] : []), outcome(row)];
   },
  },
 };
 const series = [["First candidate observed","#0072b2",row=>row.first_candidate_seconds],["Further calculation","#b45f00",row=>row.first_candidate_seconds == null ? null : Math.max(0,row.duration_seconds-row.first_candidate_seconds)],["First time unknown","#73528d",row=>row.first_candidate_seconds == null ? row.duration_seconds : null]];
 chart = new Chart(find("performance-chart"),{plugins:[{id:"quarterWidths",afterDatasetsUpdate(chart){for(const meta of chart.getSortedVisibleDatasetMetas()){if(meta.type!=="bar")continue;for(const bar of meta.data)bar.width=Math.abs(chart.scales.x.getPixelForValue(minimum+QUARTER_MILLISECONDS)-chart.scales.x.getPixelForValue(minimum))*0.9;}}}],type:"bar",data:{datasets:series.map(([label,color,value])=>({label,backgroundColor:color,data:plottedRows.map(row=>({x:Date.parse(row.started_at),y:value(row)})),barPercentage:0.9,categoryPercentage:1}))},options:{responsive:true,maintainAspectRatio:false,animation:false,onClick:(_event,elements)=>{if(!loading && elements[0] && chart.data.datasets[elements[0].datasetIndex].type!=="line"){find("performance-selection").dataset.calculationId=plottedRows[elements[0].index].id;renderSelection();detail(plottedRows[elements[0].index]);}},scales:{x:{type:"linear",offset:false,min:minimum,max:maximum,stacked:true,afterBuildTicks:scale=>{const stride=Math.ceil((maximum-minimum)/QUARTER_MILLISECONDS/(MAX_TIME_LABELS-1))*QUARTER_MILLISECONDS;scale.ticks=[];for(let time=minimum;time<=maximum;time+=stride)scale.ticks.push({value:time});},ticks:{maxTicksLimit:MAX_TIME_LABELS,maxRotation:0,minRotation:0,callback:value=>when(value,{hour:"2-digit",minute:"2-digit",hourCycle:"h23"})}},y:{stacked:true,min:0,title:{display:true,text:"Seconds"}}},plugins:{legend:{display:false},tooltip}}});
 const budgets = [...new Set(rows.map(row=>row.time_limit_seconds).filter(Number.isFinite))];
 if (budgets.length === 1) {
  const budget = budgets[0];
  chart.data.datasets.push({type:"line",label:`Solver budget: ${seconds(budget)}`,data:[{x:minimum,y:budget},{x:maximum,y:budget}],borderColor:"#6b7785",borderWidth:1,borderDash:[4,4],pointRadius:0,stack:"budget"});
  chart.update();
 }
 const legend = find("performance-legend"); legend.replaceChildren();
 chart.data.datasets.forEach((dataset,index)=>{
  const label=document.createElement("label"), input=document.createElement("input"), swatch=document.createElement("span");
  input.type="checkbox"; input.checked=true;
  input.addEventListener("change",()=>{chart.setDatasetVisibility(index,input.checked);chart.update();});
  swatch.className="series-swatch"; swatch.style.backgroundColor=dataset.backgroundColor || dataset.borderColor; swatch.setAttribute("aria-hidden","true");
  label.append(input,swatch,document.createTextNode(dataset.label));legend.append(label);
 });
 renderSelection();
}
/** Ignore late responses after a day change. Failed reads clear stale data rather than misdate it. */
async function load() {
 const version = ++requestVersion; loading = true; find("performance-selection").disabled = true; find("performance-open").disabled = true; find("performance-status").textContent="Loading calculation history.";
 try {const selected=find("performance-day").value; const data=await api(`v1/planning-performance${selected ? `?day=${encodeURIComponent(selected)}` : ""}`); if(version!==requestVersion)return; useHomeAssistantTimezone(data.timezone); find("performance-day").value=data.day; rows=data.calculations; loading = false; render(); find("performance-status").textContent="";
 const address=new URL(location.href), selectedId=address.searchParams.get("calculation"); if(selectedId){address.searchParams.delete("calculation");history.replaceState(null,"",address);const selectedRow=rows.find(row=>row.id===selectedId);if(selectedRow&&!find("calculation-detail").open)detail(selectedRow);}
 } catch(error){if(version!==requestVersion)return; rows=[]; loading = false; render(); find("performance-summary").textContent="Calculation history is unavailable for the selected day."; find("performance-status").textContent=error.message;}
}
find("performance-selection").addEventListener("input", showSelection);
find("performance-open").addEventListener("click", openSelection);
find("performance-chart").addEventListener("focus", showSelection);
find("performance-chart").addEventListener("keydown", event => {
 if (loading || !plottedRows.length) return;
 const selection = find("performance-selection");
 if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
  event.preventDefault();
  selection.value = event.key === "Home" ? 0 : event.key === "End" ? plottedRows.length - 1 : Math.max(0, Math.min(plottedRows.length - 1, selection.valueAsNumber + (event.key === "ArrowRight" ? 1 : -1)));
  showSelection();
 } else if (["Enter", " "].includes(event.key)) {event.preventDefault(); openSelection();}
});
find("calculation-detail").addEventListener("close",()=>{detailVersion++;detailChart?.destroy();detailChart=null;});
find("calculation-close").addEventListener("click",()=>find("calculation-detail").close());
find("performance-extra").addEventListener("change",render);
find("performance-day").addEventListener("change",load);
find("performance-refresh").addEventListener("click",load);
find("performance-today").addEventListener("click",()=>{find("performance-day").value=localDate();load();});
find("connect").addEventListener("submit",event=>{event.preventDefault();token=find("token").value;find("token").value="";find("connect").hidden=true;load();});
api("setup/session").then(session=>{useHomeAssistantTimezone(session.timezone);find("performance-day").value=new URLSearchParams(location.search).get("day")||localDate();find("connect").hidden=Boolean(session.ingress);if(session.ingress)load();else find("performance-status").textContent="Connect to read calculation history.";}).catch(error=>{find("performance-status").textContent=error.message;});
