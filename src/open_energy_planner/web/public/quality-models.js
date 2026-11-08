import {money, when, optionLabel, percentage} from "./display.js";

/** Associate current readiness and selection with stable forecast IDs, never historical names. */
export function currentModelRows(current = {}, selection = {}) {
    const rows = new Map();
    if (current.household) rows.set("household", {id: "household", scope: "household", label: "Household consumption", active: current.household.model, readiness: current.household.status, issue: current.household.issue});
    const solar = current.solar || {}, consumers = current.consumers || {};
    for (const [index, source] of (solar.configuration?.sources || []).entries()) {
        const choice = solar.source_selection?.[source.source_id], training = solar.learning?.[source.source_id];
        const collecting = choice && choice.complete_days < choice.required_days && (choice.requested === "automatic" || choice.requested === "ml" && training?.status === "nonlinear_shadow");
        const readiness = choice ? collecting ? "collecting_accuracy_evidence" : choice.issue ? "comparison_unavailable" : "active" : solar.calibration_status?.[source.source_id];
        rows.set(source.source_id, {id: source.source_id, scope: "solar", label: solar.source_names?.[source.source_id] || `Solar ${index + 1}`, active: solar.source_model_labels?.[source.source_id] || solar.source_models?.[source.source_id], readiness, solarSelection: choice, solarLearning: solar.calibration_status?.[source.source_id], solarTraining: solar.learning?.[source.source_id], issue: solar.history_issues?.[source.entity_id] || solar.issue});
    }
    for (const device of consumers.result?.devices || []) rows.set(device.entity_id, {id: device.entity_id, scope: "devices", label: consumers.options?.find(row => row.entity_id === device.entity_id)?.name || device.entity_id, active: device.forecast.model, readiness: device.status ?? consumers.result.status, temperature: device.weather_features_used, weatherLearning: device.weather_learning});
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
    const paragraph = (value, parent = details) => { const node = document.createElement("p"); node.textContent = value; parent.append(node); };
    const comparison = title => {
        const section = document.createElement("section"), heading = document.createElement("h4");
        section.className = "quality-comparison"; section.setAttribute("aria-label", title);
        heading.textContent = title; section.append(heading); details.append(section); return section;
    };
    const errors = (parent, rows) => {
        const table = document.createElement("table"), head = document.createElement("thead"), header = document.createElement("tr"), body = document.createElement("tbody");
        table.className = "quality-model-errors";
        for (const label of ["Prediction", "MAE (kWh/hour)"]) { const cell = document.createElement("th"); cell.scope = "col"; cell.textContent = label; header.append(cell); }
        head.append(header); table.append(head);
        for (const [label, error] of rows) {
            const line = document.createElement("tr"), name = document.createElement("th"), value = document.createElement("td");
            name.scope = "row"; name.textContent = label;
            value.textContent = error == null ? "Not yet available" : error.toLocaleString(undefined, {minimumFractionDigits: 3, maximumFractionDigits: 3});
            line.append(name, value); body.append(line);
        }
        table.append(body); parent.append(table);
    };
    paragraph("Current status observed now. The graph retains the original model and forecast fixed at the selected day's midnight.");
    const model = row.selection;
    const statuses = {training_history_required: "Collecting training history", learned_preferred: "Learned model has consistent decision benefit", collecting_decision_evidence: "Collecting realized decision evidence", no_consistent_decision_benefit: "Baseline retained: No consistent decision benefit"};
    const fields = document.createElement("dl"); fields.className = "model-selection-summary";
    const values = [[row.scope === "devices" ? "Component forecast model" : "Active forecast model", row.active || "Waiting for forecast"], ["Readiness", row.readiness ? optionLabel(row.readiness) : "Not known"]];
    if (model) values.push(["Selection preference", model.preferred === "learned" ? "Learned" : model.preferred === "baseline" ? "Baseline" : "Not known"], ["Completed comparisons", model.completed_windows == null || model.required_windows == null ? "Not known" : `${model.completed_windows} of ${model.required_windows} required`]);
    if (row.temperature != null) values.push(["Temperature features", row.temperature ? "Used" : "Not used"]);
    for (const [label, value] of values) {
        const field = document.createElement("div"), term = document.createElement("dt"), description = document.createElement("dd");
        term.textContent = label; description.textContent = value; field.append(term, description); fields.append(field);
    }
    details.append(fields);
    if (row.weatherLearning) {
        const learning = row.weatherLearning;
        const reasons = {training_history_required: "Collecting at least 28 days of complete device and temperature history.", model_runtime_unavailable: "The candidate model is unavailable. The simpler forecast remains in use.", weather_forecast_unavailable: "The candidate needs a complete temperature forecast within its observed training range. The simpler forecast remains in use.", forward_archive_required: "Waiting for saved forward comparisons.", comparison_evidence_unreadable: "Saved comparison evidence is unavailable. The simpler forecast remains in use.", collecting_forward_accuracy: "Collecting later measurements for predictions saved before the measured day.", no_consistent_accuracy_benefit: "The simpler model is retained because the candidate did not consistently improve accuracy.", accuracy_validated: "The nonlinear candidate improved all required forward comparisons. Household activation still requires the existing decision comparison."};
        paragraph(`Weather candidate: XGBoost CPU: ${learning.completed_windows} of ${learning.required_windows} complete forward days.`);
        paragraph(reasons[learning.status] || "Waiting for candidate evidence.");
        if (learning.training_quality) {
            const quality = learning.training_quality;
            paragraph(`Training coverage: ${quality.used_hours} of ${quality.source_hours} measured hours have complete inputs.`);
            const exclusions = {previous_day_incomplete: "A complete preceding day of measurements is unavailable", temperature_unavailable: "Temperature was unavailable at training time"};
            for (const period of quality.excluded_periods || []) paragraph(`Excluded training hours: ${when(period.start)} to ${when(period.end)}: ${exclusions[period.reason] || "Required input is unavailable"}.`);
        }
        paragraph("Each comparison uses the same measured hours for the candidate, Ridge or the current simpler model, and the weekday profile. Missing measurements remain unknown. Cold spells and defrost peaks are not trimmed.");
        paragraph(`The candidate must improve each simpler model by more than ${percentage(learning.required_relative_benefit_percent)} and ${learning.required_absolute_benefit_kwh?.toLocaleString(undefined, {maximumFractionDigits: 3}) ?? "Unknown"} kWh per hour in every required day.`);
        if (learning.pending_until) paragraph(`Pending measured day ends: ${when(learning.pending_until)}`);
        if (learning.excluded_window) paragraph(`Excluded comparison: ${when(learning.excluded_window.start)}: Complete measured hours are unavailable.`);
        for (const window of learning.windows || []) paragraph(`${when(window.start)} to ${when(window.end)}: Mean absolute error per hour: Candidate ${window.mae_kwh.candidate.toLocaleString(undefined, {maximumFractionDigits: 3})} kWh: Simpler model ${window.mae_kwh.simpler.toLocaleString(undefined, {maximumFractionDigits: 3})} kWh: Weekday profile ${window.mae_kwh.profile.toLocaleString(undefined, {maximumFractionDigits: 3})} kWh.`);
        paragraph("This predicts consumption. Home Assistant retains control of operating mode, comfort and safety.");
    }
    if (model) paragraph(statuses[model.status] || "Waiting for model status");
    if (row.solarSelection) {
        const solar = row.solarSelection;
        const names = {automatic: "Automatic", pvwatts: "PVWatts", ha: "Home Assistant", ml: "ML model"};
        const complete = comparison("Complete forecast comparison");
        paragraph(`Forecast source: ${names[solar.requested]}: Active: ${names[solar.active]}`, complete);
        paragraph(`${solar.complete_days} of ${solar.required_days} complete days. Forecasts saved before the measured day are compared on the same actual production hours. Lower mean absolute error is better.`, complete);
        paragraph("PVWatts and XGBoost use the same captured Open-Meteo weather. The Home Assistant provider uses its own weather. This comparison includes the weather source and determines Automatic selection.", complete);
        if (Object.keys(solar.mae_kwh || {}).length) errors(complete, Object.entries(solar.mae_kwh).map(([source, error]) => [source === "ml" ? "XGBoost" : names[source], error]));
        else paragraph("Scores appear after the required comparable days are complete.", complete);
        if (solar.issue) paragraph(solar.issue, complete);
        if (row.solarLearning && !row.solarTraining) paragraph(`ML learning: ${optionLabel(row.solarLearning)}`, complete);
    }
    if (row.solarTraining) {
        const training = row.solarTraining;
        const controlled = comparison("Model comparison");
        const names = {nonlinear_shadow: row.solarSelection?.active === "ml" ? "XGBoost is active" : "XGBoost is learning in shadow mode", baseline_preferred: "PVWatts retained after historical validation", linear_preferred: "Simpler correction retained", insufficient_clean_history: "Collecting usable training history", insufficient_validation_history: "Collecting usable historical validation", training_failed: "Training temporarily unavailable", blocked: "Independent production evidence required"};
        paragraph(`Solar learning: ${names[training.status] || "Waiting for usable history"}`, controlled);
        paragraph("XGBoost predicts production directly from weather, sun position and time. It does not multiply or depend on a PVWatts prediction.", controlled);
        paragraph("Historical validation compares PVWatts and XGBoost using the same weather and measured daylight hours from a later period held out of training. It compares the models under equal weather inputs, not the weather source's accuracy.", controlled);
        const trainingDays = training.training_days == null ? "" : ` across ${training.training_days} days`, validationDays = training.validation_days == null ? "" : ` across ${training.validation_days} days`;
        paragraph(`Training: ${training.training_hours} daylight hours${trainingDays}. Historical validation: ${training.validation_hours} daylight hours${validationDays}.`, controlled);
        if (training.validation_start && training.validation_end) paragraph(`Validation period: ${when(training.validation_start)} to ${when(training.validation_end)}`, controlled);
        errors(controlled, [["PVWatts", training.baseline_mae_kwh], ["XGBoost", training.tree_mae_kwh]]);
        paragraph("Reliable daylight measurements remain included at zero, low or unexpectedly high production. Original measurements are retained.", controlled);
        for (const [reason, count] of Object.entries(training.excluded_counts || {})) paragraph(`Excluded from learning: ${optionLabel(reason)}: ${count} ${count === 1 ? "hour" : "hours"}.`, controlled);
        for (const [reason, count] of Object.entries(training.diagnostic_counts || {})) paragraph(`Marked for review, still included: ${optionLabel(reason)}: ${count} ${count === 1 ? "hour" : "hours"}. A statistical difference does not prove a meter fault or curtailment.`, controlled);
        if (training.status === "nonlinear_shadow" && row.solarSelection?.active !== "ml") paragraph("Historical training alone does not activate it. XGBoost must also improve forecasts saved before later measurements in the complete forecast comparison.", controlled);
        if (training.issue) paragraph(training.issue, controlled);
    }
    for (const issue of new Set([row.issue, model?.issue].filter(Boolean))) paragraph(issue === "Available PV needs uncurtailed, independent production evidence" ? "Model comparison needs independent evidence of available solar production, including production prevented by curtailment." : issue);
    for (const [label, value] of [["Pending comparison ends", model?.pending_until], ["Last evaluated", model?.evaluated_at]]) if (value) paragraph(`${label}: ${when(value)}`);
    for (const window of model?.windows || []) paragraph(`${when(window.start)} to ${when(window.end)}: Simulated baseline cost: ${money(window.baseline.economic_cost)} ${model.currency}: Candidate: ${money(window.candidate.economic_cost)} ${model.currency}: Baseline grid import: ${window.baseline.corrected_import_kwh.toLocaleString()} kWh: Candidate: ${window.candidate.corrected_import_kwh.toLocaleString()} kWh, including the existing terminal-stock correction. These are simulated decisions, not measured savings.`);
    parent.append(details);
}
