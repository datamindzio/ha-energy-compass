import {dayBounds, periodBounds, periodRows, perKwh, balancePerKwh, mergeChanges, ledger, partialLedger, patchCost, stepPrices, priceEnergy, project, combine, planUsable} from './energy-compass-cost-model.js';

const REQUIRED = ['cost_entity','import_entity','export_entity','plan_entity','valid_entity','export_prices_entity'];
const OPTIONAL_DEFAULTS = {
  runtime_entity: 'sensor.energy_compass_deye_controller_runtime',
  mode_entity: 'select.energy_compass_deye_mode',
  currency: 'PLN',
};
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sum = rows => rows.reduce((s,r) => s+r.amount,0);
const GRAIN = {day:'hour',month:'day',year:'month'};
const LOCALE = {pl:'pl-PL',en:'en-GB'};

const formatCache = new Map();
function formatsFor(lang,currency) {
  const key = lang+'|'+currency;
  let f = formatCache.get(key);
  if (!f) {
    const locale = LOCALE[lang] || LOCALE.en;
    const money = new Intl.NumberFormat(locale,{style:'currency',currency});
    const number = new Intl.NumberFormat(locale,{maximumFractionDigits:2});
    const unitNumber = new Intl.NumberFormat(locale,{minimumFractionDigits:2,maximumFractionDigits:2});
    // pl+PLN keeps the household's familiar 'zł'; everything else uses the Intl symbol.
    const symbol = (lang==='pl'&&currency==='PLN') ? 'zł'
      : new Intl.NumberFormat(locale,{style:'currency',currency,currencyDisplay:'narrowSymbol'}).formatToParts(0).find(p=>p.type==='currency').value;
    f = {locale,money,number,unitNumber,symbol};
    formatCache.set(key,f);
  }
  return f;
}

