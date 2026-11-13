"""Exercise shared axis defaults with the shipped Chart.js renderer."""

import math
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[3] / "src/open_energy_planner/web/public"


@pytest.mark.parametrize("width", [390, 1280])
def test_zero_reference_keeps_zero_and_ten_percent_negative_clearance(browser, width):
    """Positive prices start at zero. Signed prices retain zero and ten percent of axis height below."""
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        page = context.new_page()
        page.set_content('<canvas id="plot"></canvas>')
        page.add_script_tag(path=str(WEB / "chart.umd.min.js"))
        page.add_script_tag(path=str(WEB / "chart_theme.js"), type="module")
        page.wait_for_function("Chart.registry.plugins.get('valueAxisHeadroom')")
        results = page.evaluate("""() => [
            {values:[0.2,0.8], bounds:[0,0.8/0.9]},
            {values:[-0.2,0.4], bounds:[-0.275,0.475]},
            {values:[-0.4,-0.2], bounds:[-0.4/0.9,0]},
            {values:[5,5], bounds:[0,5/0.9]},
            {values:[-5,-5], bounds:[-5/0.9,0]},
            {values:[0.00002,0.00003], bounds:[0,0.00003/0.9]},
            {values:[0,0]}, {values:[null,null]},
        ].map(sample => {
            const chart=new Chart(document.getElementById('plot'), {type:'line',data:{datasets:[{data:sample.values}]},
                options:{responsive:false,animation:false,scales:{y:{beginAtZero:true}}}});
            const snapshot=()=>({bounds:[chart.scales.y.min,chart.scales.y.max],ticks:chart.scales.y.ticks.map(tick=>tick.value)});
            const initial=snapshot(); chart.resize(300,300); const resized=snapshot();
            chart.destroy(); return {sample,initial,resized};
        })""")
    for result in results:
        for snapshot in (result["initial"], result["resized"]):
            lower, upper = snapshot["bounds"]
            assert math.isfinite(lower) and math.isfinite(upper) and lower < upper
            assert lower <= 0 <= upper
            assert 0 in snapshot["ticks"]
            if "bounds" in result["sample"]:
                assert snapshot["bounds"] == pytest.approx(result["sample"]["bounds"])
            else:
                assert lower == 0


