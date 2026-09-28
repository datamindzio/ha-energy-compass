// Run: node --test test-*.mjs (from tools/dashboards)
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';

const source = readFileSync(new URL('./state_bands.js', import.meta.url), 'utf8')
  .replaceAll('__PLAN__', 'sensor.plan')
  .replaceAll('__VALID__', 'binary_sensor.valid')
  .replaceAll('__LOCALE__', 'en-GB')
  .replaceAll('__PRICE_UNIT__', 'PLN/kWh');
const bands = new Function(`return (${source});`)();

const HOUR = 3600000;
const t0 = Date.UTC(2030, 0, 1, 12);
const iso = offset => new Date(t0 + offset).toISOString();

function render(intervals) {
  const annotations = [];
  const hass = {
    states: {
      'sensor.plan': {state: 'ok', attributes: {refreshing: true, intervals}},
      'binary_sensor.valid': {state: 'on'},
    },
    themes: {darkMode: true},
  };
  const chart = {
    el: {getRootNode: () => ({host: {_hass: hass}}), querySelectorAll: () => []},
    w: {globals: {minX: t0, maxX: t0 + 24 * HOUR, gridWidth: 2400}},
    addXaxisAnnotation: annotation => annotations.push(annotation),
    removeAnnotation: () => {},
  };
  bands(chart);
  return annotations.filter(a => a.id.startsWith('ec-state-')).map(a => a.label.text);
}

const slot = (from, state, extra) => ({
  start: iso(from * HOUR), end: iso((from + 1) * HOUR), state, ...extra,
});

test('grid blocks show the mean price weighted by planned grid energy', t => {
  t.mock.timers.enable({apis: ['Date'], now: t0});
  const labels = render([
    slot(1, 'CHARGE_GRID', {buy_per_kwh: 0.5, grid_import_kwh: 3, sell_per_kwh: 9}),
    slot(2, 'CHARGE_GRID', {buy_per_kwh: 1.0, grid_import_kwh: 1, sell_per_kwh: 9}),
    slot(3, 'SELF_CONSUME', {buy_per_kwh: 2, grid_import_kwh: 1}),
    slot(4, 'DISCHARGE_GRID', {sell_per_kwh: 1.2, grid_export_kwh: 1, buy_per_kwh: 9}),
    slot(5, 'DISCHARGE_GRID', {sell_per_kwh: 0.8, grid_export_kwh: 3, buy_per_kwh: 9}),
  ]);
  assert.deepEqual(labels, [
    'CHARGE_GRID · 0.63 PLN/kWh',
    'SELF_CONSUME',
    'DISCHARGE_GRID · 0.90 PLN/kWh',
  ]);
});

test('a block without planned grid energy falls back to the plain mean', t => {
  t.mock.timers.enable({apis: ['Date'], now: t0});
  const labels = render([
    slot(1, 'DISCHARGE_GRID', {sell_per_kwh: 1.0, grid_export_kwh: 0}),
    slot(2, 'DISCHARGE_GRID', {sell_per_kwh: 0.5, grid_export_kwh: 0}),
  ]);
  assert.deepEqual(labels, ['DISCHARGE_GRID · 0.75 PLN/kWh']);
});

test('a block without prices keeps the bare state label', t => {
  t.mock.timers.enable({apis: ['Date'], now: t0});
  assert.deepEqual(render([slot(1, 'CHARGE_GRID', {grid_import_kwh: 2})]), ['CHARGE_GRID']);
});

test('a slot clipped at now weighs only its remaining energy', t => {
  t.mock.timers.enable({apis: ['Date'], now: t0 + 1.75 * HOUR});
  // Visible weights: 4 kWh * 0.25 at 0.40 and 1 kWh at 1.00 -> 0.70.
  const labels = render([
    slot(1, 'CHARGE_GRID', {buy_per_kwh: 0.4, grid_import_kwh: 4}),
    slot(2, 'CHARGE_GRID', {buy_per_kwh: 1.0, grid_import_kwh: 1}),
  ]);
  assert.deepEqual(labels, ['CHARGE_GRID · 0.70 PLN/kWh']);
});