const TEXT = {
  pl: {
    periods: {day:'Dziś',month:'Miesiąc',year:'Rok'},
    step: {day:'dzień',month:'miesiąc',year:'rok'},
    prevAria: step => `Poprzedni ${step}`,
    nextAria: step => `Następny ${step}`,
    refreshAria: 'Odśwież koszty',
    refreshing: 'Odczyt…',
    refresh: 'Odśwież',
    loadingNotice: 'Pobieranie dzisiejszych kosztów i planu…',
    noData: 'Brak danych',
    unit: (value,symbol) => `${value} ${symbol}/kWh`,
    noUnit: symbol => `— ${symbol}/kWh`,
    titleLive: {day:'Prąd dzisiaj',month:'Prąd w tym miesiącu',year:'Prąd w tym roku'},
    titlePast: name => `Prąd — ${name}`,
    subtitlePast: 'Zakończony okres · statystyki HA',
    kpiActualBalance: 'Bilans dzisiaj',
    kpiProjectedBalance: 'Przewidywany bilans dnia',
    kpiRemainingImpact: 'Wpływ dalszego planu',
    impact: {unknown:'Pełny plan niedostępny',neutral:'Bez zmiany bilansu',improvement:'Poprawa bilansu',deterioration:'Pogorszenie bilansu'},
    breakdown: (purchase,deposit) => `Zakup ${purchase} − depozyt ${deposit}`,
    ratesLine: (purchase,deposit,balance) => `Za kWh: zakup ${purchase} · depozyt ${deposit} · bilans ${balance}`,
    fromMidnightTo: clock => `Od 00:00 do ${clock}`,
    measuredPlusPlanToMidnight: 'Pomiar + plan do 24:00',
    fromTo2400: clock => `Od ${clock} do 24:00`,
    balanceExplainDay: 'Bilans = zakup − depozyt. Wartość ujemna oznacza przewagę depozytu; nie jest wypłatą ani kwotą faktury.',
    depositMissingToday: 'Brak danych o depozycie — przyjęto 0 zł.',
    staleData: clock => `Dane nie odświeżały się od ${clock}. Widoczne ostatnie odczytane wartości.`,
    planUnconfirmed: 'Wykonanie planu nie jest potwierdzone. Prognoza pozostaje scenariuszem według Energy Compass.',
    chartHeading: 'Koszt zakupu i depozyt — narastająco',
    chartSub: 'Dzisiejsze pomiary i dalszy plan do północy',
    legendPurchaseActual: 'Zakup · wykonanie',
    legendPurchaseForecast: 'Zakup · prognoza',
    legendDepositActual: 'Depozyt · wykonanie',
    legendDepositForecast: 'Depozyt · prognoza',
    exportSectionTitle: 'Wartość eksportu',
    exportSectionSub: 'Szacowany przyrost depozytu prosumenckiego',
    soFar: 'Dotychczas',
    wholeDayWithPlan: 'Cały dzień z planem',
    stillAccordingToPlan: value => `Jeszcze według planu: ${value}`,
    purchaseSectionTitle: 'Koszt zakupu',
    gridEnergyByTariff: tariff => tariff ? `Energia z sieci według taryfy ${tariff}` : 'Energia pobrana z sieci',
    statusDrawn: 'Pobrano z sieci: ',
    statusReturned: ' · Oddano: ',
    statusRead: clock => `Odczyt ${clock}`,
    statusPlan: 'Plan',
    statusPlanUnavailable: 'niedostępny',
    statusUpdate: ' · Aktualizacja co minutę',
    hourlyBreakdown: 'Rozbicie godzinowe',
    tableHour: 'Godzina',
    tablePurchase: 'Zakup',
    tableDeposit: 'Depozyt',
    tableBalance: 'Bilans',
    kindActual: 'Pomiar',
    kindForecast: 'Plan',
    kindMixed: 'Pomiar + plan',
    dayFootnote: (symbol,tariff,extra) => {
      const base = `Kwoty w ${symbol}. Zakup według zapisanych kosztów${tariff?' ('+tariff+')':''}. Eksport wyceniony prognozą cen sprzedaży. Rozkład eksportu oszacowany z przyrostów licznika w przedziałach pomiarowych. Bez opłat stałych i kosztu zużycia baterii. Bilans nie jest kwotą faktury ani wypłatą gotówki.`;
      return extra ? `${base} ${extra}` : base;
    },
    chartMoney: (symbol,currency) => currency==='PLN' ? 'Koszt zakupu i depozyt w złotych' : `Koszt zakupu i depozyt w ${symbol}`,
    chartAria: clock => `Narastający koszt zakupu i depozyt: wykonanie do ${clock}, prognoza do końca dnia`,
    chartMarker: clock => `Pomiar ${clock}`,
    chartNotice: 'Wykres pojawi się po odczytaniu historii zakupu lub depozytu.',
    barsNotice: 'Brak danych do wykresu.',
    barsAria: 'Zakup i depozyt w kolejnych okresach',
    forecastUnavailable: 'Prognoza niedostępna lub wygasła.',
    forecastPartial: clock => `Plan obejmuje czas tylko do ${clock}. Pełna suma dnia niedostępna.`,
    forecastRefreshing: 'Trwa aktualizacja planu; widoczna ostatnia poprawna prognoza.',
    forecastFollowing: 'Prognoza zakłada wykonanie aktualnego planu.',
    forecastWaiting: 'Oczekiwanie na wspólny zakres pomiarów i planu.',
    exportLabel: 'Eksport',
    importLabel: 'Import',
    incompleteHistory: label => `${label}: niepełna historia pomiarów.`,
    costEstimated: from => `Koszt zakupu od ${from} oszacowany: import × cena G12 (brak zapisanego kosztu, np. po restarcie HA).`,
    costMissing: from => `Koszt zakupu: brak danych od ${from} — dla tego okresu przyjęto 0 zł.`,
    depositMissingPrices: 'Depozyt: brakuje cen RCE dla części dzisiejszego eksportu.',
    historyFailed: 'Nie udało się pobrać historii. Ponów odczyt.',
    periodStatsFailed: 'Nie udało się pobrać statystyk okresu. Ponów odczyt.',
    depositUnvalued: entity => entity ? `Brak wyceny depozytu w tym okresie (licznik ${entity} dopiero zbiera dane) — przyjęto 0 zł, bilans zawyżony.` : 'Brak wyceny depozytu w tym okresie — przyjęto 0 zł, bilans zawyżony.',
    depositFrom: (from,scopeWord) => `Depozyt wyceniony od ${from}; ${scopeWord} bez wyceny — przyjęto 0 zł, bilans zawyżony.`,
    earlierScope: {hour:'wcześniejsze godziny',day:'wcześniejsze dni',month:'wcześniejsze miesiące'},
    noPeriodStats: 'Brak statystyk dla tego okresu.',
    missingCostList: list => `Brak zapisanego kosztu zakupu: ${list} — pominięto.`,
    partialDaysThisMonth: n => `Bieżący miesiąc: ${n} dni bez zapisanego kosztu zakupu.`,
    fieldNames: {cost:'koszt zakupu',deposit:'depozyt',importKwh:'import',exportKwh:'eksport'},
    todayIncomplete: list => `Dzisiejsze dane niepełne (${list}) — widoczne pozostałe dni.`,
    todayPrefix: s => `Dziś: ${s}`,
    balanceLabel: scope => `Bilans ${scope}`,
    balanceScope: {day:'dnia',month:'miesiąca',year:'roku'},
    kwhFromGrid: v => `${v} kWh z sieci`,
    kwhExported: v => `${v} kWh oddane`,
    balanceExplainPeriod: 'Bilans = zakup − depozyt. Tylko pomiary, bez prognozy. Cena bilansu za kWh = bilans / kWh pobrane z sieci. Wartość ujemna oznacza przewagę depozytu; nie jest kwotą faktury.',
    chartByGrain: {hour:'godzinowo',day:'dziennie',month:'miesięcznie'},
    chartSectionTitle: 'Zakup i depozyt — ',
    legendPurchase: 'Zakup',
    legendDeposit: 'Depozyt',
    breakdownByGrain: {hour:'godzinowe',day:'dzienne',month:'miesięczne'},
    breakdownTitle: 'Rozbicie ',
    grainHeader: {hour:'Godzina',day:'Dzień',month:'Miesiąc'},
    tableImportKwh: 'Import kWh',
    tableExportKwh: 'Eksport kWh',
    noValuation: 'brak wyceny',
    periodFootnote: (symbol,tariff,live,extra) => {
      const base = `Kwoty w ${symbol}. Zakup według zapisanych kosztów${tariff?' ('+tariff+')':''} (statystyki HA). Depozyt wyceniony prognozą cen sprzedaży.${live?' Dziś liczone jak w widoku „Dziś”.':''} Bez opłat stałych.`;
      return extra ? `${base} ${extra}` : base;
    },
    completedPeriodTo: (from,to) => `Od ${from} do ${to}`,
    wholeDay: 'Cały dzień',
    doClock: clock => `do ${clock}`,
    perKwhDrawnFromGrid: 'pobranej z sieci',
  },
  en: {
    periods: {day:'Today',month:'Month',year:'Year'},
    step: {day:'day',month:'month',year:'year'},
    prevAria: step => `Previous ${step}`,
    nextAria: step => `Next ${step}`,
    refreshAria: 'Refresh costs',
    refreshing: 'Loading…',
    refresh: 'Refresh',
    loadingNotice: "Loading today's costs and plan…",
    noData: 'No data',
    unit: (value,symbol) => `${value} ${symbol}/kWh`,
    noUnit: symbol => `— ${symbol}/kWh`,
    titleLive: {day:'Electricity today',month:'Electricity this month',year:'Electricity this year'},
    titlePast: name => `Electricity — ${name}`,
    subtitlePast: 'Completed period · HA statistics',
    kpiActualBalance: 'Balance today',
    kpiProjectedBalance: 'Projected day balance',
    kpiRemainingImpact: 'Impact of the remaining plan',
    impact: {unknown:'Full plan unavailable',neutral:'No change to balance',improvement:'Balance improves',deterioration:'Balance worsens'},
    breakdown: (purchase,deposit) => `Purchase ${purchase} − deposit ${deposit}`,
    ratesLine: (purchase,deposit,balance) => `Per kWh: purchase ${purchase} · deposit ${deposit} · balance ${balance}`,
    fromMidnightTo: clock => `From 00:00 to ${clock}`,
    measuredPlusPlanToMidnight: 'Measured + plan to 24:00',
    fromTo2400: clock => `From ${clock} to 24:00`,
    balanceExplainDay: 'Balance = purchase − deposit. A negative value means the deposit is larger; it is not a payout or an invoice amount.',
    depositMissingToday: 'No deposit data — assumed 0.',
    staleData: clock => `Data has not refreshed since ${clock}. Showing the last values read.`,
    planUnconfirmed: 'Plan execution is not confirmed. The forecast remains an Energy Compass scenario.',
    chartHeading: 'Cumulative purchase cost and deposit',
    chartSub: "Today's measurements and the remaining plan to midnight",
    legendPurchaseActual: 'Purchase · measured',
    legendPurchaseForecast: 'Purchase · forecast',
    legendDepositActual: 'Deposit · measured',
    legendDepositForecast: 'Deposit · forecast',
    exportSectionTitle: 'Export value',
    exportSectionSub: 'Estimated growth of the prosumer deposit',
    soFar: 'So far',
    wholeDayWithPlan: 'Whole day with plan',
    stillAccordingToPlan: value => `Still according to the plan: ${value}`,
    purchaseSectionTitle: 'Purchase cost',
    gridEnergyByTariff: tariff => tariff ? `Grid energy under the ${tariff} tariff` : 'Energy drawn from the grid',
    statusDrawn: 'Drawn from the grid: ',
    statusReturned: ' · Exported: ',
    statusRead: clock => `Read ${clock}`,
    statusPlan: 'Plan',
    statusPlanUnavailable: 'unavailable',
    statusUpdate: ' · Updates every minute',
    hourlyBreakdown: 'Hourly breakdown',
    tableHour: 'Hour',
    tablePurchase: 'Purchase',
    tableDeposit: 'Deposit',
    tableBalance: 'Balance',
    kindActual: 'Measured',
    kindForecast: 'Plan',
    kindMixed: 'Measured + plan',
    dayFootnote: (symbol,tariff,extra) => {
      const base = `Amounts in ${symbol}. Purchase priced from recorded costs${tariff?' ('+tariff+')':''}. Export is valued with the export price forecast. Export distribution is estimated from meter increments within each interval. No fixed fees or battery wear cost included. The balance is not an invoice amount or a payout.`;
      return extra ? `${base} ${extra}` : base;
    },
    chartMoney: (symbol) => `Purchase cost and deposit in ${symbol}`,
    chartAria: clock => `Cumulative purchase cost and deposit: measured up to ${clock}, forecast to end of day`,
    chartMarker: clock => `Measured ${clock}`,
    chartNotice: 'The chart appears once purchase or deposit history has loaded.',
    barsNotice: 'No data for the chart.',
    barsAria: 'Purchase and deposit over successive periods',
    forecastUnavailable: 'Forecast unavailable or expired.',
    forecastPartial: clock => `The plan only covers time up to ${clock}. The full-day total is unavailable.`,
    forecastRefreshing: 'The plan is updating; showing the last valid forecast.',
    forecastFollowing: 'The forecast assumes the current plan is followed.',
    forecastWaiting: 'Waiting for measurements and plan to share a common range.',
    exportLabel: 'Export',
    importLabel: 'Import',
    incompleteHistory: label => `${label}: incomplete measurement history.`,
    costEstimated: from => `Purchase cost from ${from} estimated: import × recorded price (no saved cost, e.g. after an HA restart).`,
    costMissing: from => `Purchase cost: no data from ${from} — assumed 0 for this period.`,
    depositMissingPrices: "Deposit: missing export price data for part of today's export.",
    historyFailed: 'Failed to fetch history. Retry the read.',
    periodStatsFailed: 'Failed to fetch period statistics. Retry the read.',
    depositUnvalued: entity => entity ? `No deposit valuation for this period yet (${entity} has just started collecting data) — assumed 0, balance overstated.` : 'No deposit valuation for this period — assumed 0, balance overstated.',
    depositFrom: (from,scopeWord) => `Deposit valued from ${from}; ${scopeWord} have no valuation — assumed 0, balance overstated.`,
    earlierScope: {hour:'earlier hours',day:'earlier days',month:'earlier months'},
    noPeriodStats: 'No statistics for this period.',
    missingCostList: list => `No saved purchase cost: ${list} — omitted.`,
    partialDaysThisMonth: n => `Current month: ${n} day(s) without a saved purchase cost.`,
    fieldNames: {cost:'purchase cost',deposit:'deposit',importKwh:'import',exportKwh:'export'},
    todayIncomplete: list => `Today's data is incomplete (${list}) — showing the remaining days.`,
    todayPrefix: s => `Today: ${s}`,
    balanceLabel: scope => `Balance ${scope}`,
    balanceScope: {day:'today',month:'this month',year:'this year'},
    kwhFromGrid: v => `${v} kWh from the grid`,
    kwhExported: v => `${v} kWh exported`,
    balanceExplainPeriod: 'Balance = purchase − deposit. Measurements only, no forecast. Balance per kWh = balance / kWh drawn from the grid. A negative value means the deposit is larger; it is not an invoice amount.',
    chartByGrain: {hour:'hourly',day:'daily',month:'monthly'},
    chartSectionTitle: 'Purchase and deposit — ',
    legendPurchase: 'Purchase',
    legendDeposit: 'Deposit',
    breakdownByGrain: {hour:'hourly',day:'daily',month:'monthly'},
    breakdownTitle: 'Breakdown — ',
    grainHeader: {hour:'Hour',day:'Day',month:'Month'},
    tableImportKwh: 'Import kWh',
    tableExportKwh: 'Export kWh',
    noValuation: 'no valuation',
    periodFootnote: (symbol,tariff,live,extra) => {
      const base = `Amounts in ${symbol}. Purchase priced from recorded costs${tariff?' ('+tariff+')':''} (HA statistics). Deposit is valued with the export price forecast.${live?' Today is calculated as in the “Today” view.':''} No fixed fees.`;
      return extra ? `${base} ${extra}` : base;
    },
    completedPeriodTo: (from,to) => `From ${from} to ${to}`,
    wholeDay: 'Whole day',
    doClock: clock => `to ${clock}`,
    perKwhDrawnFromGrid: 'drawn from the grid',
  },
};