@pytest.mark.parametrize("width", [390, 1280])
def test_value_axes_pad_data_and_keep_nice_ticks_time_and_soc(browser, width):
    """Real line and stacked axes retain ten-percent clearance after toggles and resizes.

    Signed stacks reach -7 and +5, independently of their component extremes.
    Hidden series must stop influencing the range. Missing and constant data
    retain finite axes. Time bounds and the physical SoC domain remain fixed.
    """
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        page = context.new_page()
        page.set_content('<div style="height:400px"><canvas id="plot"></canvas></div>')
        page.add_script_tag(path=str(WEB / "chart.umd.min.js"))
        page.add_script_tag(path=str(WEB / "chart_theme.js"), type="module")
        page.wait_for_function("Chart.registry.plugins.get('valueAxisHeadroom')")
        results = page.evaluate("""() => {
            const cases = [
                {values: [-3, 2]}, {values: [-5, -4]}, {values: [4, 5]},
                {values: [0.00002, 0.00003]}, {values: [5, 5]},
                {values: [0, 0]}, {values: [null, null]},
                {values: [2, 3], baseline: 0, type: 'bar'},
                {values: [2, -3], other: [3, -4], baseline: 0, type: 'bar'},
            ];
            return cases.map(sample => {
                const datasets = [sample.values, ...(sample.other ? [sample.other] : [])]
                    .map(values => ({data: values.map((y, x) => ({x: x + 1, y})), stack: sample.other ? 'balance' : undefined}));
                datasets.push({data: [{x: 1, y: 100}, {x: 2, y: 0}], yAxisID: 'soc', type: 'line'});
                const chart = new Chart(document.getElementById('plot'), {
                    type: sample.type || 'line', data: {datasets},
                    options: {animation: false, responsive: false, parsing: false,
                        scales: {x: {type: 'linear', min: 0, max: 3},
                            y: {beginAtZero: false, suggestedMin: sample.baseline, stacked: Boolean(sample.other)},
                            soc: {position: 'right', min: 0, max: 100, stacked: false}}}
                });
                const snapshot = () => ({min: chart.scales.y.min, max: chart.scales.y.max,
                    ticks: chart.scales.y.ticks.map(tick => tick.value),
                    labels: chart.scales.y.ticks.map(tick => tick.label),
                    maxTicksLimit: chart.scales.y.options.ticks.maxTicksLimit,
                    x: [chart.scales.x.min, chart.scales.x.max],
                    soc: [chart.scales.soc.min, chart.scales.soc.max]});
                const result = {sample, initial: snapshot()};
                if (sample.other) {
                    chart.setDatasetVisibility(1, false); chart.update(); result.hidden = snapshot();
                    chart.setDatasetVisibility(1, true); chart.update();
                }
                chart.resize(300, 300); result.resized = snapshot();
                chart.destroy(); return result;
            });
        }""")
    for chart_result in results:
        chart_case = chart_result["sample"]
        plotted_y_values = [value for value in chart_case["values"] if value is not None]
        if "other" in chart_case:
            plotted_y_values = [5, -7]
        if "baseline" in chart_case:
            plotted_y_values.append(chart_case["baseline"])
        for axis_snapshot in (chart_result["initial"], chart_result["resized"]):
            assert math.isfinite(axis_snapshot["min"]) and math.isfinite(axis_snapshot["max"])
            assert axis_snapshot["min"] < axis_snapshot["max"]
            assert axis_snapshot["maxTicksLimit"] == 7
            assert axis_snapshot["x"] == [0, 3]
            assert axis_snapshot["soc"] == [0, 100]
            if plotted_y_values:
                margin = (axis_snapshot["max"] - axis_snapshot["min"]) * 0.1
                assert axis_snapshot["min"] <= min(plotted_y_values) - margin + 1e-12
                assert axis_snapshot["max"] >= max(plotted_y_values) + margin - 1e-12
                if max(plotted_y_values) > min(plotted_y_values):
                    assert min(plotted_y_values) - axis_snapshot["min"] == pytest.approx(margin)
                    assert axis_snapshot["max"] - max(plotted_y_values) == pytest.approx(margin)
            intervals = [
                right - left
                for left, right in zip(axis_snapshot["ticks"], axis_snapshot["ticks"][1:])
            ]
            assert len(intervals) >= 1
            assert intervals == pytest.approx([intervals[0]] * len(intervals))
            normalized = intervals[0] / 10 ** math.floor(math.log10(intervals[0]))
            assert any(normalized == pytest.approx(step) for step in (1, 2, 2.5, 5, 10))
            assert len(set(axis_snapshot["labels"])) == len(axis_snapshot["labels"])
        if "hidden" in chart_result:
            assert chart_result["hidden"]["max"] < chart_result["initial"]["max"]
            assert chart_result["hidden"]["min"] > chart_result["initial"]["min"]
            assert chart_result["hidden"]["min"] == pytest.approx(-3.625)
            assert chart_result["hidden"]["max"] == pytest.approx(2.625)


