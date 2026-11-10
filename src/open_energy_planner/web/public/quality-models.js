import {money, when, dateLabel, optionLabel, percentage} from "./display.js";

const HISTORY_IMPORT_MESSAGES = {
    recorder_unavailable: "Home Assistant Recorder is unavailable. Check Recorder; the next background history check retries.",
    history_unavailable: "Home Assistant production history is not available yet. The next background history check retries.",
    unsupported_statistics: "The production meter does not provide supported energy statistics. Check its Home Assistant energy settings.",
    history_continuity_unavailable: "The production meter identity or accepted readings changed. Earlier history is retained. Restore the original meter history or choose Learning data from for a new production regime.",
    history_cache_unreadable: "Saved production history cannot be read. Check the saved database and restore a working backup if needed.",
};

/** Associate current readiness and selection with stable forecast IDs, never historical names. */
export function currentModelRows(current = {}, selection = {}) {
    const rows = new Map();
    if (current.household) rows.set("household", {id: "household", scope: "household", label: "Household consumption", active: current.household.model, readiness: current.household.status, issue: current.household.issue, dailySelection: current.household.model_selection});
    const solar = current.solar || {}, consumers = current.consumers || {};
    for (const [index, source] of (solar.configuration?.sources || []).entries()) {
        const choice = solar.source_selection?.[source.source_id], training = solar.learning?.[source.source_id];
        const collecting = choice && choice.complete_days < choice.required_days && (choice.requested === "automatic" || choice.requested === "ml" && training?.status === "nonlinear_shadow");
        const readiness = choice ? collecting ? "collecting_accuracy_evidence" : choice.issue ? "comparison_unavailable" : "active" : solar.calibration_status?.[source.source_id];
        rows.set(source.source_id, {id: source.source_id, scope: "solar", label: solar.source_names?.[source.source_id] || `Solar ${index + 1}`, active: solar.source_model_labels?.[source.source_id] || solar.source_models?.[source.source_id], readiness, solarSelection: choice, solarLearning: solar.calibration_status?.[source.source_id], solarTraining: solar.learning?.[source.source_id], solarWeatherService: solar.weather_service, solarHistory: solar.history_import?.[source.entity_id], solarLearningStart: source.learning_start, issue: solar.history_issues?.[source.entity_id] || solar.feedback_issues?.[source.curtailment_entity_id] || solar.issue});
    }
    for (const device of consumers.result?.devices || []) rows.set(device.entity_id, {id: device.entity_id, scope: "devices", label: consumers.options?.find(row => row.entity_id === device.entity_id)?.name || device.entity_id, active: device.forecast.model, readiness: device.status ?? consumers.result.status, temperature: device.weather_features_used, weatherLearning: device.weather_learning, dailySelection: device.model_selection});
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
    if (row.solarLearningStart) values.push(["Learning data from", when(row.solarLearningStart)]);
    if (row.solarHistory) {
        const history = row.solarHistory;
        values.push(["Recorder availability", history.available_from_month ? `From month ${history.available_from_month.slice(0, 7)}` : "Not yet discovered"], ["Imported years", history.loaded_years?.length ? history.loaded_years.join(", ") : "None yet"], ["Received cumulative readings", history.hours == null ? "Not known" : history.hours.toLocaleString()], ["Import range ends", history.loaded_through ? `${when(history.loaded_through)} (exclusive)` : "Not yet received"]);
    }
    if (model) values.push(["Selection preference", model.preferred === "learned" ? "Learned" : model.preferred === "baseline" ? "Baseline" : "Not known"], ["Completed comparisons", model.completed_windows == null || model.required_windows == null ? "Not known" : `${model.completed_windows} of ${model.required_windows} required`]);
    if (row.temperature != null) values.push(["Temperature features", row.temperature ? "Used" : "Not used"]);
    for (const [label, value] of values) {
        const field = document.createElement("div"), term = document.createElement("dt"), description = document.createElement("dd");
        term.textContent = label; description.textContent = value; field.append(term, description); fields.append(field);
    }
    details.append(fields);
    if (row.solarHistory) {
        paragraph("Recorder availability is month-level evidence, not the first complete measured hour. Missing production or weather hours remain unknown.");
        if (row.solarHistory.issue) paragraph(Object.hasOwn(HISTORY_IMPORT_MESSAGES, row.solarHistory.issue) ? HISTORY_IMPORT_MESSAGES[row.solarHistory.issue] : "History import is temporarily unavailable. Last accepted history is retained.");
    }
    if (row.solarWeatherService) {
        const weather = row.solarWeatherService, service = comparison("Current forecast weather");
        paragraph(`Weather model: ${weather.model === "ecmwf_ifs" ? "ECMWF IFS" : optionLabel(weather.model)}. Selection: ${weather.selection === "latest_available" ? "Newest available forecast run" : optionLabel(weather.selection)}.`, service);
        paragraph(`Run initialized: ${weather.initialized_at ? when(weather.initialized_at) : "Not supplied"}. Actual receipt: ${when(weather.received_at)}.`, service);
        paragraph(`Provider publication: ${weather.available_at ? when(weather.available_at) : "Not known"}. Initialization and receipt do not establish a publication time.`, service);
    }
    if (row.dailySelection) {
        const learning = row.dailySelection;
        paragraph(`Daily accuracy: ${learning.completed_windows} of ${learning.required_windows} complete local days. Selection: ${optionLabel(learning.status)}.`);
        paragraph("Original forecasts are frozen before midnight in the Home Assistant timezone. Models use the same measured hours. Missing measurements do not count as zero consumption. A simpler profile remains active until another model consistently improves accuracy.");
        paragraph(`Each compared day must improve the baseline by more than ${percentage(learning.required_relative_benefit_percent)} and ${learning.required_absolute_benefit_kwh?.toLocaleString(undefined, {maximumFractionDigits: 3}) ?? "Unknown"} kWh per hour.`);
        if (learning.pending_until) paragraph(`Pending measured day ends: ${when(learning.pending_until)}`);
        for (const window of learning.windows || []) {
            const errors = Object.entries(window.mae_kwh).map(([model, value]) => `${optionLabel(model)}: ${value.toLocaleString(undefined, {maximumFractionDigits: 4})} kWh`).join(", ");
            paragraph(`${dateLabel(window.day)}: Mean absolute hourly error: ${errors}.`);
            const totals = Object.entries(window.daily_energy_error_kwh || {}).map(([model, value]) => `${optionLabel(model)}: ${value.toLocaleString(undefined, {maximumFractionDigits: 4})} kWh`).join(", ");
            if (totals) paragraph(`${dateLabel(window.day)}: Absolute daily energy error: ${totals}.`);
        }
        if (learning.excluded_reason === "controlled_use") paragraph("Days with planned device operation are excluded from spontaneous-use comparisons. A planned shift does not prove a behavior forecast was wrong.");
        if (learning.excluded_reason === "control_history_unavailable") paragraph("Published control history is unavailable. The simpler model remains active until intervention evidence can be checked.");
        if (learning.training_quality) {
            const quality = learning.training_quality;
            paragraph(`Weather candidate training coverage: ${quality.used_hours} of ${quality.source_hours} measured hours have complete inputs.`);
            for (const period of quality.excluded_periods || []) paragraph(`Excluded training hours: ${when(period.start)} to ${when(period.end)}: ${optionLabel(period.reason)}.`);
        }
    }
    if (row.weatherLearning) {
        const learning = row.weatherLearning;
        const reasons = {training_history_required: "Collecting at least 28 days of complete device and temperature history.", model_runtime_unavailable: "The candidate model is unavailable. The simpler forecast remains in use.", weather_forecast_unavailable: "The candidate needs a complete temperature forecast within its observed training range. The simpler forecast remains in use.", forward_archive_required: "Waiting for saved forward comparisons.", comparison_evidence_unreadable: "Saved comparison evidence is unavailable. The simpler forecast remains in use.", collecting_forward_accuracy: "Collecting later measurements for predictions saved before the measured day.", no_consistent_accuracy_benefit: "The simpler model is retained because the candidate did not consistently improve accuracy.", accuracy_validated: "The nonlinear candidate improved all required forward comparisons. Household activation still requires the existing decision comparison."};
        paragraph(`Weather candidate: Machine learning: ${learning.completed_windows} of ${learning.required_windows} complete forward days.`);
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
        const names = {automatic: "Automatic", pvwatts: "PVWatts", ha: "Home Assistant", ml: "Machine learning"};
        const complete = comparison("Complete forecast comparison");
        paragraph(`Forecast source: ${names[solar.requested]}: Active: ${names[solar.active]}`, complete);
        if (solar.admission_basis === "historical_forecast_backtest") paragraph("Machine learning passed the historical forecast assessment. Home Assistant forecasts retain their separate captured comparison.", complete);
        else paragraph("Forecasts saved before the measured day are compared on the same actual production hours. Historical forecast assessment can admit machine learning without waiting for new daily comparisons. Lower mean absolute error is better.", complete);
        paragraph("Captured comparisons use real forecasts saved before their outcomes. PVWatts and Machine learning use the same captured ECMWF IFS weather forecast. The Home Assistant provider uses its own weather. This comparison includes the weather source and determines Automatic selection when a common captured cohort is available.", complete);
        if (solar.admission_basis === "historical_forecast_backtest") paragraph("Historical admission scores are shown in the historical forecast backtest below. They are not real captured forecast scores.", complete);
        else if (Object.keys(solar.mae_kwh || {}).length) errors(complete, Object.entries(solar.mae_kwh).map(([source, error]) => [source === "ml" ? "Machine learning" : names[source], error]));
        else paragraph("Captured comparison scores are unavailable. Check the historical assessment and usable production history below.", complete);
        if (solar.issue) paragraph(solar.issue, complete);
        if (row.solarLearning && !row.solarTraining) paragraph(`ML learning: ${optionLabel(row.solarLearning)}`, complete);
    }
    if (row.solarTraining) {
        const training = row.solarTraining;
        if (training.historical_assessment) {
            const assessment = training.historical_assessment, historical = comparison("Historical forecast backtest");
            const states = {eligible: "Accuracy requirements passed", insufficient_history: "More complete forecast and measurement history is needed", not_improved: "PVWatts retained: Accuracy did not improve enough", invalid_evidence: "Historical forecast evidence is unavailable"};
            paragraph(states[assessment.status] || "Waiting for historical forecast evidence", historical);
            paragraph(`Archived weather: ${assessment.weather_model === "ecmwf_ifs" ? "ECMWF IFS" : optionLabel(assessment.weather_model)}. ${assessment.run_initializations?.length || 0} predetermined weekly runs; two simulated 48-hour plans per run.`, historical);
            paragraph(`${assessment.matched_pairs} forecast/measurement pairs across ${assessment.unique_hours} unique production hours. The plans overlap; pairs are not independent measured hours.`, historical);
            paragraph("Origins at initialization and 24 hours later are simulations. They do not claim that a forecast was actually published then. Historical publication time is not known.", historical);
            for (const origin of assessment.per_origin || []) {
                paragraph(`Planning origin: ${origin.origin_delay_hours} hours after initialization. ${origin.matched_hours} matched forecast/measurement pairs. Mean absolute error is in kWh per hour; both the model and served forecast must improve on PVWatts.`, historical);
                errors(historical, [["PVWatts", origin.baseline_mae_kwh], ["Machine learning", origin.model_mae_kwh], ["Served forecast", origin.service_mae_kwh]]);
            }
            if (assessment.windows?.length) {
                const windows = document.createElement("details"), label = document.createElement("summary");
                label.textContent = `Simulated planning windows: ${assessment.windows.length}`; windows.append(label);
                for (const window of assessment.windows) paragraph(`Run initialized: ${when(window.initialized_at)}. Simulated plan: ${when(window.origin)} to ${when(window.end)} (end exclusive). ${window.matched_hours} matched hours; provider lead starts at initialization, planning lead starts at the simulated origin.`, windows);
                historical.append(windows);
            }
            if (assessment.created_at) paragraph(`Assessment created: ${when(assessment.created_at)}. This actual creation time is not backdated.`, historical);
            if (assessment.issue) paragraph(assessment.issue, historical);
        }
        const controlled = comparison("Model comparison");
        const names = {nonlinear_shadow: row.solarSelection?.active === "ml" ? "Machine learning is active" : "Machine learning is learning in shadow mode", baseline_preferred: "PVWatts retained after historical validation", linear_preferred: "Simpler correction retained", insufficient_clean_history: "Collecting usable training history", insufficient_validation_history: "Collecting usable historical validation", training_failed: "Training temporarily unavailable", blocked: "Independent production evidence required"};
        paragraph(`Solar learning: ${names[training.status] || "Waiting for usable history"}`, controlled);
        paragraph("Machine learning predicts production directly from weather, sun position and time. It does not multiply or depend on a PVWatts prediction.", controlled);
        paragraph("Historical validation compares PVWatts and Machine learning using the same archived weather forecasts and measured daylight hours from a later period held out of training. Reanalysis is not used for training or forecast admission. Equal weather inputs compare the models, not the weather source's own accuracy.", controlled);
        const trainingDays = training.training_days == null ? "" : ` across ${training.training_days} days`, validationDays = training.validation_days == null ? "" : ` across ${training.validation_days} days`;
        paragraph(`Training: ${training.training_hours} daylight hours${trainingDays}. Historical validation: ${training.validation_hours} daylight hours${validationDays}.`, controlled);
        if (training.training_samples != null) paragraph(`Training samples: ${training.training_samples} forecast/measurement pairs across ${training.training_hours} unique production hours. Forecasts sharing an actual hour share that hour's total weight.`, controlled);
        if (training.training_start && training.training_end) paragraph(`Training period: ${when(training.training_start)} to ${when(training.training_end)} (end exclusive).`, controlled);
        if (training.validation_start && training.validation_end) paragraph(`Validation period: ${when(training.validation_start)} to ${when(training.validation_end)}`, controlled);
        paragraph("Training and validation measurements all count equally. No fixed age discount is applied.", controlled);
        if (training.time_trend) {
            const trend = training.time_trend;
            const states = {active: "Learned trend selected automatically for the candidate", insufficient_history: "No trend selected: More comparable seasonal history is needed", not_improved: "No trend selected: Seasonal comparisons did not improve enough", unstable: "No trend selected: The estimate did not meet the stability or safety checks"};
            paragraph(`Time trend: ${states[trend.status] || "Waiting for trend evidence"}`, controlled);
            if (trend.status === "active" && trend.annual_change != null) paragraph(`Selected change in yield: ${trend.annual_change > 0 ? "+" : ""}${percentage(trend.annual_change * 100)} per year.`, controlled);
            if (trend.status !== "active" && trend.candidate_annual_change != null) paragraph(`Tested change in yield: ${trend.candidate_annual_change > 0 ? "+" : ""}${percentage(trend.candidate_annual_change * 100)} per year. This trend is not applied.`, controlled);
            if (trend.reference) paragraph(`Trend reference: ${when(trend.reference)}`, controlled);
            if (trend.reason) paragraph(trend.reason, controlled);
            paragraph("A learned change in yield can include shading, soiling and weather bias. It does not establish physical panel degradation.", controlled);
            if (trend.folds?.length) {
                const backtests = document.createElement("details"), heading = document.createElement("summary");
                heading.textContent = `Seasonal time-trend comparisons: ${trend.folds.length}`; backtests.append(heading);
                for (const fold of trend.folds) paragraph(`Training ends: ${when(fold.training_end)}. Validation: ${when(fold.validation_start)} to ${when(fold.validation_end)}. Mean absolute error: Without trend ${fold.baseline_mae_kwh.toLocaleString(undefined, {maximumFractionDigits: 3})} kWh/hour. With trend ${fold.trend_mae_kwh.toLocaleString(undefined, {maximumFractionDigits: 3})} kWh/hour. Tested annual change: ${percentage(fold.annual_change * 100)}.`, backtests);
                controlled.append(backtests);
            }
        }
        errors(controlled, [["PVWatts", training.baseline_mae_kwh], ["Machine learning", training.tree_mae_kwh]]);
        paragraph("Reliable daylight measurements remain included at zero, low or unexpectedly high production. Original measurements are retained.", controlled);
        for (const [reason, count] of Object.entries(training.excluded_counts || {})) paragraph(`Excluded from learning: ${optionLabel(reason)}: ${count} ${count === 1 ? "hour" : "hours"}.`, controlled);
        for (const [reason, count] of Object.entries(training.diagnostic_counts || {})) paragraph(`Marked for review, still included: ${optionLabel(reason)}: ${count} ${count === 1 ? "hour" : "hours"}. A statistical difference does not prove a meter fault or curtailment.`, controlled);
        if (training.status === "nonlinear_shadow" && row.solarSelection?.active !== "ml" && training.historical_assessment?.status !== "eligible") paragraph("Historical training alone does not activate it. Machine learning also needs a successful historical forecast backtest or complete forecasts captured before later measurements.", controlled);
        if (training.issue) paragraph(training.issue, controlled);
    }
    for (const issue of new Set([row.issue, model?.issue].filter(issue => issue && issue !== row.solarHistory?.issue))) paragraph(Object.hasOwn(HISTORY_IMPORT_MESSAGES, issue) ? HISTORY_IMPORT_MESSAGES[issue] : issue === "Available PV needs uncurtailed, independent production evidence" ? "Model comparison needs independent evidence of available solar production, including production prevented by curtailment." : issue);
    for (const [label, value] of [["Pending comparison ends", model?.pending_until], ["Last evaluated", model?.evaluated_at]]) if (value) paragraph(`${label}: ${when(value)}`);
    for (const window of model?.windows || []) paragraph(`${when(window.start)} to ${when(window.end)}: Simulated baseline cost: ${money(window.baseline.economic_cost)} ${model.currency}: Candidate: ${money(window.candidate.economic_cost)} ${model.currency}: Baseline grid import: ${window.baseline.corrected_import_kwh.toLocaleString()} kWh: Candidate: ${window.candidate.corrected_import_kwh.toLocaleString()} kWh, including the existing terminal-stock correction. These are simulated decisions, not measured savings.`);
    parent.append(details);
}
