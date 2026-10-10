import test from 'node:test';
import assert from 'node:assert/strict';
import {dayBounds} from './energy-compass-cost-model.js';

let Card;
globalThis.HTMLElement = class {
  attachShadow() { this.shadowRoot = {innerHTML:'',querySelector:()=>null,querySelectorAll:()=>[]}; }
};
globalThis.customElements = {get:()=>null,define:(_,type)=>{Card=type;}};
await import('./energy-compass-cost-card.js');

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
  runtime_entity:'sensor.energy_compass_deye_controller_runtime',
  mode_entity:'select.energy_compass_deye_mode',
};

function render({purchase=3,deposit=10,futurePurchase=2,futureDeposit=1,complete=true}={}) {
  const now=Date.now(), bounds=dayBounds(now,'Europe/Warsaw');
  const card=new Card();
  card.config={...HOUSEHOLD_CONFIG}; card.zone='Europe/Warsaw';
  card._hass={states:{}};
  card.report={now,bounds,actualCost:[{start:bounds.start,end:now,amount:purchase}],
    actualExport:deposit===null?null:[{start:bounds.start,end:now,amount:deposit}],
    importKwh:0,exportKwh:0,issues:[]};
  card.forecast=()=>({note:'Plan',plan:{rows:[],complete,end:bounds.end,
    importCost:complete?futurePurchase:null,exportValue:complete?futureDeposit:null}});
  card.chart=()=>'';
  card.render();
  return card.shadowRoot.innerHTML;
}
function tile(html,label) {
  const start=html.indexOf('>'+label+'</div>');
  assert.ok(start>=0,`Missing tile: ${label}`);
  return html.slice(start,html.indexOf('</small></div>',start));
}

test('headline balances subtract deposit and show deterioration separately',()=>{
  const html=render();
  assert.match(tile(html,'Bilans dzisiaj'),/-7,00/);
  assert.match(tile(html,'Przewidywany bilans dnia'),/-6,00/);
  assert.match(tile(html,'Wpływ dalszego planu'),/1,00[\s\S]*Pogorszenie bilansu/);
});
test('more future deposit means improvement, with positive magnitude',()=>{
  const html=render({futurePurchase:1,futureDeposit:3});
  assert.match(tile(html,'Wpływ dalszego planu'),/>2,00[\s\S]*Poprawa bilansu/);
});
test('sub-cent change reads as unchanged, matching currency rounding',()=>{
  assert.match(tile(render({futurePurchase:1.001,futureDeposit:1}),'Wpływ dalszego planu'),/>0,00[\s\S]*Bez zmiany bilansu/);
});
test('missing deposit uses zero in balances and explains the assumption',()=>{
  const html=render({deposit:null});
  assert.match(tile(html,'Bilans dzisiaj'),/>3,00/);
  assert.match(tile(html,'Bilans dzisiaj'),/depozyt 0,00/);
  assert.match(tile(html,'Przewidywany bilans dnia'),/>4,00/);
  assert.match(html,/Brak danych o depozycie.*0 zł/);
  assert.match(tile(html,'Wpływ dalszego planu'),/Pogorszenie bilansu/);
});
test('incomplete plan never claims full-day balance or neutral change',()=>{
  const html=render({complete:false});
  assert.match(tile(html,'Przewidywany bilans dnia'),/Brak danych/);
  const impact=tile(html,'Wpływ dalszego planu');
  assert.match(impact,/Brak danych/);
  assert.doesNotMatch(impact,/Bez zmiany bilansu|Poprawa bilansu|Pogorszenie bilansu/);
});

