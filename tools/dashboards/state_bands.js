function (chart) {
  // ApexCharts Card 2.2.3 keeps the latest HA state on its shadow-root host.
  const hass = chart.el.getRootNode().host?._hass;
  for (const id of chart.__energyCompassAnnotationIds || []) chart.removeAnnotation(id);
  chart.__energyCompassAnnotationIds = [];
  const plan = hass?.states['__PLAN__'];
  const valid = hass?.states['__VALID__'];
  const now = Date.now();
  if (!plan || ['unknown', 'unavailable'].includes(plan.state)) return;
  if (plan.attributes.refreshing !== true &&
      (valid?.state !== 'on' || !(Date.parse(plan.attributes.valid_until) > now))) return;

  const min = Math.max(now, chart.w.globals.minX);
  const max = chart.w.globals.maxX;
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return;
  const stateColors = {
    CHARGE_PV: '#fdd835',
    CHARGE_GRID: '#ef5350',
    DISCHARGE_GRID: '#ab47bc',
    SELF_CONSUME: '#66bb6a',
  };
  // Grid blocks show the energy-weighted mean of the price they trade at.
  const priceFields = {
    CHARGE_GRID: ['buy_per_kwh', 'grid_import_kwh'],
    DISCHARGE_GRID: ['sell_per_kwh', 'grid_export_kwh'],
  };
  const intervals = (plan.attributes.intervals || []).map(row => {
    const from = Date.parse(row.start), to = Date.parse(row.end);
    const start = Math.max(min, from), end = Math.min(max, to);
    const [priceKey, energyKey] = priceFields[row.state] || [];
    const price = priceKey ? Number(row[priceKey]) : NaN;
    const energy = energyKey ? Number(row[energyKey]) : NaN;
    // A slot clipped at now or at the chart edge keeps only its visible share of energy.
    const share = to > from ? (end - start) / (to - from) : 0;
    return {start, end, state: row.state, price,
      weight: Number.isFinite(energy) && energy > 0 ? energy * share : 0};
  }).filter(row => Number.isFinite(row.start) && row.end > row.start)
    .sort((a, b) => a.start - b.start);
  const runs = [];
  for (const interval of intervals) {
    let run = runs[runs.length - 1];
    if (run && run.state === interval.state && run.end === interval.start) {
      run.end = interval.end;
    } else {
      run = {start: interval.start, end: interval.end, state: interval.state,
        weighted: 0, weight: 0, sum: 0, count: 0};
      runs.push(run);
    }
    if (Number.isFinite(interval.price)) {
      run.weighted += interval.price * interval.weight;
      run.weight += interval.weight;
      run.sum += interval.price;
      run.count += 1;
    }
  }
  // Without planned grid energy the block falls back to the plain mean of its slots.
  const formatPrice = run => {
    const price = run.weight > 0 ? run.weighted / run.weight : run.count ? run.sum / run.count : NaN;
    return Number.isFinite(price) ? price.toLocaleString('__LOCALE__',
      {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' __PRICE_UNIT__' : '';
  };
  for (const run of runs) run.price = formatPrice(run);
  const describe = run => run.price ? run.state + ' · ' + run.price : run.state;

  const add = (annotation) => {
    chart.addXaxisAnnotation(annotation, false);
    chart.__energyCompassAnnotationIds.push(annotation.id);
  };
  runs.forEach((band, index) => add({
    id: 'ec-background-' + index,
    x: band.start, x2: band.end,
    fillColor: stateColors[band.state] || '#78909c', opacity: 0.14,
    borderColor: 'transparent', strokeDashArray: 0,
  }));

  runs.forEach((run, index) => {
    const width = (run.end - run.start) / (max - chart.w.globals.minX) * chart.w.globals.gridWidth;
    const label = width >= 13 ? {
      text: describe(run), position: 'top', orientation: 'vertical', offsetY: 8, offsetX: width / 2,
      borderWidth: 0,
      style: {background: 'transparent', color: hass?.themes?.darkMode === false ? '#616161' : '#bdbdbd', fontSize: '9px', fontWeight: 700,
        padding: {left: 1, right: 1, top: 1, bottom: 1}},
    } : {text: ''};
    add({id: 'ec-state-' + index, x: run.start, x2: run.end,
      fillColor: 'transparent', opacity: 0, borderColor: '#616161', strokeDashArray: 3, label});
  });

  // Native SVG titles expose even intervals too narrow for a visible label.
  for (const [index, run] of runs.entries()) {
    const time = stamp => new Date(stamp).toLocaleTimeString('__LOCALE__', {hour: '2-digit', minute: '2-digit'});
    const elements = chart.el.querySelectorAll('.ec-state-' + index);
    for (const element of elements) {
      const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = `${run.state} · ${time(run.start)}–${time(run.end)}` +
        (run.price ? ` · ${run.price}` : '');
      element.appendChild(title);
    }
  }
}