def test_independent_scales_follow_windows_partial_bars_hidden_series_and_line_edges(browser):
    """Each chart keeps ten-percent headroom around its own visible signed peaks.

    A peak of six needs ±7.5, independently of another chart. Hiding a chart cannot
    change its neighbours. Offscreen peaks do not widen the visible range.
    A line crossing a window uses its interpolated boundary values.
    """
    with browser.new_context() as context:
        page = context.new_page()
        page.set_content("<canvas id='a'></canvas><canvas id='b'></canvas><canvas id='c'></canvas>")
        page.add_script_tag(path=str(WEB / "chart.umd.min.js"))
        page.add_script_tag(path=str(WEB / "chart_theme.js"), type="module")
        page.wait_for_function("Chart.registry.plugins.get('valueAxisHeadroom')")
        snapshots = page.evaluate("""() => {
            const charts = ['a','b','c'].map((id, index) => new Chart(document.getElementById(id), {
                type: 'bar', data: {datasets: [1,-1].map(sign => ({stack:'balance', data:[
                    {x:0.5,y:sign*[1,3,6][index],start:0,end:1},
                    {x:2.5,y:sign*[10,20,100][index],start:2,end:3}]}))},
                options:{responsive:false,animation:false,parsing:false,
                    scales:{x:{type:'linear',min:0,max:1},y:{stacked:true}}}
            }));
            const update = () => {charts.forEach(chart => chart.update()); return charts.map(chart => [chart.scales.y.min,chart.scales.y.max]);};
            const initial=update();
            charts[2].setDatasetVisibility(0,false); charts[2].setDatasetVisibility(1,false);
            const hidden=update();
            charts[2].setDatasetVisibility(0,true); charts[2].setDatasetVisibility(1,true);
            charts.forEach(chart => {chart.options.scales.x.min=0.9;chart.options.scales.x.max=1.1;});
            const partial=update();
            charts.forEach(chart => {chart.options.scales.x.min=2;chart.options.scales.x.max=3;});
            const later=update(); charts.forEach(chart => chart.destroy());
            const line=new Chart(document.getElementById('a'), {type:'line',data:{datasets:[{data:[{x:0,y:100},{x:10,y:0}]}]},
                options:{responsive:false,animation:false,parsing:false,scales:{x:{type:'linear',min:5,max:6},y:{}}}});
            const edge=[line.scales.y.min,line.scales.y.max]; line.destroy();
            return {initial,hidden,partial,later,edge};
        }""")
    for phase in ("initial", "partial"):
        for axis_bounds, extent in zip(snapshots[phase], (1.25, 3.75, 7.5), strict=True):
            assert axis_bounds == pytest.approx([-extent, extent])
    assert snapshots["hidden"][:2] == snapshots["initial"][:2]
    for axis_bounds, extent in zip(snapshots["later"], (12.5, 25, 125), strict=True):
        assert axis_bounds == pytest.approx([-extent, extent])
    assert snapshots["edge"] == pytest.approx([38.75, 51.25])


@pytest.mark.parametrize("width", [390, 1280])
def test_physical_nonnegative_axes_keep_zero_and_only_upper_headroom(browser, width):
    """A six-unit nonnegative peak ends at 6/0.9 with exactly zero below. Empty/zero series stay finite and signed prices retain negative clearance."""
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        page = context.new_page()
        page.set_content("<canvas id='plot'></canvas>")
        page.add_script_tag(path=str(WEB / "chart.umd.min.js"))
        page.add_script_tag(path=str(WEB / "chart_theme.js"), type="module")
        page.wait_for_function("Chart.registry.plugins.get('valueAxisHeadroom')")
        results = page.evaluate("""() => [[0,6],[0,0],[null,null]].map(values => {
            const chart=new Chart(document.getElementById('plot'), {type:'line',data:{datasets:[{data:values}]},
                options:{responsive:false,animation:false,scales:{y:{min:0}}}});
            const initial=[chart.scales.y.min,chart.scales.y.max];
            chart.resize(300,300); const resized=[chart.scales.y.min,chart.scales.y.max];
            chart.destroy();return {initial,resized};
        })""")
    for result in results:
        for bounds in (result["initial"], result["resized"]):
            assert bounds[0] == 0
            assert math.isfinite(bounds[1]) and bounds[1] > 0
    assert results[0]["initial"] == pytest.approx([0, 6 / 0.9])


