import {money, when, optionLabel} from "./display.js";

/** Associate current readiness and selection with stable forecast IDs, never historical names. */
export function currentModelRows(current = {}, selection = {}) {
    const rows = new Map();
    if (current.household) rows.set("household", {id: "household", scope: "household", label: "Household consumption", active: current.household.model, readiness: current.household.status, issue: current.household.issue});
    const solar = current.solar || {}, consumers = current.consumers || {};
    for (const [index, source] of (solar.configuration?.sources || []).entries()) {
        const choice = solar.source_selection?.[source.source_id];
        const readiness = choice ? choice.issue ? "comparison_unavailable" : choice.requested === "automatic" && choice.complete_days < choice.required_days ? "collecting_accuracy_evidence" : "active" : solar.calibration_status?.[source.source_id];
        rows.set(source.source_id, {id: source.source_id, scope: "solar", label: solar.source_names?.[source.source_id] || `Solar ${index + 1}`, active: solar.source_model_labels?.[source.source_id] || solar.source_models?.[source.source_id], readiness, solarSelection: choice, solarLearning: solar.calibration_status?.[source.source_id], issue: solar.history_issues?.[source.entity_id] || solar.issue});
    }
    for (const device of consumers.result?.devices || []) rows.set(device.entity_id, {id: device.entity_id, scope: "devices", label: consumers.options?.find(row => row.entity_id === device.entity_id)?.name || device.entity_id, active: device.forecast.model, readiness: device.status ?? consumers.result.status, temperature: device.weather_features_used});
    for (const selected of consumers.selected || []) if (!rows.has(selected.entity_id)) rows.set(selected.entity_id, {id: selected.entity_id, scope: "devices", label: consumers.options?.find(row => row.entity_id === selected.entity_id)?.name || selected.entity_id, readiness: consumers.result?.status || "waiting_for_history", issue: consumers.history_issues?.[selected.entity_id] || consumers.result?.detail || consumers.issue || "Waiting for usable historical evidence"});
    for (const model of selection.models || []) {
        const id = model.scope === "demand" ? "household" : model.scope?.startsWith("solar:") ? model.scope.slice("solar:".length) : null;
        if (!id) continue;
        rows.set(id, {...rows.get(id), id, scope: model.scope === "demand" ? "household" : "solar", label: model.scope?.startsWith("solar:") ? rows.get(id)?.label || model.label : model.label, active: model.active_model_label || model.active_model, selection: model});
    }
    return rows;
}

/** Append text-only current evidence, explicitly separate from the chart's frozen midnight model. */
export function renderCurrentModel(parent, row, expanded = false) {
    const details = document.createElement("details"), summary = document.createElement("summary");
    details.className = "quality-current-model"; details.dataset.modelSource = row.id; details.open = expanded;
    summary.textContent = "Current model and learning"; details.append(summary);
    const paragraph = value => { const node = document.createElement("p"); node.textContent = value; details.append(node); };
    paragraph("Current status observed now. The graph retains the original model and forecast fixed at the selected day's midnight.");
    const model = row.selection;
    const statuses = {training_history_required: "Collecting training history", learned_preferred: "Learned model has consistent decision benefit", collecting_decision_evidence: "Collecting realized decision evidence", no_consistent_decision_benefit: "Baseline retained: No consistent decision benefit"};
    const fields = document.createElement("dl"); fields.className = "model-selection-summary";
    const values = [["Active forecast model", row.active || "Waiting for forecast"], ["Readiness", row.readiness ? optionLabel(row.readiness) : "Not known"]];
    if (model) values.push(["Selection preference", model.preferred === "learned" ? "Learned" : model.preferred === "baseline" ? "Baseline" : "Not known"], ["Completed comparisons", model.completed_windows == null || model.required_windows == null ? "Not known" : `${model.completed_windows} of ${model.required_windows} required`]);
    if (row.temperature != null) values.push(["Temperature features", row.temperature ? "Used" : "Not used"]);
    for (const [label, value] of values) {
        const field = document.createElement("div"), term = document.createElement("dt"), description = document.createElement("dd");
        term.textContent = label; description.textContent = value; field.append(term, description); fields.append(field);
    }
    details.append(fields);
    if (model) paragraph(statuses[model.status] || "Waiting for model status");
    if (row.solarSelection) {
        const solar = row.solarSelection;
        const names = {automatic: "Automatic", pvwatts: "PVWatts", ha: "Home Assistant", ml: "ML model"};
        paragraph(`Forecast source: ${names[solar.requested]}: Active: ${names[solar.active]}`);
        paragraph(`Accuracy comparison: ${solar.complete_days} of ${solar.required_days} complete days. Candidates use the same previously forecast hours. Lower mean absolute error is better.`);
        for (const [source, error] of Object.entries(solar.mae_kwh || {})) paragraph(`${names[source]}: Mean absolute error: ${error.toLocaleString(undefined, {maximumFractionDigits: 3})} kWh per hour`);
        if (solar.issue) paragraph(solar.issue);
        if (row.solarLearning) paragraph(`ML learning: ${optionLabel(row.solarLearning)}`);
    }
    for (const issue of new Set([row.issue, model?.issue].filter(Boolean))) paragraph(issue === "Available PV needs uncurtailed, independent production evidence" ? "Model comparison needs independent evidence of available solar production, including production prevented by curtailment." : issue);
    for (const [label, value] of [["Pending comparison ends", model?.pending_until], ["Last evaluated", model?.evaluated_at]]) if (value) paragraph(`${label}: ${when(value)}`);
    for (const window of model?.windows || []) paragraph(`${when(window.start)} to ${when(window.end)}: Simulated baseline cost: ${money(window.baseline.economic_cost)} ${model.currency}: Candidate: ${money(window.candidate.economic_cost)} ${model.currency}: Baseline grid import: ${window.baseline.corrected_import_kwh.toLocaleString()} kWh: Candidate: ${window.candidate.corrected_import_kwh.toLocaleString()} kWh, including the existing terminal-stock correction. These are simulated decisions, not measured savings.`);
    parent.append(details);
}