function renderPeriod(period,data,offset=0) {
  const now=Date.now(), bounds=dayBounds(now,'Europe/Warsaw');
  const card=new Card();
  card.config={...HOUSEHOLD_CONFIG}; card.zone='Europe/Warsaw'; card._hass={states:{}}; card.period=period; card.offset=offset;
  card.report={now,bounds,issues:['live-only issue']};
  card.periodData={period,offset,bounds:{start:bounds.start-86_400_000,end:bounds.end},now,data};
  card.bars=()=>'';
  card.render();
  return card.shadowRoot.innerHTML;
}
const d=86_400_000;
test('month view shows totals and warns that early days have no deposit valuation',()=>{
  const start=dayBounds(Date.now(),'Europe/Warsaw').start;
  const rows=[{start:start-d,cost:5,deposit:null,importKwh:4,exportKwh:3},{start,cost:2,deposit:1,importKwh:1,exportKwh:1,today:true,todayMissing:[]}];
  const html=renderPeriod('month',{rows,purchase:7,deposit:1,balance:6,importKwh:5,exportKwh:4,missingCost:[],missingDeposit:[rows[0]],depositFrom:start});
  assert.match(html,/Prąd w tym miesiącu/);
  assert.match(tile(html,'Bilans miesiąca'),/>6,00/);
  assert.match(html,/Depozyt wyceniony od .*przyjęto 0 zł/);
  assert.match(html,/brak wyceny/);
  assert.match(html,/aria-selected="true" class="active">Miesiąc/);
});
test('year view flags missing purchase statistics instead of hiding the year',()=>{
  const start=dayBounds(Date.now(),'Europe/Warsaw').start;
  const rows=[{start:start-40*d,cost:null,deposit:null,importKwh:10,exportKwh:0},{start,cost:3,deposit:null,importKwh:2,exportKwh:0,today:true,todayMissing:['deposit']}];
  const html=renderPeriod('year',{rows,purchase:3,deposit:0,balance:3,importKwh:12,exportKwh:0,missingCost:[rows[0]],missingDeposit:rows,depositFrom:null});
  assert.match(tile(html,'Bilans roku'),/>3,00/);
  assert.match(html,/Brak zapisanego kosztu zakupu/);
  assert.match(html,/Dzisiejsze dane niepełne \(depozyt\)/);
});

test('period tiles show unit prices; balance per kWh is per imported kWh',()=>{
  const start=dayBounds(Date.now(),'Europe/Warsaw').start;
  const rows=[{start,cost:7,deposit:2,importKwh:10,exportKwh:4}];
  const html=renderPeriod('month',{rows,purchase:7,deposit:2,balance:5,importKwh:10,exportKwh:4,purchasePerKwh:.7,depositPerKwh:.5,balancePerKwh:.5,missingCost:[],missingDeposit:[],depositFrom:start},-1);
  assert.match(tile(html,'Zakup'),/0,70 zł\/kWh/);
  assert.match(tile(html,'Depozyt'),/0,50 zł\/kWh/);
  assert.match(tile(html,'Bilans miesiąca'),/0,50 zł\/kWh<\/span> pobranej/);
});
test('past day uses hourly statistics, disables forward navigation only when live, hides live issues',()=>{
  const start=dayBounds(Date.now(),'Europe/Warsaw').start-86_400_000;
  const rows=[{start,cost:1,deposit:null,importKwh:2,exportKwh:0}];
  const html=renderPeriod('day',{rows,purchase:1,deposit:0,balance:1,importKwh:2,exportKwh:0,purchasePerKwh:.5,depositPerKwh:null,balancePerKwh:.5,missingCost:[],missingDeposit:rows,depositFrom:null},-1);
  assert.match(html,/Prąd — /);assert.match(html,/Bilans dnia/);assert.match(html,/Rozbicie godzinowe/);
  assert.doesNotMatch(html,/live-only issue/);
  assert.doesNotMatch(html,/Następny dzień" disabled/);
  assert.match(tile(html,'Depozyt'),/— zł\/kWh/);
});
test('live day view shows unit prices for measured and projected day',()=>{
  const html=render();
  assert.match(tile(html,'Bilans dzisiaj'),/Za kWh: zakup/);
  assert.match(html,/Następny dzień" disabled/);
});