@pytest.mark.parametrize("width", [390, 1280])
def test_comparison_balances_share_scale_after_range_legend_and_resize(browser, width):
    """All three charts follow their joint signed stacks. SoC and unrelated charts do not enlarge the shared kW axis."""
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        page = context.new_page()
        page.set_content(
            "".join(
                f'<canvas id="plot-{index}" height="{100 + index * 100}"></canvas>'
                for index in range(4)
            )
        )
        page.add_script_tag(path=str(WEB / "chart.umd.min.js"))
        page.add_script_tag(path=str(WEB / "chart_theme.js"), type="module")
        page.wait_for_function("Chart.registry.plugins.get('valueAxisHeadroom')")
        result = page.evaluate("""() => {
            const charts = [2,4,8,1000].map((power,index) => new Chart(document.getElementById(`plot-${index}`), {
                type:'bar', data:{datasets:[1,-1].map(sign => ({type:'bar',stack:'energy',
                    data:[{x:1,y:sign*power,start:0,end:2},{x:8,y:sign*20,start:7,end:9}]})).concat([
                        {type:'line',yAxisID:'soc',data:[{x:1,y:100},{x:8,y:100}]}])},
                options:{responsive:false,animation:false,parsing:false,
                    plugins:{valueAxisHeadroom:{group:index < 3 ? 'comparison' : null}},
                    scales:{x:{type:'linear',min:0,max:10},y:{stacked:true},soc:{position:'right',min:0,max:100}}}
            }));
            const read = () => charts.map(chart => ({min:chart.scales.y.min,max:chart.scales.y.max,
                ticks:chart.scales.y.ticks.map(tick=>tick.value),soc:[chart.scales.soc.min,chart.scales.soc.max]}));
            const full=read();
            for(const chart of charts) chart.options.scales.x.max=6;
            for(const chart of charts) chart.update('none');
            const six=read();
            charts[2].setDatasetVisibility(0,false); charts[2].setDatasetVisibility(1,false); charts[2].update('none');
            const hidden=read();
            charts[2].setDatasetVisibility(0,true); charts[2].setDatasetVisibility(1,true); charts[2].update('none');
            for(const chart of charts) chart.resize(300,300);
            const resized=read();
            return {full,six,hidden,resized};
        }""")
    for phase, bounds in (
        ("full", [-25, 25]),
        ("six", [-10, 10]),
        ("hidden", [-5, 5]),
        ("resized", [-10, 10]),
    ):
        charts = result[phase]
        assert charts[0] == charts[1] == charts[2]
        assert [charts[0]["min"], charts[0]["max"]] == pytest.approx(bounds)
        assert charts[0]["soc"] == [0, 100]
        assert charts[3]["max"] > 1000


@pytest.mark.parametrize("width", [390, 1280])
def test_unchanged_shared_bounds_do_not_redraw_peers(browser, width):
    """A stable scale updates once. A changed group extent updates all three charts exactly once."""
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        page = context.new_page()
        page.set_content(
            '<canvas id="one"></canvas><canvas id="two"></canvas><canvas id="three"></canvas>'
        )
        page.add_script_tag(path=str(WEB / "chart.umd.min.js"))
        page.add_script_tag(path=str(WEB / "chart_theme.js"), type="module")
        page.wait_for_function("Chart.registry.plugins.get('valueAxisHeadroom')")
        result = page.evaluate("""() => {
            const charts=['one','two','three'].map((id,index)=>new Chart(document.getElementById(id),{
                type:'bar',data:{datasets:[{data:[{x:0,y:index+1}]}]},options:{responsive:false,animation:false,
                plugins:{valueAxisHeadroom:{group:'test-shared'}},scales:{x:{type:'linear'},y:{beginAtZero:true}}}}));
            const counts=[0,0,0];
            charts.forEach((chart,index)=>{const update=chart.update;chart.update=function(...args){counts[index]++;return update.apply(this,args);};});
            charts[0].update('none');
            const unchanged=[...counts];counts.fill(0);
            charts[0].data.datasets[0].data[0].y=8;
            charts[0].update('none');
            const changed=[...counts], bounds=charts.map(chart=>[chart.scales.y.min,chart.scales.y.max]);
            charts.forEach(chart=>chart.destroy());
            return {unchanged,changed,bounds};
        }""")
    assert result["unchanged"] == [1, 0, 0]
    assert result["changed"] == [1, 1, 1]
    assert result["bounds"] == [[0, 8 / 0.9]] * 3


