import test from 'node:test';
import assert from 'node:assert/strict';
import {dayBounds} from './energy-compass-cost-model.js';

const defined = new Map();
globalThis.HTMLElement = class {
  attachShadow() { this.shadowRoot = {innerHTML:'',querySelector:()=>null,querySelectorAll:()=>[]}; }
};
globalThis.customElements = {get:name=>defined.get(name)||null,define:(name,type)=>{defined.set(name,type);}};
globalThis.window = globalThis;
await import('./energy-compass-cost-card.js');
const Card = defined.get('energy-compass-cost-card');

const HOUSEHOLD_CONFIG = {
  language:'pl', currency:'PLN', tariff_label:'PGE G12', deposit_backfill:'pv_costs:depozyt_backfill',
  cost_entity:'sensor.inverter_deye_total_energy_import_cost',
  import_entity:'sensor.inverter_deye_total_energy_import',
  export_entity:'sensor.inverter_deye_total_energy_export',
  import_price_entity:'sensor.cena_pse_kupna_energii',
  plan_entity:'sensor.energy_compass_home_pilot_plan',
  valid_entity:'binary_sensor.energy_compass_home_pilot_poprawna_prognoza',
  export_prices_entity:'sensor.energy_compass_rce_export_forecast',
  deposit_entity:'sensor.pv_depozyt',
};

function render(config, {purchase=3,deposit=10,futurePurchase=2,futureDeposit=1,complete=true}={}) {
  const now=Date.now(), bounds=dayBounds(now,'Europe/Warsaw');
  const card=new Card();
  card.config=config; card.zone='Europe/Warsaw';
  card._hass={states:{}};
  card.report={now,bounds,actualCost:[{start:bounds.start,end:now,amount:purchase}],
    actualExport:deposit===null?null:[{start:bounds.start,end:now,amount:deposit}],
    importKwh:0,exportKwh:0,issues:[]};
  card.forecast=()=>({note:'note',plan:{rows:[],complete,end:bounds.end,
    importCost:complete?futurePurchase:null,exportValue:complete?futureDeposit:null}});
  card.chart=()=>'';
  card.render();
  return card.shadowRoot.innerHTML;
}
function renderPeriod(config,period,data,offset=0) {
  const now=Date.now(), bounds=dayBounds(now,'Europe/Warsaw');
  const card=new Card();
  card.config=config; card.zone='Europe/Warsaw'; card._hass={states:{}}; card.period=period; card.offset=offset;
  card.report={now,bounds,issues:[]};
  card.periodData={period,offset,bounds:{start:bounds.start-86_400_000,end:bounds.end},now,data};
  card.bars=()=>'';
  card.render();
  return card.shadowRoot.innerHTML;
}

const PL_WORDS = /Bilans|Zakup|Depozyt|Prognoza|Wpływ|Rozbicie/;

test('English day view has no Polish words or zł currency',()=>{
  const html=render({...HOUSEHOLD_CONFIG,language:'en',currency:'EUR',tariff_label:undefined});
  assert.doesNotMatch(html,PL_WORDS);
  assert.doesNotMatch(html,/zł/);
  assert.match(html,/€/);
});

test('English month view has no Polish words or zł currency',()=>{
  const start=dayBounds(Date.now(),'Europe/Warsaw').start;
  const rows=[{start,cost:7,deposit:2,importKwh:10,exportKwh:4}];
  const html=renderPeriod({...HOUSEHOLD_CONFIG,language:'en',currency:'EUR',tariff_label:undefined},'month',
    {rows,purchase:7,deposit:2,balance:5,importKwh:10,exportKwh:4,purchasePerKwh:.7,depositPerKwh:.5,balancePerKwh:.5,missingCost:[],missingDeposit:[],depositFrom:start});
  assert.doesNotMatch(html,PL_WORDS);
  assert.doesNotMatch(html,/zł/);
  assert.match(html,/€/);
});

test('setConfig throws listing every missing required key',()=>{
  const card=new Card();
  assert.throws(()=>card.setConfig({}),/cost_entity/);
  assert.throws(()=>card.setConfig({}),/import_entity/);
  assert.throws(()=>card.setConfig({}),/export_entity/);
  assert.throws(()=>card.setConfig({}),/plan_entity/);
  assert.throws(()=>card.setConfig({}),/valid_entity/);
  assert.throws(()=>card.setConfig({}),/export_prices_entity/);
});

test('setConfig succeeds with all required keys present',()=>{
  const card=new Card();
  card._hass={states:{},config:{}};
  card.isConnected=false;
  assert.doesNotThrow(()=>card.setConfig(HOUSEHOLD_CONFIG));
});

test('missing deposit_backfill never puts undefined/null into statistic_ids',async()=>{
  const card=new Card();
  const config={...HOUSEHOLD_CONFIG}; delete config.deposit_backfill;
  card.config=config; card.zone='Europe/Warsaw';
  // Mid-month: on the 1st the month-to-date read is empty and makes no call.
  const now=Date.parse('2026-09-15T10:00:00Z'), bounds=dayBounds(now,'Europe/Warsaw');
  card.report={now,bounds,actualCost:[{start:bounds.start,end:now,amount:1}],actualExport:[{start:bounds.start,end:now,amount:1}],importKwh:1,exportKwh:1,issues:[]};
  const calls=[];
  card._hass={states:{},callWS:async req=>{calls.push(req); return {};}};
  await card.loadPeriod(0);
  for(const req of calls){
    if(req.statistic_ids){
      assert.ok(req.statistic_ids.every(id=>id!==undefined&&id!==null),`statistic_ids must not contain undefined/null: ${JSON.stringify(req.statistic_ids)}`);
    }
  }
  assert.ok(calls.some(c=>c.statistic_ids));
});

test('tariff_label absent produces no PGE text and no dangling separator',()=>{
  const html=render({...HOUSEHOLD_CONFIG,tariff_label:undefined});
  assert.doesNotMatch(html,/PGE/);
  assert.doesNotMatch(html,/ · <\/p>/);
  assert.doesNotMatch(html,/muted"> · /);
});

test('pv-cost-card alias is still defined',()=>{
  assert.ok(defined.has('pv-cost-card'));
  assert.ok(defined.has('energy-compass-cost-card'));
});