class EnergyCompassCostCard extends HTMLElement {
  constructor() { super(); this.attachShadow({mode:'open'}); this.period='day'; this.offset=0; }
  setConfig(config) {
    const missing = REQUIRED.filter(k=>!config?.[k]);
    if (missing.length) throw new Error(`energy-compass-cost-card: missing required option(s): ${missing.join(', ')}`);
    this.configGeneration=(this.configGeneration||0)+1;
    this.config = {...OPTIONAL_DEFAULTS,...config}; this.report=null; this.error=null; this.periodData=null;
    this.render(); this.refresh();
  }
  get lang() {
    const l = this.config?.language;
    if (l==='en'||l==='pl') return l;
    return this._hass?.language?.startsWith('pl') ? 'pl' : 'en';
  }
  get t() { return TEXT[this.lang]; }
  f() { return formatsFor(this.lang, this.config?.currency||'PLN'); }
  money(value) { const f=this.f(); return Number.isFinite(value) ? f.money.format(Math.abs(value)<.005?0:value) : this.t.noData; }
  unit(value) { const f=this.f(); return Number.isFinite(value) ? this.t.unit(f.unitNumber.format(value),f.symbol) : this.t.noUnit(f.symbol); }
  getCardSize() { return 15; }
  getGridOptions() { return {columns:'full',rows:'auto',min_columns:6}; }
  connectedCallback() {
    clearInterval(this.timer);
    this.timer = setInterval(() => this.refresh(), 60_000);
    this.resize = new ResizeObserver(entries => {
      const width = entries[0].contentRect.width;
      if (Math.abs(width-(this.width||0))>5) { this.width=width; if(this.report)this.render(); }
    });
    this.resize.observe(this);
    this.refresh();
  }
  disconnectedCallback() { clearInterval(this.timer); this.timer = null; this.resize?.disconnect(); }
  set hass(value) {
    this._hass = value;
    if (!this.config || !this.isConnected) return;
    if (!this.report && !this.loading) this.refresh();
    else if (this.report) {
      const plan=value.states[this.config.plan_entity];
      const runtime=value.states[this.config.runtime_entity]?.attributes.runtime||{};
      const signature=JSON.stringify([plan?.state,plan?.attributes.refreshing,plan?.attributes.valid_until,
        value.states[this.config.valid_entity]?.state,value.states[this.config.mode_entity]?.state,
        runtime.code,runtime.uncertain,runtime.original_deadline]);
      if(Date.parse(plan?.attributes.generated_at)>this.report.now) this.refresh();
      else if(signature!==this.signature) this.render();
      this.signature=signature;
    }
  }
  clock(time) { return new Intl.DateTimeFormat(this.f().locale,{timeZone:this.zone,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(time); }
  async refresh() {
    if (!this._hass || !this.config || !this.isConnected || this.loading) return;
    this.loading = true;
    const now = Date.now();
    this.zone = this._hass.config.time_zone || 'Europe/Warsaw';
    const bounds = dayBounds(now,this.zone);
    const cfg = this.config, t = this.t;
    const generation = this.configGeneration, period = this.period, offset = this.offset;
    const ids = [cfg.cost_entity,cfg.export_entity,cfg.import_entity];
    try {
      const statistics = await this._hass.callWS({
        type:'recorder/statistics_during_period',
        start_time:new Date(bounds.start-5*60_000).toISOString(),end_time:new Date(now).toISOString(),
        statistic_ids:ids,period:'5minute',types:['sum','state'],
      });
      const ends = ids.map(id => (statistics[id] || []).filter(r=>r.end<=now).at(-1)?.end);
      // Include the preceding bucket so history supplies the state at each ledger boundary.
      const historyStart = Math.min(...ends.filter(Number.isFinite),now)-5*60_000;
      const history = await this._hass.callWS({
        type:'history/history_during_period',start_time:new Date(historyStart).toISOString(),
        end_time:new Date(now).toISOString(),entity_ids:ids,
        minimal_response:false,no_attributes:false,significant_changes_only:false,
      });
      if(generation!==this.configGeneration)return;
      const issues = [];
      const read = (id,label) => {
        try { return ledger(statistics[id]||[],history[id]||[],bounds.start,now); }
        catch(e) { issues.push(t.incompleteHistory(label)); console.warn('EC cost ledger',id,e.message); return null; }
      };
      const exportEnergy = read(cfg.export_entity,t.exportLabel);
      const importEnergy = read(cfg.import_entity,t.importLabel);
      // A restarted HA cost sensor stays unknown until import changes; keep the verified part.
      const costPartial = partialLedger(statistics[cfg.cost_entity]||[],history[cfg.cost_entity]||[],bounds.start,now);
      let priceHistory = [];
      if (costPartial.until < now && cfg.import_price_entity) {
        console.warn('EC cost ledger',cfg.cost_entity,costPartial.error);
        try {
          const prices = await this._hass.callWS({
            type:'history/history_during_period',start_time:new Date(costPartial.until).toISOString(),
            end_time:new Date(now).toISOString(),entity_ids:[cfg.import_price_entity],
            minimal_response:false,no_attributes:true,significant_changes_only:false,
          });
          if(generation!==this.configGeneration)return;
          priceHistory = prices[cfg.import_price_entity]||[];
        } catch(e) { console.warn('EC cost price history',e.message); }
      }
      const patched = patchCost(costPartial,importEnergy,stepPrices(priceHistory,now),now);
      const actualCost = patched.segments;
      if (patched.from !== null) {
        const from = patched.from===bounds.start ? '00:00' : this.clock(patched.from);
        issues.push(patched.estimated ? t.costEstimated(from) : t.costMissing(from));
      }
      let actualExport = null;
      if (exportEnergy) {
        try { actualExport = priceEnergy(exportEnergy,this._hass.states[cfg.export_prices_entity]?.attributes.prices || []); }
        catch(e) { issues.push(t.depositMissingPrices); console.warn('EC cost export',e.message); }
      }
      this.report = {now,bounds,actualCost,actualExport,
        importKwh:importEnergy===null?null:sum(importEnergy),exportKwh:exportEnergy===null?null:sum(exportEnergy),issues};
      this.error = null;
      if (this.period !== 'day' || this.offset !== 0) await this.loadPeriod(generation);
    } catch(e) {
      if(generation!==this.configGeneration)return;
      this.error = t.historyFailed;
      console.warn('EC cost history',e.message);
    } finally {
      this.loading = false;
      // A tab switch during this read is loaded right away instead of waiting for the timer.
      if(generation!==this.configGeneration||period!==this.period||offset!==this.offset)this.refresh();
      else this.render();
    }
  }
  // Month/year: recorder day/month `change` before today, plus today's live day-view values.
  async loadPeriod(generation) {
    const r=this.report, cfg=this.config, t=this.t, now=r.now, zone=this.zone, period=this.period, offset=this.offset;
    const month=periodBounds(now,zone,'month');
    const ids={cost:cfg.cost_entity,deposit:cfg.deposit_entity,importKwh:cfg.import_entity,exportKwh:cfg.export_entity};
    const fetch=async (kind,start,end)=>{
      if(end<=start)return {};
      const statisticIds=[...Object.values(ids).filter(Boolean),...(cfg.deposit_backfill?[cfg.deposit_backfill]:[])];
      const res=await this._hass.callWS({type:'recorder/statistics_during_period',start_time:new Date(start).toISOString(),
        end_time:new Date(end).toISOString(),statistic_ids:statisticIds,period:kind,types:['change']});
      const out=Object.fromEntries(Object.entries(ids).map(([k,id])=>[k,(res[id]||[]).filter(x=>x.start<end)]));
      // Backfill (until the counter started) and the live counter cover disjoint time.
      out.deposit=mergeChanges((res[cfg.deposit_backfill]||[]).filter(x=>x.start<end),out.deposit);
      return out;
    };
    const today={cost:r.actualCost?sum(r.actualCost):null,deposit:r.actualExport?sum(r.actualExport):null,importKwh:r.importKwh,exportKwh:r.exportKwh};
    try {
      if(offset!==0){
        // Past period: statistics only, no live values and no forecast.
        const bounds=periodBounds(now,zone,period,offset);
        const data=periodRows(await fetch(GRAIN[period],bounds.start,bounds.end),null);
        if(generation!==this.configGeneration||period!==this.period||offset!==this.offset)return;
        this.periodData={period,offset,bounds,data,now};
        return;
      }
      const monthData=periodRows(await fetch('day',month.start,r.bounds.start),today,r.bounds.start);
      let data=monthData, bounds=month;
      if(period==='year'){
        bounds=periodBounds(now,zone,'year');
        const whole=(key,total)=>monthData.rows.every(x=>x[key]===null)?null:total;
        data=periodRows(await fetch('month',bounds.start,month.start),{cost:whole('cost',monthData.purchase),
          deposit:whole('deposit',monthData.deposit),importKwh:whole('importKwh',monthData.importKwh),exportKwh:whole('exportKwh',monthData.exportKwh),
          costedImportKwh:monthData.costedImportKwh,valuedExportKwh:monthData.valuedExportKwh},month.start);
        const cur=data.rows.at(-1);
        cur.todayMissing=[...new Set([...(cur.todayMissing||[]),...monthData.rows.at(-1).todayMissing])];
        cur.partialDays=monthData.missingCost.filter(x=>!x.today).length;
        // Report the exact first valued day, not just the month bucket.
        if(data.depositFrom===month.start)data.depositFrom=monthData.depositFrom;
      }
      if(generation!==this.configGeneration||period!==this.period||offset!==this.offset)return;
      this.periodData={period,offset,bounds,data,now};
    } catch(e) {
      if(generation!==this.configGeneration)return;
      this.periodData=null;
      this.error=t.periodStatsFailed;
      console.warn('EC cost period',e.message);
    }
  }
  forecast(cutoff, end) {
    const t=this.t;
    const plan = this._hass.states[this.config.plan_entity];
    const attrs = plan?.attributes;
    if (!planUsable(plan,this._hass.states[this.config.valid_entity]?.state,Date.now())) return {plan:null,note:t.forecastUnavailable};
    try {
      const p = project(attrs.intervals||[],cutoff,end);
      return {plan:p,note:!p.complete ? t.forecastPartial(this.clock(p.end)) : attrs.refreshing ? t.forecastRefreshing : t.forecastFollowing};
    } catch(e) {
      console.warn('EC cost forecast',e.message);
      return {plan:null,note:t.forecastWaiting};
    }
  }
  chart(data,bounds,cutoff) {
    const t=this.t, f=this.f();
    const W=Math.max(320,Math.min(1000,(this.width||this.clientWidth||1048)-48)),H=270,L=48,R=20,T=18,B=38;
    const points=[...data.actualCurve,...data.forecastCurve,...data.actualExportCurve,...data.forecastExportCurve];
    if (!points.length) return `<p class="notice">${t.chartNotice}</p>`;
    const top=Math.max(1,Math.ceil(Math.max(...points.map(p=>p[1]))*1.15));
    const x=time=>L+(time-bounds.start)/(bounds.end-bounds.start)*(W-L-R);
    const y=v=>H-B-v/top*(H-T-B);
    const path=ps=>ps.map(([time,v],i)=>`${i?'L':'M'}${x(time).toFixed(2)},${y(v).toFixed(2)}`).join(' ');
    const grids=Array.from({length:5},(_,i)=>{
      const v=top*i/4;return `<line x1="${L}" y1="${y(v)}" x2="${W-R}" y2="${y(v)}" class="grid"/><text x="${L-12}" y="${y(v)+5}" text-anchor="end">${f.number.format(v)}</text>`;
    }).join('');
    const ticks=[];
    for(let time=bounds.start;time<bounds.end;time+=(W<500?6:4)*3600_000) ticks.push(`<text x="${x(time)}" y="${H-10}" text-anchor="middle">${this.clock(time)}</text>`);
    ticks.push(`<text x="${x(bounds.end)}" y="${H-10}" text-anchor="end">24:00</text>`);
    const marker=x(cutoff);
    const labelX=Math.min(W-R-65,Math.max(L+65,marker));
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${t.chartAria(this.clock(cutoff))}"><title>${t.chartMoney(f.symbol,this.config.currency||'PLN')}</title>
      ${grids}${ticks.join('')}<text x="${L}" y="12">${f.symbol}</text>
      <path d="${path(data.actualCurve)}" class="actual-line"/>
      <path d="${path(data.forecastCurve)}" class="forecast-line"/>
      <path d="${path(data.actualExportCurve)}" class="export-line"/>
      <path d="${path(data.forecastExportCurve)}" class="export-forecast-line"/>
      <line x1="${marker}" y1="${T}" x2="${marker}" y2="${H-B}" class="marker"/>
      <text x="${labelX}" y="${T+11}" text-anchor="middle" class="marker-label">${t.chartMarker(this.clock(cutoff))}</text>
      ${data.actualCurve.length?`<circle cx="${marker}" cy="${y(data.actualImport)}" r="4.5" class="dot"/>`:''}
      ${data.actualExportCurve.length?`<circle cx="${marker}" cy="${y(data.actualExport)}" r="4.5" class="export-dot"/>`:''}
    </svg>`;
  }
  render() {
    if (!this.config) return;
    const t=this.t, f=this.f();
    const r=this.report;
    const zone=this.zone||'Europe/Warsaw';
    const date=new Intl.DateTimeFormat(f.locale,{timeZone:zone,weekday:'long',day:'numeric',month:'long'}).format(r?.now||Date.now());
    const style=`
      :host{display:block;color:var(--primary-text-color);font-family:var(--primary-font-family,Roboto,sans-serif);--purchase:#42a5f5;--projection:#ffb74d;--export:#66bb6a}
      ha-card{padding:24px;background:var(--ha-card-background,var(--card-background-color));overflow:hidden}
      header{display:flex;justify-content:space-between;align-items:start;gap:12px}h1{font-size:26px;line-height:1.2;font-weight:500;margin:0 0 6px}h2{font-size:18px;font-weight:500;margin:0 0 6px}p{margin:0}small,.muted{color:var(--secondary-text-color);font-size:13px;line-height:1.5}
      button{font:inherit;cursor:pointer;color:var(--primary-text-color);background:transparent;border:1px solid var(--divider-color);border-radius:10px;padding:8px 12px;white-space:nowrap}button:hover{background:var(--secondary-background-color)}button:focus-visible,summary:focus-visible{outline:2px solid var(--purchase);outline-offset:3px}button:disabled{opacity:.5;cursor:wait}
      .kpis{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr) minmax(0,1fr);gap:0;margin:24px 0 18px;border:1px solid var(--divider-color);border-radius:12px}
      .kpi{padding:18px 22px;border-right:1px solid var(--divider-color);min-width:0}.kpi:last-child{border:0}.label{font-size:14px;line-height:1.5;color:var(--secondary-text-color)}.value{font-size:34px;font-weight:500;font-variant-numeric:tabular-nums;line-height:1.6;white-space:nowrap}.kpi.featured{background:var(--secondary-background-color)}.kpi.featured .label{color:var(--primary-text-color);font-weight:500}.kpi.featured .value{font-size:42px;font-weight:600}.kpi small{display:block}.kpi .effect{font-weight:500;color:var(--primary-text-color)}.kpi.improvement .value{color:var(--export)}.kpi.deterioration .value{color:var(--projection)}.value.missing,.kpi.featured .value.missing{font-size:21px;padding:10px 0}
      .chart{margin:24px 0 22px}.chart svg{width:100%;height:auto;min-height:180px;display:block;margin-top:12px}svg text{fill:var(--secondary-text-color);font-size:13px;font-family:inherit}.grid{stroke:var(--divider-color);stroke-dasharray:3 4;stroke-width:1}.actual-line{fill:none;stroke:var(--purchase);stroke-width:3}.forecast-line{fill:none;stroke:var(--purchase);stroke-width:3;stroke-dasharray:7 5}.export-line,.export-forecast-line{fill:none;stroke:var(--export);stroke-width:3}.export-forecast-line{stroke-dasharray:7 5}.export-dot{fill:var(--export)}.marker{stroke:var(--secondary-text-color);stroke-dasharray:3 5;opacity:.7}.marker-label{font-size:12px}.dot{fill:var(--purchase)}
      .legend{display:flex;gap:24px;flex-wrap:wrap;font-size:13px;color:var(--secondary-text-color);margin-top:10px}.legend span{display:flex;gap:8px;align-items:center}.legend i{width:25px;height:0;border-top:3px solid var(--purchase)}.legend .future{border-top-style:dashed}.legend .export{border-color:var(--export)}
      .secondary{display:grid;grid-template-columns:1fr 1fr;gap:24px;padding-top:22px;border-top:1px solid var(--divider-color)}.amounts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:12px 0}.amounts strong{display:block;font-size:24px;line-height:1.5;font-weight:500;font-variant-numeric:tabular-nums}.deposit strong{color:var(--export)}
      .notice{padding:12px 14px;border-left:3px solid var(--projection);background:var(--secondary-background-color);font-size:13px;line-height:1.5;margin:12px 0}.status{margin-top:20px;line-height:1.7;font-size:13px;color:var(--secondary-text-color)}.status strong{color:var(--primary-text-color);font-weight:500}
      details{margin-top:22px;border-top:1px solid var(--divider-color);padding-top:16px}summary{cursor:pointer;font-size:16px;font-weight:500;padding:4px 0}table{border-collapse:collapse;width:100%;font-size:14px;margin-top:12px;font-variant-numeric:tabular-nums}th,td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--divider-color)}th:first-child,td:first-child{text-align:left}th{font-size:12px;color:var(--secondary-text-color);font-weight:500}.kind{display:block;font-size:11px;color:var(--secondary-text-color)}tr.future-row td{color:var(--projection)}tr.mixed-row td{font-style:italic}.table-wrap{overflow-x:auto}.footnote{margin-top:18px;font-size:12px;color:var(--secondary-text-color);line-height:1.6}
      .actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.tabs{display:flex;border:1px solid var(--divider-color);border-radius:10px;overflow:hidden}.tabs button{border:0;border-radius:0;padding:8px 12px}.tabs button+button{border-left:1px solid var(--divider-color)}.tabs button.active{background:var(--secondary-background-color);font-weight:600}
      .bar-cost{fill:var(--purchase)}.bar-deposit{fill:var(--export)}.bar-today{opacity:.6}.legend .box{width:12px;height:12px;border:0;background:var(--purchase)}.legend .box.export{background:var(--export)}
      @media(max-width:650px){header{flex-wrap:wrap}.actions{justify-content:flex-start}ha-card{padding:16px}h1{font-size:23px}.kpis{grid-template-columns:1fr;margin-top:18px}.kpi{padding:12px 14px;border-right:0;border-bottom:1px solid var(--divider-color);display:grid;grid-template-columns:1fr auto;align-items:center;gap:0 12px}.kpi .value{grid-column:2;grid-row:1/3;font-size:29px}.kpi small{grid-column:1/-1}.kpi.featured{display:block;padding:18px 14px}.kpi.featured .value{font-size:40px}.kpi .value.missing{font-size:21px}.secondary{grid-template-columns:1fr;gap:18px}.amounts strong{font-size:23px}.chart svg{min-height:150px}.chart{margin-top:20px}th,td{padding:10px 8px;font-size:12px}svg text{font-size:16px}.marker-label{font-size:14px}}
    `;
    const live=this.offset===0;
    const shown=periodBounds(r?.now||Date.now(),zone,this.period,this.offset).start;
    const periodName=new Intl.DateTimeFormat(f.locale,{timeZone:zone,...{day:{weekday:'long',day:'numeric',month:'long',year:'numeric'},month:{month:'long',year:'numeric'},year:{year:'numeric'}}[this.period]}).format(shown);
    const title=live?t.titleLive[this.period]:t.titlePast(periodName);
    const subtitle=live?(this.period==='day'?date:periodName):t.subtitlePast;
    const tabs=`<div class="tabs" role="tablist">${Object.keys(GRAIN).map(k=>`<button role="tab" data-period="${k}" aria-selected="${k===this.period}" class="${k===this.period?'active':''}">${t.periods[k]}</button>`).join('')}</div>`;
    const step=t.step[this.period];
    const nav=`<div class="tabs nav"><button data-shift="-1" aria-label="${t.prevAria(step)}">‹</button><button data-shift="1" aria-label="${t.nextAria(step)}" ${live?'disabled':''}>›</button></div>`;
    const tariff=this.config.tariff_label;
    const head=`<header><div><h1>${escape(title)}</h1><p class="muted">${escape(subtitle)}${tariff?' · '+escape(tariff):''}</p></div><div class="actions">${tabs}${nav}<button id="refresh" ${this.loading?'disabled':''} aria-label="${t.refreshAria}">${this.loading?t.refreshing:t.refresh}</button></div></header>`;
    const pd=this.periodData;
    if (!r || ((this.period!=='day'||!live) && (pd?.period!==this.period||pd?.offset!==this.offset))) {
      this.shadowRoot.innerHTML=`<style>${style}</style><ha-card>${head}<p class="notice">${this.error||t.loadingNotice}</p></ha-card>`;
      this.bind();return;
    }
    if (this.period!=='day'||!live) { this.renderPeriod(style,head); return; }
    const {plan,note}=this.forecast(r.now,r.bounds.end);
    const data=combine(r.actualCost,r.actualExport,plan,r.bounds.start,r.bounds.end,r.now);
    const stale=Date.now()-r.now>150_000;
    const wrongDay=dayBounds(Date.now(),zone).start!==r.bounds.start;
    // Do not carry yesterday's totals across midnight while the first query runs.
    if (wrongDay) {this.report=null;this.render(); if(!this.loading)this.refresh();return;}
    const value=v=>`<div class="value ${v===null?'missing':''}">${this.money(v)}</div>`;
    const actualNet=data.actualImport===null||data.actualExport===null?null:data.actualImport-data.actualExport;
    const totalNet=data.totalImport===null||data.totalExport===null?null:data.totalImport-data.totalExport;
    const remainingNet=Number.isFinite(plan?.importCost)&&Number.isFinite(plan?.exportValue)?plan.importCost-plan.exportValue:null;
    const impact=remainingNet===null?'unknown':Math.abs(remainingNet)<.005?'neutral':remainingNet<0?'improvement':'deterioration';
    const impactLabel=t.impact[impact];
    const breakdown=(purchase,deposit)=>t.breakdown(this.money(purchase),this.money(deposit));
    const addKwh=(a,b)=>Number.isFinite(a)&&Number.isFinite(b)?a+b:null;
    // Balance per kWh is net cost per kWh drawn from the grid.
    const prices=(purchase,deposit,imp,exp)=>t.ratesLine(this.unit(perKwh(purchase,imp)),this.unit(perKwh(deposit,exp)),this.unit(balancePerKwh(purchase===null||deposit===null?null:purchase-deposit,imp)));
    const mode=this._hass.states[this.config.mode_entity]?.state;
    const runtime=this._hass.states[this.config.runtime_entity]?.attributes.runtime||{};
    const uncertain=Array.isArray(runtime.uncertain)?runtime.uncertain.length>0:Boolean(runtime.uncertain);
    const followed=mode==='Auto'&&runtime.code==='ok'&&!uncertain&&Date.parse(runtime.original_deadline)>Date.now();
    const issues=[...r.issues];
    if(r.actualExport===null)issues.push(t.depositMissingToday);
    if(this.error)issues.push(this.error);
    if(stale)issues.push(t.staleData(this.clock(r.now)));
    if(!followed)issues.push(t.planUnconfirmed);
    const opened=this.shadowRoot.querySelector('details')?.open;
    const table=data.hours.map(h=>`<tr class="${h.kind==='forecast'?'future-row':h.kind==='mixed'?'mixed-row':''}"><td>${this.clock(h.start)}–${h.end===r.bounds.end?'24:00':this.clock(h.end)}<span class="kind">${h.kind==='actual'?t.kindActual:h.kind==='forecast'?t.kindForecast:t.kindMixed}</span></td><td>${this.money(h.importCost)}</td><td>${this.money(h.exportValue)}</td><td>${this.money(h.net)}</td></tr>`).join('');
    const generation=Date.parse(this._hass.states[this.config.plan_entity]?.attributes.generated_at);
    const gridEnergy=t.gridEnergyByTariff(tariff);
    const footnote=t.dayFootnote(f.symbol,tariff,this.config.footnote);
    this.shadowRoot.innerHTML=`<style>${style}</style><ha-card>${head}
      <div class="kpis">
        <div class="kpi"><div class="label">${t.kpiActualBalance}</div>${value(actualNet)}<small>${breakdown(data.actualImport,data.actualExport)}<br>${prices(data.actualImport,data.actualExport,r.importKwh,r.exportKwh)}<br>${t.fromMidnightTo(this.clock(r.now))}</small></div>
        <div class="kpi featured"><div class="label">${t.kpiProjectedBalance}</div>${value(totalNet)}<small>${breakdown(data.totalImport,data.totalExport)}<br>${prices(data.totalImport,data.totalExport,addKwh(r.importKwh,plan?.importKwh),addKwh(r.exportKwh,plan?.exportKwh))}<br>${t.measuredPlusPlanToMidnight}</small></div>
        <div class="kpi ${impact}"><div class="label">${t.kpiRemainingImpact}</div>${value(remainingNet===null?null:Math.abs(remainingNet))}<small><span class="effect">${impactLabel}</span><br>${t.fromTo2400(this.clock(r.now))}</small></div>
      </div>
      <p class="muted">${t.balanceExplainDay}</p>
      ${issues.map(s=>`<p class="notice">${escape(s)}</p>`).join('')}
      <p class="muted">${escape(note)}</p>
      <section class="chart"><h2>${t.chartHeading}</h2><p class="muted">${t.chartSub}</p>${this.chart(data,r.bounds,r.now)}
        <div class="legend"><span><i></i>${t.legendPurchaseActual}</span><span><i class="future"></i>${t.legendPurchaseForecast}</span><span><i class="export"></i>${t.legendDepositActual}</span><span><i class="export future"></i>${t.legendDepositForecast}</span></div>
      </section>
      <section class="secondary">
        <div class="deposit"><h2>${t.exportSectionTitle}</h2><p class="muted">${t.exportSectionSub}</p><div class="amounts"><div><span class="label">${t.soFar}</span><strong>${this.money(data.actualExport)}</strong></div><div><span class="label">${t.wholeDayWithPlan}</span><strong>${this.money(data.totalExport)}</strong></div></div><p class="muted">${t.stillAccordingToPlan(this.money(plan?.exportValue??null))}</p></div>
        <div><h2>${t.purchaseSectionTitle}</h2><p class="muted">${escape(gridEnergy)}</p><div class="amounts"><div><span class="label">${t.soFar}</span><strong>${this.money(data.actualImport)}</strong></div><div><span class="label">${t.wholeDayWithPlan}</span><strong>${this.money(data.totalImport)}</strong></div></div><p class="muted">${t.stillAccordingToPlan(this.money(plan?.importCost??null))}</p></div>
      </section>
      <p class="status">${t.statusDrawn}<strong>${r.importKwh===null?t.noData:f.number.format(r.importKwh)+' kWh'}</strong>${t.statusReturned}<strong>${r.exportKwh===null?t.noData:f.number.format(r.exportKwh)+' kWh'}</strong><br>${t.statusRead(this.clock(r.now))} · ${t.statusPlan} ${Number.isFinite(generation)?this.clock(generation):t.statusPlanUnavailable}${t.statusUpdate}</p>
      <details ${opened?'open':''}><summary>${t.hourlyBreakdown}</summary><div class="table-wrap"><table><thead><tr><th>${t.tableHour}</th><th>${t.tablePurchase}</th><th>${t.tableDeposit}</th><th>${t.tableBalance}</th></tr></thead><tbody>${table}</tbody></table></div></details>
      <p class="footnote">${escape(footnote)}</p>
    </ha-card>`;
    this.bind();
  }
  renderPeriod(style,head) {
    const t=this.t, f=this.f();
    const {period,offset,bounds,data,now}=this.periodData, zone=this.zone||'Europe/Warsaw', live=offset===0;
    const grain=GRAIN[period];
    const label=time=>grain==='hour'?this.clock(time):grain==='day'?new Intl.DateTimeFormat(f.locale,{timeZone:zone,day:'numeric',month:'short'}).format(time):new Intl.DateTimeFormat(f.locale,{timeZone:zone,month:'long'}).format(time);
    const dateLabel=time=>new Intl.DateTimeFormat(f.locale,{timeZone:zone,day:'numeric',month:'long',year:'numeric'}).format(time);
    const value=v=>`<div class="value ${v===null?'missing':''}">${this.money(v)}</div>`;
    const issues=[];
    if(data.depositFrom===null)issues.push(t.depositUnvalued(this.config.deposit_entity));
    else if(data.missingDeposit.length)issues.push(t.depositFrom(grain==='hour'?this.clock(data.depositFrom):dateLabel(data.depositFrom),t.earlierScope[grain]));
    if(!data.rows.length)issues.push(t.noPeriodStats);
    const noCost=data.missingCost.filter(x=>!x.today);
    if(noCost.length)issues.push(t.missingCostList(noCost.map(x=>label(x.start)).join(', ')));
    const partial=data.rows.at(-1)?.partialDays;
    if(partial)issues.push(t.partialDaysThisMonth(partial));
    const names=t.fieldNames;
    const todayMissing=data.rows.at(-1)?.todayMissing||[];
    if(todayMissing.length)issues.push(t.todayIncomplete(todayMissing.map(k=>names[k]).join(', ')));
    if(live)for(const s of this.report.issues)issues.push(t.todayPrefix(s));
    if(this.error)issues.push(this.error);
    const rows=data.rows.map(x=>({...x,net:x.cost===null?null:x.cost-(x.deposit??0)}));
    const kwh=v=>v===null?'—':f.number.format(v);
    const table=rows.map(x=>`<tr class="${x.today?'mixed-row':''}"><td>${escape(label(x.start))}${x.today?'<span class="kind">'+t.doClock(this.clock(now))+'</span>':''}</td><td>${this.money(x.cost)}</td><td>${x.deposit===null?t.noValuation:this.money(x.deposit)}</td><td>${this.money(x.net)}</td><td>${kwh(x.importKwh)}</td><td>${kwh(x.exportKwh)}</td></tr>`).join('');
    const opened=this.shadowRoot.querySelector('details')?.open;
    const scope=t.balanceScope[period];
    const range=live?t.completedPeriodTo(escape(dateLabel(bounds.start)),this.clock(now)):grain==='hour'?t.wholeDay:t.completedPeriodTo(escape(dateLabel(bounds.start)),escape(dateLabel(bounds.end-1)));
    const tariff=this.config.tariff_label;
    const footnote=t.periodFootnote(f.symbol,tariff,live,this.config.footnote);
    this.shadowRoot.innerHTML=`<style>${style}</style><ha-card>${head}
      <div class="kpis">
        <div class="kpi"><div class="label">${t.legendPurchase}</div>${value(data.purchase)}<small>${t.kwhFromGrid(kwh(data.importKwh))}<br><span class="effect">${this.unit(data.purchasePerKwh)}</span></small></div>
        <div class="kpi featured"><div class="label">${t.balanceLabel(scope)}</div>${value(data.balance)}<small>${t.breakdown(this.money(data.purchase),this.money(data.deposit))}<br><span class="effect">${this.unit(data.balancePerKwh)}</span> ${t.perKwhDrawnFromGrid}<br>${range}</small></div>
        <div class="kpi"><div class="label">${t.legendDeposit}</div>${value(data.deposit)}<small>${t.kwhExported(kwh(data.exportKwh))}<br><span class="effect">${this.unit(data.depositPerKwh)}</span></small></div>
      </div>
      <p class="muted">${t.balanceExplainPeriod}</p>
      ${issues.map(s=>`<p class="notice">${escape(s)}</p>`).join('')}
      <section class="chart"><h2>${t.chartSectionTitle}${t.chartByGrain[grain]}</h2>${this.bars(rows,label)}
        <div class="legend"><span><i class="box"></i>${t.legendPurchase}</span><span><i class="box export"></i>${t.legendDeposit}</span></div>
      </section>
      <details ${opened?'open':''}><summary>${t.breakdownTitle}${t.breakdownByGrain[grain]}</summary><div class="table-wrap"><table><thead><tr><th>${t.grainHeader[grain]}</th><th>${t.tablePurchase}</th><th>${t.tableDeposit}</th><th>${t.tableBalance}</th><th>${t.tableImportKwh}</th><th>${t.tableExportKwh}</th></tr></thead><tbody>${table}</tbody></table></div></details>
      <p class="footnote">${escape(footnote)}</p>
    </ha-card>`;
    this.bind();
  }
  bars(rows,label) {
    const t=this.t, f=this.f();
    if(!rows.length)return `<p class="notice">${t.barsNotice}</p>`;
    const W=Math.max(320,Math.min(1000,(this.width||this.clientWidth||1048)-48)),H=240,L=48,R=12,T=14,B=34;
    const top=Math.max(1,Math.ceil(Math.max(...rows.flatMap(r=>[r.cost??0,r.deposit??0]))*1.15));
    const slot=(W-L-R)/rows.length, bw=Math.max(2,Math.min(22,slot*.38));
    const y=v=>H-B-v/top*(H-T-B);
    const grids=Array.from({length:5},(_,i)=>{const v=top*i/4;return `<line x1="${L}" y1="${y(v)}" x2="${W-R}" y2="${y(v)}" class="grid"/><text x="${L-10}" y="${y(v)+5}" text-anchor="end">${f.number.format(v)}</text>`;}).join('');
    const every=Math.ceil(rows.length/(W<500?6:12));
    const bars=rows.map((r,i)=>{
      const cx=L+slot*(i+.5), cls=r.today?' bar-today':'';
      const bar=(v,x,c)=>v===null?'':`<rect x="${x.toFixed(1)}" y="${y(v).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0,H-B-y(v)).toFixed(1)}" class="${c}${cls}"><title>${escape(label(r.start))}: ${this.money(v)}</title></rect>`;
      const tick=i%every===0?`<text x="${cx}" y="${H-10}" text-anchor="middle">${escape(label(r.start))}</text>`:'';
      return bar(r.cost,cx-bw-1,'bar-cost')+bar(r.deposit,cx+1,'bar-deposit')+tick;
    }).join('');
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${t.barsAria}"><text x="${L}" y="11">${f.symbol}</text>${grids}${bars}</svg>`;
  }
  bind(){
    this.shadowRoot.querySelector('#refresh')?.addEventListener('click',()=>this.refresh());
    this.shadowRoot.querySelectorAll('[data-period]').forEach(b=>b.addEventListener('click',()=>{
      if(b.dataset.period===this.period&&this.offset===0)return;
      this.period=b.dataset.period; this.offset=0; this.render(); this.refresh();
    }));
    this.shadowRoot.querySelectorAll('[data-shift]').forEach(b=>b.addEventListener('click',()=>{
      this.offset=Math.min(0,this.offset+Number(b.dataset.shift)); this.render(); this.refresh();
    }));
  }
}
class PVCostCard extends EnergyCompassCostCard {}
if(!customElements.get('energy-compass-cost-card'))customElements.define('energy-compass-cost-card',EnergyCompassCostCard);
if(!customElements.get('pv-cost-card'))customElements.define('pv-cost-card',PVCostCard);
if(typeof window!=='undefined'){
  window.customCards = window.customCards || [];
  window.customCards.push({type:'energy-compass-cost-card',name:'Energy Compass cost card',description:'Purchase cost and export deposit, measured and forecast.'});
}