def test_complete_chart_batch_expands_shrinks_and_recovers_after_failure(app_page):
    """One update per chart uses the complete group, supports smaller/empty ranges and clears failed batches."""
    page, url = app_page
    page.goto(url + "/overview")
    result = page.evaluate("""async () => {
        const resource=performance.getEntriesByType('resource').find(entry=>new URL(entry.name).pathname.endsWith('/chart_theme.js'));
        const {updateCharts}=await import(resource.name);
        const charts=[2,4,8].map((value,index)=>{
            const canvas=document.createElement('canvas');document.body.append(canvas);
            return new Chart(canvas,{type:'bar',data:{datasets:[{data:[{x:0,y:value}]},{type:'line',yAxisID:'soc',data:[{x:0,y:100}]}]},
                options:{responsive:false,animation:false,plugins:{valueAxisHeadroom:{group:'batch-test'}},
                    scales:{x:{type:'linear'},y:{beginAtZero:true},soc:{position:'right',min:0,max:100}}}});
        });
        const counts=[0,0,0];
        charts.forEach((chart,index)=>{const original=chart.update;chart.update=function(...args){counts[index]++;return original.apply(this,args);};});
        const snapshot=()=>({counts:[...counts],bounds:charts.map(chart=>[chart.scales.y.min,chart.scales.y.max]),soc:charts.map(chart=>[chart.scales.soc.min,chart.scales.soc.max])});
        charts.forEach((chart,index)=>chart.data.datasets[0].data[0].y=[8,16,32][index]);
        updateCharts(charts);const expanded=snapshot();counts.fill(0);
        charts.forEach((chart,index)=>chart.data.datasets[0].data[0].y=[1,2,3][index]);
        updateCharts(charts);const shrunk=snapshot();counts.fill(0);
        charts.forEach(chart=>chart.setDatasetVisibility(0,false));
        updateCharts(charts);const empty=snapshot();
        charts.forEach(chart=>chart.setDatasetVisibility(0,true));
        const original=charts[0].update;charts[0].update=()=>{throw new Error('Synthetic chart failure');};
        let failed=false;try{updateCharts(charts);}catch{failed=true;}finally{charts[0].update=original;}
        charts[2].data.datasets[0].data[0].y=9;charts[2].update('none');const recovered=snapshot();
        charts.forEach(chart=>chart.destroy());
        return {expanded,shrunk,empty,failed,recovered};
    }""")
    for phase, maximum in (("expanded", 32 / 0.9), ("shrunk", 3 / 0.9)):
        assert result[phase]["counts"] == [1, 1, 1]
        for bounds in result[phase]["bounds"]:
            assert bounds == pytest.approx([0, maximum])
    assert result["empty"]["counts"] == [1, 1, 1]
    assert (
        result["empty"]["bounds"][0] == result["empty"]["bounds"][1] == result["empty"]["bounds"][2]
    )
    assert all(math.isfinite(value) for value in result["empty"]["bounds"][0])
    assert result["failed"]
    for phase in ("expanded", "shrunk", "empty", "recovered"):
        assert result[phase]["soc"] == [[0, 100]] * 3
    for bounds in result["recovered"]["bounds"]:
        assert bounds == pytest.approx([0, 9 / 0.9])
