let state = null;
let page = 1;
let sortKey = "cost";
let sortDir = "desc";
let statusTimer = null;
let latestSyncStamp = null;
let loadPromise = null;
let accessState = null;
let activeTab = "overview";
let backtestState = null;
let backtestPromise = null;
const pageSize = 50;
const descDefaultSortKeys = new Set(["cost", "quality", "network_installs"]);
const defaultColumnWidths = [118, 92, 132, 150, 360, 118, 126, 126, 126, 126, 126, 136, 136, 136, 136, 178];
const overviewHorizons = [7, 30, 60, 90, 120, 180, 360, 540, 720];
const summaryCachePrefix = "mmpredictions-summary-v3:";
const preferencesKey = "mmpredictions-preferences-v1";
const columnWidthsKey = "mmpredictions-column-widths-v1";
const activeTabKey = "mmpredictions-active-tab-v1";
const backtestCacheKey = "mmpredictions-backtest-v1";
const campaignExclusionsKey = "mmpredictions-campaign-exclusions-v1";
const summaryCacheMaxAgeMs = 24 * 60 * 60 * 1000;
let excludedCampaignSet = new Set();

const pct = v => `${(Number(v || 0) * 100).toFixed(1)}%`;
const money = v => {
  const value = Number(v || 0);
  const digits = Math.abs(value) >= 10000 ? 1 : 2;
  return value.toLocaleString(undefined, {style: "currency", currency: "USD", maximumFractionDigits: digits});
};
const ltv = v => v == null ? "n/a" : Number(v).toLocaleString(undefined, {style: "currency", currency: "USD", maximumFractionDigits: 3});
const retentionPct = v => v == null ? "n/a" : `${(Number(v || 0) * 100).toFixed(1)}%`;
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  "\"": "&quot;",
  "'": "&#39;"
})[c]);
const sourceName = p => p.source_channel || p.partner_name || "unknown";
const roasValue = p => Number(p.display_roas ?? p.predicted_roas ?? 0);
const revenueValue = p => Number(p.display_revenue ?? p.predicted_revenue ?? (roasValue(p) * Number(p.cost || 0)));
const ltvValue = p => p.display_ltv ?? p.predicted_ltv;
const retentionValue = p => p.display_retention ?? p.predicted_retention;
const horizonSortKey = horizon => `h_${horizon}`;
const horizonFromSortKey = key => Number(String(key || "").replace(/^h_/, ""));
const scopeLabel = value => ({
  auto: "Auto scope",
  day: "1-day cohorts",
  week: "7-day cohorts",
  month: "30-day cohorts",
  custom: "Custom dates"
})[value] || value || "Auto scope";

function selected() {
  return {
    scope: document.getElementById("scopeFilter").value,
    dateFrom: document.getElementById("dateFrom").value,
    dateTo: document.getElementById("dateTo").value,
    platform: document.getElementById("platformFilter").value,
    country: document.getElementById("countryFilter").value,
    partner: document.getElementById("partnerFilter").value,
    campaign: document.getElementById("campaignFilter").value
  };
}

function writeHash() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(selected())) {
    if (value) params.set(key === "partner" ? "source" : key, value);
  }
  savePreferences();
  history.replaceState(null, "", `${location.pathname}${location.search}${params.toString() ? `#${params}` : ""}`);
}

function safeStorageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch (_err) {
    return null;
  }
}

function safeStorageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (_err) {
    return;
  }
}

function readCampaignExclusions() {
  const raw = safeStorageGet(campaignExclusionsKey);
  if (!raw) return new Set();
  try {
    const parsed = JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? parsed.filter(Boolean).map(String) : []);
  } catch (_err) {
    return new Set();
  }
}

function writeCampaignExclusions() {
  safeStorageSet(campaignExclusionsKey, JSON.stringify([...excludedCampaignSet].sort()));
}

function campaignExcluded(campaign) {
  return excludedCampaignSet.has(String(campaign || ""));
}

function exclusionMatches(p) {
  return !campaignExcluded(p.campaign_network);
}

function savePreferences() {
  safeStorageSet(preferencesKey, JSON.stringify(selected()));
}

function readPreferences() {
  const raw = safeStorageGet(preferencesKey);
  if (!raw) return;
  try {
    const prefs = JSON.parse(raw);
    const mapping = {
      scope: "scopeFilter",
      platform: "platformFilter",
      country: "countryFilter",
      partner: "partnerFilter",
      campaign: "campaignFilter",
      dateFrom: "dateFrom",
      dateTo: "dateTo"
    };
    for (const [key, id] of Object.entries(mapping)) {
      if (prefs[key]) {
        const control = document.getElementById(id);
        control.value = prefs[key];
        control.dataset.pendingValue = prefs[key];
      }
    }
  } catch (_err) {
    return;
  }
}

function readHash() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  for (const key of ["platform", "country", "campaign"]) {
    const value = params.get(key);
    if (value) {
      const control = document.getElementById(`${key}Filter`);
      control.value = value;
      control.dataset.pendingValue = value;
    }
  }
  const source = params.get("source") || params.get("partner");
  if (source) {
    const control = document.getElementById("partnerFilter");
    control.value = source;
    control.dataset.pendingValue = source;
  }
  if (params.get("scope")) document.getElementById("scopeFilter").value = params.get("scope");
  if (params.get("dateFrom") || params.get("date_from")) document.getElementById("dateFrom").value = params.get("dateFrom") || params.get("date_from");
  if (params.get("dateTo") || params.get("date_to")) document.getElementById("dateTo").value = params.get("dateTo") || params.get("date_to");
}

function summaryParams() {
  const f = selected();
  const params = new URLSearchParams();
  params.set("seed", "0");
  if (f.scope) params.set("scope", f.scope);
  if (f.scope === "custom") {
    if (f.dateFrom) params.set("date_from", f.dateFrom);
    if (f.dateTo) params.set("date_to", f.dateTo);
  }
  if (f.platform) params.set("platform", f.platform);
  return params;
}

function summaryCacheKey(params) {
  const normalized = new URLSearchParams(params);
  normalized.delete("seed");
  return `${summaryCachePrefix}${normalized.toString()}`;
}

function readSummaryCache(key) {
  const raw = safeStorageGet(key);
  if (!raw) return null;
  try {
    const cached = JSON.parse(raw);
    if (!cached.payload || Date.now() - Number(cached.savedAt || 0) > summaryCacheMaxAgeMs) return null;
    return cached;
  } catch (_err) {
    return null;
  }
}

function writeSummaryCache(key, payload) {
  safeStorageSet(key, JSON.stringify({savedAt: Date.now(), payload}));
}

function readBacktestCache() {
  const raw = safeStorageGet(backtestCacheKey);
  if (!raw) return null;
  try {
    const cached = JSON.parse(raw);
    if (!cached.payload || Date.now() - Number(cached.savedAt || 0) > summaryCacheMaxAgeMs) return null;
    return cached.payload;
  } catch (_err) {
    return null;
  }
}

function writeBacktestCache(payload) {
  safeStorageSet(backtestCacheKey, JSON.stringify({savedAt: Date.now(), payload}));
}

function setActiveTab(tab) {
  activeTab = tab || "overview";
  document.querySelectorAll("[data-tab-panel]").forEach(panel => {
    panel.hidden = panel.dataset.tabPanel !== activeTab;
  });
  document.querySelectorAll("[data-tab]").forEach(button => {
    const isActive = button.dataset.tab === activeTab;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-pressed", isActive ? "true" : "false");
  });
  safeStorageSet(activeTabKey, activeTab);
  if (activeTab === "backtest") loadBacktest().catch(err => renderBacktestError(err.message));
}

function readActiveTab() {
  const saved = safeStorageGet(activeTabKey);
  if (saved && ["overview", "campaigns", "backtest", "quality"].includes(saved)) activeTab = saved;
}

function renderAccess() {
  const button = document.getElementById("accessButton");
  const rows = document.getElementById("accessRows");
  if (!accessState || !accessState.is_admin) {
    button.hidden = true;
    return;
  }
  button.hidden = false;
  rows.innerHTML = (accessState.users || []).map(user => `
    <tr>
      <td>${esc(user.email)}</td>
      <td>${esc(user.role)}</td>
    </tr>
  `).join("");
}

async function loadAccess() {
  try {
    const res = await fetch("/api/access", {cache: "no-store"});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || "Access API error");
    accessState = payload;
    renderAccess();
  } catch (_err) {
    document.getElementById("accessButton").hidden = true;
  }
}

async function addAccessUser(event) {
  event.preventDefault();
  const message = document.getElementById("accessMessage");
  message.textContent = "Adding user...";
  const email = document.getElementById("accessEmail").value.trim();
  const role = document.getElementById("accessRole").value;
  try {
    const res = await fetch("/api/access/users", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({email, role})
    });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.detail || payload.error || "Access update failed");
    accessState = payload;
    renderAccess();
    document.getElementById("accessForm").reset();
    message.textContent = `${email} added as ${role}.`;
  } catch (err) {
    message.textContent = err.message;
  }
}

function filteredPredictions() {
  if (!state) return [];
  const f = selected();
  return state.predictions.filter(p =>
    (!f.platform || p.platform === f.platform) &&
    (!f.country || p.country_code === f.country) &&
    (!f.partner || sourceName(p) === f.partner) &&
    (!f.campaign || p.campaign_network === f.campaign) &&
    exclusionMatches(p)
  );
}

function filteredRetentionPredictions() {
  if (!state) return [];
  const f = selected();
  return (state.retention_predictions || []).filter(p =>
    (!f.platform || p.platform === f.platform) &&
    (!f.country || p.country_code === f.country) &&
    (!f.partner || sourceName(p) === f.partner) &&
    (!f.campaign || p.campaign_network === f.campaign) &&
    exclusionMatches(p)
  );
}

function proxyLabel(p) {
  const expected = `D${p.horizon}`;
  return p.target_label && p.target_label !== expected ? p.target_label : "";
}

function qualityClass(score) {
  const value = Number(score || 0);
  if (value >= 0.72) return "quality-high";
  if (value >= 0.60) return "quality-good";
  if (value >= 0.48) return "quality-mid";
  return "quality-low";
}

function qualityText(score) {
  const value = Number(score || 0);
  if (value >= 0.72) return "high";
  if (value >= 0.60) return "solid";
  if (value >= 0.48) return "watch";
  return "low";
}

function predictionQuality(p) {
  return qualityClass(p.confidence_score);
}

function sourcePresenceRows() {
  return (state?.source_presence || []).filter(row => !row.excluded);
}

function campaignCatalogRows() {
  if (!state) return [];
  const f = selected();
  const grouped = new Map();
  for (const p of state.predictions || []) {
    if (f.platform && p.platform !== f.platform) continue;
    if (f.country && p.country_code !== f.country) continue;
    if (f.partner && sourceName(p) !== f.partner) continue;
    const campaign = String(p.campaign_network || "unknown");
    const row = grouped.get(campaign) || {
      campaign,
      sources: new Set(),
      countries: new Set(),
      platforms: new Set(),
      cost: 0,
      horizonRows: 0,
      dedupe: new Set(),
      excluded: campaignExcluded(campaign)
    };
    row.sources.add(sourceName(p));
    row.countries.add(p.country_code === "ZZ" ? p.country : `${p.country} (${p.country_code})`);
    row.platforms.add(p.platform);
    row.horizonRows += 1;
    const costKey = [
      p.cohort_start,
      p.cohort_end,
      p.platform,
      p.country_code,
      sourceName(p),
      p.campaign_network,
      p.campaign_id_network
    ].join("\u001f");
    if (!row.dedupe.has(costKey)) {
      row.dedupe.add(costKey);
      row.cost += Number(p.cost || 0);
    }
    grouped.set(campaign, row);
  }
  return [...grouped.values()].map(row => ({
    ...row,
    sourcesText: [...row.sources].sort().join(", "),
    countriesText: [...row.countries].sort().slice(0, 4).join(", "),
    platformsText: [...row.platforms].sort().join(", ")
  })).sort((a, b) => Number(b.excluded) - Number(a.excluded) || b.cost - a.cost || a.campaign.localeCompare(b.campaign));
}

function renderCampaignExclusions() {
  const count = excludedCampaignSet.size;
  document.getElementById("excludedCampaignCount").textContent = count ? String(count) : "";
  const list = document.getElementById("campaignExclusionList");
  const summary = document.getElementById("campaignExclusionSummary");
  if (!list || !summary) return;
  const query = document.getElementById("campaignExclusionSearch").value.trim().toLowerCase();
  const rows = campaignCatalogRows();
  const visible = rows.filter(row =>
    !query ||
    row.campaign.toLowerCase().includes(query) ||
    row.sourcesText.toLowerCase().includes(query) ||
    row.countriesText.toLowerCase().includes(query)
  );
  const excludedInScope = rows.filter(row => row.excluded).length;
  summary.textContent = `${excludedInScope} excluded · ${visible.length} shown · ${rows.length} campaigns in current slice`;
  if (!visible.length) {
    list.innerHTML = `<div class="muted">No campaigns in current slice.</div>`;
    return;
  }
  list.innerHTML = visible.map((row, index) => {
    const id = `campaignExclude${index}`;
    return `<label class="exclusion-row" title="${esc(row.campaign)}">
      <input id="${id}" type="checkbox" data-campaign="${esc(row.campaign)}" ${row.excluded ? "checked" : ""}>
      <span class="exclusion-campaign">
        <strong>${esc(row.campaign)}</strong>
        <small>${esc(row.sourcesText || "unknown source")}</small>
      </span>
      <span class="muted">${esc(row.platformsText || "n/a")}</span>
      <span class="num">${money(row.cost)}</span>
      <span class="num">${Number(row.horizonRows || 0).toLocaleString()} rows</span>
    </label>`;
  }).join("");
  list.querySelectorAll("input[type='checkbox']").forEach(input => {
    input.addEventListener("change", event => {
      const campaign = event.currentTarget.dataset.campaign || "";
      if (event.currentTarget.checked) excludedCampaignSet.add(campaign);
      else excludedCampaignSet.delete(campaign);
      writeCampaignExclusions();
      page = 1;
      render();
    });
  });
}

function dataScopeNote() {
  const scope = state?.data_scope || {};
  if (!scope.fallback) return "";
  const start = scope.cohort_start || "n/a";
  const end = scope.cohort_end || "n/a";
  return `Using ${scope.used_granularity || "weekly"} fallback cohorts: ${start} - ${end}`;
}

function aggregateByHorizon(items) {
  const result = new Map();
  for (const p of items) {
    const row = result.get(p.horizon) || {horizon: p.horizon, cost: 0, revenue: 0, low: 0, high: 0, errors: [], confidences: [], samples: 0, labels: new Set(), actualCount: 0, proxyCount: 0, count: 0};
    row.cost += Number(p.cost || 0);
    row.networkInstalls = (row.networkInstalls || 0) + Number(p.network_installs || 0);
    row.revenue += revenueValue(p);
    row.low += Number(p.low_roas || 0) * Number(p.cost || 0);
    row.high += Number(p.high_roas || 0) * Number(p.cost || 0);
    if (p.error_mape != null) row.errors.push(Number(p.error_mape));
    if (p.confidence_score != null) row.confidences.push(Number(p.confidence_score));
    if (proxyLabel(p)) row.labels.add(proxyLabel(p));
    if (p.roas_source === "actual") row.actualCount += 1;
    if (p.roas_source === "proxy") row.proxyCount = (row.proxyCount || 0) + 1;
    row.count += 1;
    row.samples += Number(p.sample_size || 0);
    result.set(p.horizon, row);
  }
  return enforceMonotonicAggregate([...result.values()].sort((a, b) => a.horizon - b.horizon).map(r => ({
    ...r,
    proxyLabels: [...r.labels],
    roas: r.cost ? r.revenue / r.cost : 0,
    predictedLtv: r.networkInstalls ? r.revenue / r.networkInstalls : null,
    lowLtv: r.networkInstalls ? r.low / r.networkInstalls : null,
    highLtv: r.networkInstalls ? r.high / r.networkInstalls : null,
    lowRoas: r.cost ? r.low / r.cost : 0,
    highRoas: r.cost ? r.high / r.cost : 0,
    error: r.errors.length ? r.errors.sort((a, b) => a - b)[Math.floor(r.errors.length / 2)] : null,
    confidence: r.confidences.length ? r.confidences.reduce((a, b) => a + b, 0) / r.confidences.length : null
  })));
}

function aggregateRetentionByHorizon(items) {
  const result = new Map();
  for (const p of items) {
    const row = result.get(p.horizon) || {horizon: p.horizon, installs: 0, retained: 0, low: 0, high: 0, errors: [], samples: 0, actualCount: 0, proxyCount: 0, count: 0};
    const installs = Number(p.network_installs || p.installs || 0);
    row.installs += installs;
    row.retained += Number(retentionValue(p) || 0) * installs;
    row.low += Number(p.low_retention || 0) * installs;
    row.high += Number(p.high_retention || 0) * installs;
    if (p.error_mape != null) row.errors.push(Number(p.error_mape));
    if (p.retention_source === "actual") row.actualCount += 1;
    if (p.retention_source === "proxy") row.proxyCount += 1;
    row.samples += Number(p.sample_size || 0);
    row.count += 1;
    result.set(p.horizon, row);
  }
  let previous = 1;
  let previousLow = 1;
  let previousHigh = 1;
  return [...result.values()].sort((a, b) => a.horizon - b.horizon).map(row => {
    const retention = row.installs ? row.retained / row.installs : 0;
    const low = row.installs ? row.low / row.installs : 0;
    const high = row.installs ? row.high / row.installs : 0;
    const next = {
      ...row,
      retention: Math.min(retention, previous),
      lowRetention: Math.min(low, previousLow, retention),
      highRetention: Math.max(Math.min(high, previousHigh), Math.min(retention, previous)),
      error: row.errors.length ? row.errors.sort((a, b) => a - b)[Math.floor(row.errors.length / 2)] : null
    };
    previous = next.retention;
    previousLow = next.lowRetention;
    previousHigh = next.highRetention;
    return next;
  });
}

function enforceMonotonicAggregate(rows) {
  let prevRoas = 0;
  let prevLow = 0;
  let prevHigh = 0;
  let prevLtv = null;
  let prevLowLtv = null;
  let prevHighLtv = null;
  return rows.map(row => {
    const next = {...row};
    next.roas = Math.max(Number(next.roas || 0), prevRoas);
    next.lowRoas = Math.max(Number(next.lowRoas || 0), prevLow);
    next.highRoas = Math.max(Number(next.highRoas || 0), prevHigh, next.roas);
    if (next.predictedLtv != null) {
      next.predictedLtv = prevLtv == null ? next.predictedLtv : Math.max(Number(next.predictedLtv || 0), prevLtv);
      prevLtv = next.predictedLtv;
    }
    if (next.lowLtv != null) {
      next.lowLtv = prevLowLtv == null ? next.lowLtv : Math.max(Number(next.lowLtv || 0), prevLowLtv);
      prevLowLtv = next.lowLtv;
    }
    if (next.highLtv != null) {
      next.highLtv = prevHighLtv == null ? next.highLtv : Math.max(Number(next.highLtv || 0), prevHighLtv, Number(next.predictedLtv || 0));
      prevHighLtv = next.highLtv;
    }
    prevRoas = next.roas;
    prevLow = next.lowRoas;
    prevHigh = next.highRoas;
    return next;
  });
}

function setView(view, detail = "") {
  const panel = document.getElementById("statePanel");
  panel.className = "state-panel";
  panel.innerHTML = "";
  if (view === "ready") return;
  panel.classList.add("visible");
  if (view === "loading") {
    panel.innerHTML = "<strong>Loading dashboard data...</strong><div class=\"skeleton\"></div>";
  } else if (view === "empty") {
    panel.innerHTML = `<strong>No predictions yet.</strong> Try a wider date scope or wait for the next Cloud Scheduler run. <button type="button" id="emptyRefresh">Refresh</button>`;
    document.getElementById("emptyRefresh").addEventListener("click", () => load(true));
  } else if (view === "error") {
    panel.innerHTML = `<strong>Dashboard error.</strong> ${esc(detail)} <button type="button" id="retryLoad">Retry</button>`;
    document.getElementById("retryLoad").addEventListener("click", () => load(true));
  }
}

function renderMetrics() {
  const rows = aggregateByHorizon(selectedCohortRows());
  document.getElementById("metrics").innerHTML = rows.map(r => {
    const proxy = r.proxyLabels.length ? `<span class="proxy-pill" title="${esc(r.proxyLabels.join(", "))}">proxy</span>` : "";
    const observed = r.count > 0 && r.actualCount === r.count;
    const range = observed
      ? `<div class="range observed">observed from MMP</div>`
      : `<div class="range">${pct(r.lowRoas)} - ${pct(r.highRoas)}</div>`;
    return `<div class="metric">
      <div class="label">D${r.horizon} ROAS ${proxy}</div>
      <div class="value">${pct(r.roas)}</div>
      ${range}
      <div class="muted">pLTV ${ltv(r.predictedLtv)} / network install · conf ${r.confidence == null ? "n/a" : Math.round(r.confidence * 100)}</div>
    </div>`;
  }).join("");
}

function latestCohortRows(items) {
  if (!items.length) return [];
  const latest = items.reduce((max, p) => String(p.cohort_end || p.cohort_start) > max ? String(p.cohort_end || p.cohort_start) : max, "");
  return items.filter(p => String(p.cohort_end || p.cohort_start) === latest);
}

function selectedCohortRows() {
  return latestCohortRows(filteredPredictions());
}

function selectedRetentionRows() {
  return latestCohortRows(filteredRetentionPredictions());
}

function summarizeRows(items) {
  const cost = items.reduce((sum, p) => sum + Number(p.cost || 0), 0);
  const installs = items.reduce((sum, p) => sum + Number(p.network_installs || 0), 0);
  const revenue = items.reduce((sum, p) => sum + revenueValue(p), 0);
  const lowRevenue = items.reduce((sum, p) => sum + Number(p.low_roas || 0) * Number(p.cost || 0), 0);
  const highRevenue = items.reduce((sum, p) => sum + Number(p.high_roas || 0) * Number(p.cost || 0), 0);
  const actualCount = items.filter(p => p.roas_source === "actual").length;
  const confidence = items
    .filter(p => p.confidence_score != null)
    .reduce((sum, p, _i, arr) => sum + Number(p.confidence_score || 0) / arr.length, 0);
  return {
    cost,
    installs,
    roas: cost ? revenue / cost : 0,
    ltv: installs ? revenue / installs : null,
    low: cost ? lowRevenue / cost : 0,
    high: cost ? highRevenue / cost : 0,
    actualCount,
    confidence,
    count: items.length
  };
}

function renderChannelOverview() {
  const items = filteredPredictions();
  const grouped = new Map();
  for (const p of items) grouped.set(sourceName(p), [...(grouped.get(sourceName(p)) || []), p]);
  const channels = [...grouped.entries()].map(([source, rows]) => {
    const latestRows = latestCohortRows(rows);
    const horizons = new Map(aggregateByHorizon(latestRows).map(row => [Number(row.horizon), row]));
    const latestStart = latestRows[0] ? latestRows[0].cohort_start : "";
    const latestEnd = latestRows[0] ? latestRows[0].cohort_end : "";
    const cost = [...horizons.values()][0]?.cost || rows.reduce((sum, p) => sum + Number(p.cost || 0), 0);
    return {source, latestStart, latestEnd, cost, horizons, rows: latestRows.length, status: "paid"};
  }).sort((a, b) => b.cost - a.cost);
  const presentSources = sourcePresenceRows();
  const seen = new Set(channels.map(channel => channel.source));
  for (const source of presentSources) {
    if (seen.has(source.source)) continue;
    if (excludedCampaignSet.size && Number(source.cost || 0) > 0) continue;
    channels.push({
      source: source.source,
      latestStart: source.cohort_start || state?.data_scope?.requested_start || "",
      latestEnd: source.cohort_end || state?.data_scope?.requested_end || "",
      cost: Number(source.cost || 0),
      horizons: new Map(),
      rows: Number(source.rows || 0),
      status: source.status || "zero_spend",
      campaigns: Number(source.campaigns || 0)
    });
  }
  channels.sort((a, b) => b.cost - a.cost || a.source.localeCompare(b.source));

  const note = dataScopeNote();
  document.getElementById("overviewSummary").textContent = `${channels.length} sources${note ? ` · ${note}` : ""}`;
  document.getElementById("channelOverview").innerHTML = channels.map(channel => {
    const metrics = overviewHorizons.map(horizon => {
      const row = channel.horizons.get(horizon);
      if (!row) return `<div class="overview-metric muted"><span>D${horizon}</span><strong>n/a</strong></div>`;
      const source = row.actualCount === row.count ? "actual" : row.proxyCount === row.count ? "proxy" : row.actualCount > 0 || row.proxyCount > 0 ? "mixed" : "pred";
      const detail = source === "actual"
        ? `observed from MMP · pLTV ${ltv(row.predictedLtv)}`
        : `${pct(row.lowRoas)} - ${pct(row.highRoas)} · pLTV ${ltv(row.predictedLtv)}`;
      return `<div class="overview-metric">
        <span>D${horizon} <em>${source}</em></span>
        <strong>${pct(row.roas)}</strong>
        <small>${detail}</small>
      </div>`;
    }).join("");
    const status = channel.status === "zero_spend" ? "0 spend in selected period" :
      channel.status === "below_minimum_spend" ? `Below ${money(state?.minimum_cost || 0)} minimum spend` :
      channel.status === "paid" ? "" : esc(channel.status || "");
    const action = channel.cost > 0 ? `<button type="button" class="secondary view-campaigns" data-source="${esc(channel.source)}">Campaigns</button>` : `<span class="proxy-pill">0 spend</span>`;
    return `<article class="channel-card" data-source="${esc(channel.source)}">
      <div class="channel-head">
        <div>
          <button type="button" class="channel-source" data-source="${esc(channel.source)}">${esc(channel.source)}</button>
          <div class="muted">${esc(channel.latestStart)} - ${esc(channel.latestEnd)} · ${money(channel.cost)}${status ? ` · ${status}` : ""}</div>
        </div>
        ${action}
      </div>
      <div class="overview-grid">${metrics}</div>
    </article>`;
  }).join("");
  document.querySelectorAll(".channel-source, .view-campaigns").forEach(button => {
    button.addEventListener("click", event => {
      const source = event.currentTarget.dataset.source || "";
      document.getElementById("partnerFilter").value = source;
      page = 1;
      render();
      setActiveTab("campaigns");
      document.querySelector(".table-section")?.scrollIntoView({behavior: "smooth", block: "start"});
    });
  });
}

function renderCohortContext() {
  const allRows = filteredPredictions();
  const latestRows = selectedCohortRows();
  const f = selected();
  const scope = state?.data_scope || {};
  const cohortStart = latestRows[0]?.cohort_start || "n/a";
  const cohortEnd = latestRows[0]?.cohort_end || "n/a";
  const campaignCount = new Set(latestRows.map(p => p.campaign_network)).size;
  const sourceCount = sourcePresenceRows().length || new Set(allRows.map(sourceName)).size;
  const actualCount = latestRows.filter(p => p.roas_source === "actual").length;
  const proxyCount = latestRows.filter(p => p.roas_source === "proxy").length;
  const predCount = latestRows.length - actualCount - proxyCount;
  const anchors = [...new Set(latestRows.map(p => `D${p.anchor_day}`))].sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
  const groups = [...new Set(latestRows.map(p => p.model_group).filter(Boolean))].sort();
  const filters = [
    f.platform || "All platforms",
    f.country ? `Country ${f.country}` : "All countries",
    f.partner || "All sources",
    f.campaign || "All campaigns"
  ];
  if (excludedCampaignSet.size) filters.push(`excluding ${excludedCampaignSet.size} campaigns`);
  const cost = latestRows
    .filter((p, index, rows) => rows.findIndex(other =>
      other.cohort_start === p.cohort_start &&
      other.cohort_end === p.cohort_end &&
      other.campaign_network === p.campaign_network &&
      other.country_code === p.country_code &&
      sourceName(other) === sourceName(p)
    ) === index)
    .reduce((sum, p) => sum + Number(p.cost || 0), 0);

  document.getElementById("contextSummary").textContent = `${cohortStart} - ${cohortEnd}`;
  document.getElementById("cohortContext").innerHTML = `
    <div class="context-item">
      <span>Cohort window</span>
      <strong>${esc(cohortStart)} - ${esc(cohortEnd)}</strong>
      <small>${esc(scopeLabel(f.scope))} · ${campaignCount} campaigns · ${money(cost)}</small>
    </div>
    <div class="context-item">
      <span>Traffic slice</span>
      <strong>${esc(filters.join(" · "))}</strong>
      <small>${sourceCount} sources in current result · ${allRows.length} horizon rows</small>
    </div>
    <div class="context-item">
      <span>Prediction inputs</span>
      <strong>${state.cohort_weeks || 0} weekly training cohorts</strong>
      <small>${state.row_count || 0} paid training rows · anchors ${anchors.join(", ") || "n/a"} · groups ${groups.join(", ") || "n/a"}${scope.fallback ? ` · ${esc(dataScopeNote())}` : ""}</small>
    </div>
    <div class="context-item">
      <span>Metric status</span>
      <strong>${actualCount} actual · ${proxyCount} proxy · ${predCount} predicted</strong>
      <small>Cards and charts use this selected latest cohort only</small>
    </div>`;
}

function interpolateChartRow(rows, day, keys) {
  const sorted = [...rows].sort((a, b) => Number(a.horizon) - Number(b.horizon));
  if (!sorted.length) return null;
  if (day <= Number(sorted[0].horizon)) return {row: sorted[0], exact: day === Number(sorted[0].horizon), from: sorted[0], to: sorted[0]};
  const last = sorted[sorted.length - 1];
  if (day >= Number(last.horizon)) return {row: last, exact: day === Number(last.horizon), from: last, to: last};
  for (let i = 0; i < sorted.length - 1; i += 1) {
    const from = sorted[i];
    const to = sorted[i + 1];
    const fromDay = Number(from.horizon);
    const toDay = Number(to.horizon);
    if (day < fromDay || day > toDay) continue;
    const ratio = toDay === fromDay ? 0 : (day - fromDay) / (toDay - fromDay);
    const row = {horizon: day};
    for (const key of keys) {
      const fromValue = Number(from[key] ?? 0);
      const toValue = Number(to[key] ?? fromValue);
      row[key] = fromValue + (toValue - fromValue) * ratio;
    }
    return {row, exact: day === fromDay || day === toDay, from, to};
  }
  return {row: last, exact: false, from: last, to: last};
}

function tooltipRange(label, low, high, formatter) {
  if (low == null || high == null) return "";
  return `<div>${esc(label)}: ${formatter(low)} - ${formatter(high)}</div>`;
}

function hideChartTooltip(svg) {
  const tooltip = svg.parentElement?.querySelector(".chart-tooltip");
  if (tooltip) tooltip.hidden = true;
  const hover = svg.querySelector(".hover-layer");
  if (hover) hover.setAttribute("visibility", "hidden");
}

function attachChartTooltip(svg, rows, config) {
  const parent = svg.parentElement;
  if (!parent || !rows.length) {
    hideChartTooltip(svg);
    return;
  }
  let tooltip = parent.querySelector(".chart-tooltip");
  if (!tooltip) {
    tooltip = document.createElement("div");
    tooltip.className = "chart-tooltip";
    tooltip.hidden = true;
    parent.appendChild(tooltip);
  }
  const {width, height, left, right, top, bottom, maxHorizon, maxY, valueKey, lowKey, highKey, formatter, title, color} = config;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const keys = [valueKey, lowKey, highKey].filter(Boolean);
  const x = h => left + (Number(h) / maxHorizon) * plotWidth;
  const y = v => height - bottom - (Number(v || 0) / maxY) * plotHeight;
  const hover = svg.querySelector(".hover-layer");
  const line = svg.querySelector(".hover-line");
  const dot = svg.querySelector(".hover-dot");

  svg.onpointerleave = () => hideChartTooltip(svg);
  svg.onpointermove = event => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const svgX = (event.clientX - rect.left) * (width / rect.width);
    if (svgX < left || svgX > width - right) {
      hideChartTooltip(svg);
      return;
    }
    const day = Math.max(1, Math.min(maxHorizon, Math.round(((svgX - left) / plotWidth) * maxHorizon)));
    const interpolated = interpolateChartRow(rows, day, keys);
    if (!interpolated) {
      hideChartTooltip(svg);
      return;
    }
    const row = interpolated.row;
    const cx = x(day);
    const cy = y(row[valueKey]);
    if (hover) hover.setAttribute("visibility", "visible");
    if (line) {
      line.setAttribute("x1", cx);
      line.setAttribute("x2", cx);
    }
    if (dot) {
      dot.setAttribute("cx", cx);
      dot.setAttribute("cy", cy);
      dot.setAttribute("fill", color);
    }
    const anchorNote = interpolated.exact || interpolated.from === interpolated.to
      ? ""
      : `<div class="muted">Interpolated D${interpolated.from.horizon} - D${interpolated.to.horizon}</div>`;
    tooltip.innerHTML = `
      <strong>D${day} ${esc(title)}</strong>
      <div>${formatter(row[valueKey])}</div>
      ${tooltipRange("Range", row[lowKey], row[highKey], formatter)}
      ${anchorNote}
    `;
    tooltip.hidden = false;
    const leftPx = (cx / width) * rect.width;
    const topPx = (cy / height) * rect.height;
    const maxLeft = Math.max(8, rect.width - 190);
    const maxTop = Math.max(8, rect.height - 98);
    tooltip.style.left = `${Math.min(maxLeft, Math.max(8, leftPx + 12))}px`;
    tooltip.style.top = `${Math.min(maxTop, Math.max(8, topPx - 42))}px`;
  };
}

function renderCurve() {
  const rows = aggregateByHorizon(selectedCohortRows());
  const svg = document.getElementById("curve");
  const width = 840, height = 360, left = 58, right = 22, top = 20, bottom = 44;
  if (!rows.length) {
    svg.innerHTML = `<rect x="0" y="0" width="${width}" height="${height}" fill="white"/><text x="24" y="48" fill="#697586">No data</text>`;
    hideChartTooltip(svg);
    return;
  }
  const maxY = Math.max(0.1, ...rows.map(r => r.highRoas)) * 1.18;
  const maxHorizon = Math.max(180, ...rows.map(r => r.horizon));
  const x = h => left + (h / maxHorizon) * (width - left - right);
  const y = v => height - bottom - (v / maxY) * (height - top - bottom);
  const line = key => rows.map(r => `${x(r.horizon)},${y(r[key])}`).join(" ");
  const band = rows.map(r => `${x(r.horizon)},${y(r.highRoas)}`).join(" ") + " " +
    [...rows].reverse().map(r => `${x(r.horizon)},${y(r.lowRoas)}`).join(" ");
  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="white"/>
    ${[0, .25, .5, .75, 1].map(t => `<line x1="${left}" y1="${top + t*(height-top-bottom)}" x2="${width-right}" y2="${top + t*(height-top-bottom)}" stroke="#e5e9f0"/><text x="8" y="${top + t*(height-top-bottom)+4}" fill="#697586" font-size="12">${pct(maxY*(1-t))}</text>`).join("")}
    <polygon points="${band}" fill="rgba(47,111,237,0.16)"/>
    <polyline points="${line("roas")}" fill="none" stroke="#2f6fed" stroke-width="3"/>
    ${rows.map(r => `<circle cx="${x(r.horizon)}" cy="${y(r.roas)}" r="4" fill="#2f6fed"/><text x="${x(r.horizon)-12}" y="${height-16}" fill="#697586" font-size="12">D${r.horizon}</text>`).join("")}
    <g class="hover-layer" visibility="hidden" pointer-events="none">
      <line class="hover-line" x1="${left}" x2="${left}" y1="${top}" y2="${height-bottom}" stroke="#17202a" stroke-width="1" stroke-dasharray="4 4" opacity="0.55"/>
      <circle class="hover-dot" cx="${left}" cy="${height-bottom}" r="5" fill="#2f6fed" stroke="white" stroke-width="2"/>
    </g>
  `;
  attachChartTooltip(svg, rows, {width, height, left, right, top, bottom, maxHorizon, maxY, valueKey: "roas", lowKey: "lowRoas", highKey: "highRoas", formatter: pct, title: "pROAS", color: "#2f6fed"});
}

function renderLtvCurve() {
  const rows = aggregateByHorizon(selectedCohortRows()).filter(r => r.predictedLtv != null);
  const svg = document.getElementById("ltvCurve");
  const width = 840, height = 360, left = 70, right = 22, top = 20, bottom = 44;
  if (!rows.length) {
    svg.innerHTML = `<rect x="0" y="0" width="${width}" height="${height}" fill="white"/><text x="24" y="48" fill="#697586">No data</text>`;
    hideChartTooltip(svg);
    return;
  }
  const maxY = Math.max(0.01, ...rows.map(r => Number(r.highLtv ?? r.predictedLtv ?? 0))) * 1.18;
  const maxHorizon = Math.max(180, ...rows.map(r => r.horizon));
  const x = h => left + (h / maxHorizon) * (width - left - right);
  const y = v => height - bottom - (v / maxY) * (height - top - bottom);
  const line = key => rows.map(r => `${x(r.horizon)},${y(Number(r[key] ?? 0))}`).join(" ");
  const band = rows.map(r => `${x(r.horizon)},${y(Number(r.highLtv ?? r.predictedLtv ?? 0))}`).join(" ") + " " +
    [...rows].reverse().map(r => `${x(r.horizon)},${y(Number(r.lowLtv ?? r.predictedLtv ?? 0))}`).join(" ");
  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="white"/>
    ${[0, .25, .5, .75, 1].map(t => `<line x1="${left}" y1="${top + t*(height-top-bottom)}" x2="${width-right}" y2="${top + t*(height-top-bottom)}" stroke="#e5e9f0"/><text x="8" y="${top + t*(height-top-bottom)+4}" fill="#697586" font-size="12">${ltv(maxY*(1-t))}</text>`).join("")}
    <polygon points="${band}" fill="rgba(31,157,104,0.14)"/>
    <polyline points="${line("predictedLtv")}" fill="none" stroke="#1f9d68" stroke-width="3"/>
    ${rows.map(r => `<circle cx="${x(r.horizon)}" cy="${y(Number(r.predictedLtv || 0))}" r="4" fill="#1f9d68"/><text x="${x(r.horizon)-12}" y="${height-16}" fill="#697586" font-size="12">D${r.horizon}</text>`).join("")}
    <g class="hover-layer" visibility="hidden" pointer-events="none">
      <line class="hover-line" x1="${left}" x2="${left}" y1="${top}" y2="${height-bottom}" stroke="#17202a" stroke-width="1" stroke-dasharray="4 4" opacity="0.55"/>
      <circle class="hover-dot" cx="${left}" cy="${height-bottom}" r="5" fill="#1f9d68" stroke="white" stroke-width="2"/>
    </g>
  `;
  attachChartTooltip(svg, rows, {width, height, left, right, top, bottom, maxHorizon, maxY, valueKey: "predictedLtv", lowKey: "lowLtv", highKey: "highLtv", formatter: ltv, title: "pLTV", color: "#1f9d68"});
}

function renderRetentionCurve() {
  const rows = aggregateRetentionByHorizon(selectedRetentionRows());
  const svg = document.getElementById("retentionCurve");
  const width = 840, height = 360, left = 58, right = 22, top = 20, bottom = 44;
  if (!rows.length) {
    svg.innerHTML = `<rect x="0" y="0" width="${width}" height="${height}" fill="white"/><text x="24" y="48" fill="#697586">No retention data</text>`;
    hideChartTooltip(svg);
    return;
  }
  const maxY = Math.max(0.02, ...rows.map(r => r.highRetention)) * 1.12;
  const maxHorizon = Math.max(180, ...rows.map(r => r.horizon));
  const x = h => left + (h / maxHorizon) * (width - left - right);
  const y = v => height - bottom - (v / maxY) * (height - top - bottom);
  const line = key => rows.map(r => `${x(r.horizon)},${y(Number(r[key] ?? 0))}`).join(" ");
  const band = rows.map(r => `${x(r.horizon)},${y(Number(r.highRetention || 0))}`).join(" ") + " " +
    [...rows].reverse().map(r => `${x(r.horizon)},${y(Number(r.lowRetention || 0))}`).join(" ");
  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="white"/>
    ${[0, .25, .5, .75, 1].map(t => `<line x1="${left}" y1="${top + t*(height-top-bottom)}" x2="${width-right}" y2="${top + t*(height-top-bottom)}" stroke="#e5e9f0"/><text x="8" y="${top + t*(height-top-bottom)+4}" fill="#697586" font-size="12">${retentionPct(maxY*(1-t))}</text>`).join("")}
    <polygon points="${band}" fill="rgba(183,121,31,0.14)"/>
    <polyline points="${line("retention")}" fill="none" stroke="#b7791f" stroke-width="3"/>
    ${rows.map(r => `<circle cx="${x(r.horizon)}" cy="${y(Number(r.retention || 0))}" r="4" fill="#b7791f"/><text x="${x(r.horizon)-12}" y="${height-16}" fill="#697586" font-size="12">D${r.horizon}</text>`).join("")}
    <g class="hover-layer" visibility="hidden" pointer-events="none">
      <line class="hover-line" x1="${left}" x2="${left}" y1="${top}" y2="${height-bottom}" stroke="#17202a" stroke-width="1" stroke-dasharray="4 4" opacity="0.55"/>
      <circle class="hover-dot" cx="${left}" cy="${height-bottom}" r="5" fill="#b7791f" stroke="white" stroke-width="2"/>
    </g>
  `;
  attachChartTooltip(svg, rows, {width, height, left, right, top, bottom, maxHorizon, maxY, valueKey: "retention", lowKey: "lowRetention", highKey: "highRetention", formatter: retentionPct, title: "pRetention", color: "#b7791f"});
}

function renderQuality() {
  const rows = aggregateByHorizon(selectedCohortRows());
  const svg = document.getElementById("quality");
  const width = 840, height = 320, left = 52, bottom = 42, top = 20;
  if (!rows.length) {
    svg.innerHTML = `<rect x="0" y="0" width="${width}" height="${height}" fill="white"/><text x="24" y="48" fill="#697586">No data</text>`;
    return;
  }
  const maxErr = Math.max(0.2, ...rows.map(r => r.error || 0)) * 1.25;
  const barW = 54;
  svg.innerHTML = `<rect x="0" y="0" width="${width}" height="${height}" fill="white"/>` +
    rows.map((r, i) => {
      const x = left + i * 88;
      const h = ((r.error || 0) / maxErr) * (height - top - bottom);
      const y = height - bottom - h;
      const color = (r.error || 0) < 0.25 ? "#1f9d68" : (r.error || 0) < 0.55 ? "#b7791f" : "#c44949";
      return `<rect x="${x}" y="${y}" width="${barW}" height="${h}" fill="${color}"/>
        <text x="${x + barW/2}" y="${height - 16}" text-anchor="middle" fill="#697586" font-size="12">D${r.horizon}</text>
        <text x="${x + barW/2}" y="${Math.max(14, y - 7)}" text-anchor="middle" fill="#17202a" font-size="12">${r.error == null ? "n/a" : pct(r.error)}</text>`;
    }).join("");
}

function modelLabel(model) {
  return String(model || "").replace(/_v\d+$/, "").replace(/_/g, " ");
}

function summaryForModel(payload, model, horizon) {
  return (payload.summary_by_model_horizon?.[model] || {})[String(horizon)] ||
    (payload.summary_by_model_horizon?.[model] || {})[Number(horizon)] || {};
}

function comparisonForModel(payload, model, horizon) {
  return (payload.comparison?.[model] || {})[String(horizon)] ||
    (payload.comparison?.[model] || {})[Number(horizon)] || {};
}

function renderBacktestError(detail) {
  document.getElementById("backtestSummaryText").textContent = `Backtest error: ${detail}`;
  document.getElementById("backtestSummaryRows").innerHTML = "";
  document.getElementById("backtestRows").innerHTML = "";
  document.getElementById("backtestRowsSummary").textContent = "";
  document.getElementById("retentionBacktestSummaryText").textContent = "";
  document.getElementById("retentionBacktestSummaryRows").innerHTML = "";
  document.getElementById("retentionBacktestRows").innerHTML = "";
  document.getElementById("retentionBacktestRowsSummary").textContent = "";
}

function renderBacktest() {
  if (!backtestState) {
    document.getElementById("backtestSummaryText").textContent = "Loading backtest...";
    return;
  }
  const payload = backtestState;
  const models = payload.models || [payload.baseline_model || payload.model].filter(Boolean);
  const horizons = (payload.horizons || []).map(Number);
  const summaryRows = [];
  for (const model of models) {
    for (const horizon of horizons) {
      const row = summaryForModel(payload, model, horizon);
      const comp = comparisonForModel(payload, model, horizon);
      summaryRows.push({model, horizon, ...row, weighted_mape_delta: comp.weighted_mape_delta});
    }
  }
  document.getElementById("backtestSummaryText").textContent =
    `${(payload.rows || []).length} prediction rows · generated ${payload.generated_at || "n/a"}`;
  document.getElementById("backtestSummaryRows").innerHTML = summaryRows.map(row => `
    <tr>
      <td>${esc(modelLabel(row.model))}${row.model === payload.baseline_model ? " <span class=\"proxy-pill\">prod baseline</span>" : ""}</td>
      <td>H${esc(row.horizon)}</td>
      <td class="num">${Number(row.count || 0).toLocaleString()}</td>
      <td class="num">${row.actual_roas == null ? "n/a" : pct(row.actual_roas)}</td>
      <td class="num">${row.predicted_roas == null ? "n/a" : pct(row.predicted_roas)}</td>
      <td class="num">${row.weighted_mape == null ? "n/a" : pct(row.weighted_mape)}</td>
      <td class="num">${row.median_ape == null ? "n/a" : pct(row.median_ape)}</td>
      <td class="num">${row.coverage == null ? "n/a" : pct(row.coverage)}</td>
      <td class="num">${row.weighted_mape_delta == null ? "baseline" : pct(row.weighted_mape_delta)}</td>
    </tr>
  `).join("");

  const rows = (payload.rows || []).slice(0, 500);
  document.getElementById("backtestRowsSummary").textContent = `Showing ${rows.length} of ${(payload.rows || []).length} rows`;
  document.getElementById("backtestRows").innerHTML = rows.map(row => `
    <tr>
      <td>${esc(modelLabel(row.model))}</td>
      <td>${esc(row.cohort_start)}<div class="cell-sub">${esc(row.cohort_end)}</div></td>
      <td>${esc(row.platform)}</td>
      <td>${row.country_code === "ZZ" ? esc(row.country) : `${esc(row.country)} (${esc(row.country_code)})`}</td>
      <td>${esc(row.source_channel || row.partner_name || "")}</td>
      <td title="${esc(row.campaign_network)}">${esc(row.campaign_network)}</td>
      <td class="num">${money(row.cost)}</td>
      <td class="num">H${esc(row.horizon)}</td>
      <td class="num">${pct(row.actual_roas)}</td>
      <td class="num">${pct(row.predicted_roas)}</td>
      <td class="num">${pct(row.ape)}</td>
      <td>${esc(row.model_group)}<div class="cell-sub">n=${esc(row.sample_size)}</div></td>
    </tr>
  `).join("");

  const retentionHorizons = (payload.retention_horizons || []).map(Number);
  const retentionSummary = payload.retention_summary_by_horizon || {};
  const retentionSummaryRows = retentionHorizons.map(horizon => ({
    horizon,
    ...(retentionSummary[String(horizon)] || retentionSummary[horizon] || {})
  }));
  document.getElementById("retentionBacktestSummaryText").textContent =
    `${(payload.retention_rows || []).length} retention prediction rows`;
  document.getElementById("retentionBacktestSummaryRows").innerHTML = retentionSummaryRows.map(row => `
    <tr>
      <td>${esc(modelLabel(payload.retention_model || "retention_multiplier_v1"))}</td>
      <td>H${esc(row.horizon)}</td>
      <td class="num">${Number(row.count || 0).toLocaleString()}</td>
      <td class="num">${row.actual_retention == null ? "n/a" : retentionPct(row.actual_retention)}</td>
      <td class="num">${row.predicted_retention == null ? "n/a" : retentionPct(row.predicted_retention)}</td>
      <td class="num">${row.weighted_mape == null ? "n/a" : pct(row.weighted_mape)}</td>
      <td class="num">${row.median_ape == null ? "n/a" : pct(row.median_ape)}</td>
      <td class="num">${row.coverage == null ? "n/a" : pct(row.coverage)}</td>
    </tr>
  `).join("");
  const retentionRows = (payload.retention_rows || []).slice(0, 500);
  document.getElementById("retentionBacktestRowsSummary").textContent =
    `Showing ${retentionRows.length} of ${(payload.retention_rows || []).length} rows`;
  document.getElementById("retentionBacktestRows").innerHTML = retentionRows.map(row => `
    <tr>
      <td>${esc(row.cohort_start)}<div class="cell-sub">${esc(row.cohort_end)}</div></td>
      <td>${esc(row.platform)}</td>
      <td>${row.country_code === "ZZ" ? esc(row.country) : `${esc(row.country)} (${esc(row.country_code)})`}</td>
      <td>${esc(row.source_channel || row.partner_name || "")}</td>
      <td title="${esc(row.campaign_network)}">${esc(row.campaign_network)}</td>
      <td class="num">${Number(row.network_installs || 0).toLocaleString()}</td>
      <td class="num">H${esc(row.horizon)}</td>
      <td class="num">${retentionPct(row.actual_retention)}</td>
      <td class="num">${retentionPct(row.predicted_retention)}</td>
      <td class="num">${pct(row.ape)}</td>
      <td>${esc(row.model_group)}<div class="cell-sub">n=${esc(row.sample_size)}</div></td>
    </tr>
  `).join("");
}

async function loadBacktest(force = false) {
  if (backtestPromise) return backtestPromise;
  backtestPromise = (async () => {
    const cached = force ? null : readBacktestCache();
    if (cached) {
      backtestState = cached;
      renderBacktest();
    } else {
      document.getElementById("backtestSummaryText").textContent = "Loading backtest...";
    }
    const res = await fetch("/api/backtest", {cache: "no-store"});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || "Backtest API error");
    backtestState = payload;
    writeBacktestCache(payload);
    renderBacktest();
  })();
  try {
    return await backtestPromise;
  } finally {
    backtestPromise = null;
  }
}

function tableGroups() {
  const grouped = new Map();
  for (const p of filteredPredictions()) {
    const key = [
      p.cohort_start,
      p.cohort_end,
      p.platform,
      p.country_code,
      sourceName(p),
      p.campaign_network,
      p.campaign_id_network
    ].join("\u001f");
    const row = grouped.get(key) || {
      cohort_start: p.cohort_start,
      cohort_end: p.cohort_end,
      platform: p.platform,
      country: p.country,
      country_code: p.country_code,
      source: sourceName(p),
      partner_name: p.partner_name,
      campaign: p.campaign_network,
      campaign_id: p.campaign_id_network,
      cost: Number(p.cost || 0),
      horizons: new Map(),
      confidences: [],
      errors: [],
      samples: 0
    };
    row.cost = Math.max(Number(row.cost || 0), Number(p.cost || 0));
    row.horizons.set(Number(p.horizon), p);
    if (p.confidence_score != null) row.confidences.push(Number(p.confidence_score));
    if (p.error_mape != null) row.errors.push(Number(p.error_mape));
    row.samples = Math.max(Number(row.samples || 0), Number(p.sample_size || 0));
    grouped.set(key, row);
  }
  return [...grouped.values()].map(row => ({
    ...row,
    confidence: row.confidences.length ? row.confidences.reduce((sum, value) => sum + value, 0) / row.confidences.length : 0,
    error: row.errors.length ? row.errors.sort((a, b) => a - b)[Math.floor(row.errors.length / 2)] : null
  }));
}

function sortedItems() {
  const direction = sortDir === "asc" ? 1 : -1;
  return tableGroups().sort((a, b) => {
    const horizon = String(sortKey).startsWith("h_") ? horizonFromSortKey(sortKey) : null;
    const av = horizon ? roasValue(a.horizons.get(horizon) || {}) : sortKey === "quality" ? a.confidence : a[sortKey] ?? "";
    const bv = horizon ? roasValue(b.horizons.get(horizon) || {}) : sortKey === "quality" ? b.confidence : b[sortKey] ?? "";
    if (typeof av === "number" || typeof bv === "number") return (Number(av || 0) - Number(bv || 0)) * direction;
    return String(av).localeCompare(String(bv)) * direction;
  });
}

function horizonCell(row, horizon) {
  const p = row.horizons.get(horizon);
  if (!p) return `<td class="num horizon-cell empty">-</td>`;
  const proxy = proxyLabel(p);
  const badge = p.roas_source === "actual" ? "actual" : p.roas_source === "proxy" ? "proxy" : "pred";
  const detail = badge === "actual"
    ? `<small>observed from MMP</small>`
    : `<small>${pct(p.low_roas)} - ${pct(p.high_roas)} · D${p.anchor_day}</small>`;
  return `<td class="num horizon-cell ${predictionQuality(p)}" title="${esc(proxy || `H${horizon}`)}">
    <strong>${pct(roasValue(p))}</strong>
    <small>${badge} · pLTV ${ltv(ltvValue(p))}</small>
    ${detail}
    ${proxy ? `<span class="proxy-pill">${esc(proxy)}</span>` : ""}
  </td>`;
}

function rowHtml(row) {
  const quality = qualityClass(row.confidence);
  const rawSource = row.partner_name && row.partner_name !== row.source ? `<div class="cell-sub" title="${esc(row.partner_name)}">${esc(row.partner_name)}</div>` : "";
  return `<tr>
    <td>${row.cohort_start}<div class="cell-sub">${row.cohort_end}</div></td>
    <td>${esc(row.platform)}</td>
    <td>${row.country_code === "ZZ" ? esc(row.country) : `${esc(row.country)} (${esc(row.country_code)})`}</td>
    <td>${esc(row.source)}${rawSource}</td>
    <td title="${esc(row.campaign)}">${esc(row.campaign)}</td>
    <td class="num">${money(row.cost)}</td>
    ${overviewHorizons.map(horizon => horizonCell(row, horizon)).join("")}
    <td><span class="quality-pill ${quality}">${esc(qualityText(row.confidence))} ${Math.round(row.confidence * 100)} · err ${row.error == null ? "n/a" : pct(row.error)} · n=${row.samples}</span></td>
  </tr>`;
}

function cardHtml(row) {
  const metrics = overviewHorizons.map(horizon => {
    const p = row.horizons.get(horizon);
    return `<div class="card-row"><span>H${horizon}</span><strong>${p ? pct(roasValue(p)) : "n/a"}</strong></div>`;
  }).join("");
  return `<article class="card">
    <strong>${esc(row.campaign)}</strong>
    <div class="muted">${esc(row.platform)} · ${esc(row.country)} · ${esc(row.source)} · ${row.cohort_start}</div>
    ${metrics}
    <div class="card-row"><span>Cost</span><span>${money(row.cost)}</span></div>
  </article>`;
}

function renderTable() {
  const items = sortedItems();
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  page = Math.min(page, pages);
  const visible = items.slice((page - 1) * pageSize, page * pageSize);
  document.getElementById("rows").innerHTML = visible.map(rowHtml).join("");
  document.getElementById("cards").innerHTML = visible.map(cardHtml).join("");
  const horizonRows = filteredPredictions().length;
  document.getElementById("pageSummary").textContent = `${items.length} cohorts · ${horizonRows} horizon rows`;
  document.getElementById("pageStatus").textContent = `Page ${page} of ${pages} · ${items.length} cohorts`;
  document.getElementById("prevPage").disabled = page <= 1;
  document.getElementById("nextPage").disabled = page >= pages;
}

function populateFilters() {
  const f = selected();
  const country = document.getElementById("countryFilter");
  const partner = document.getElementById("partnerFilter");
  const campaign = document.getElementById("campaignFilter");
  const base = state.predictions.filter(p => !f.platform || p.platform === f.platform);
  const countries = [...new Map(base.map(p => [p.country_code, p.country])).entries()]
    .sort((a, b) => a[1].localeCompare(b[1]));
  country.innerHTML = `<option value="">All countries</option>` + countries.map(([code, name]) => `<option value="${esc(code)}">${esc(name)} (${esc(code)})</option>`).join("");
  const wantedCountry = f.country || country.dataset.pendingValue || "";
  country.value = countries.some(([code]) => code === wantedCountry) ? wantedCountry : "";
  delete country.dataset.pendingValue;
  const countryFiltered = base.filter(p => !country.value || p.country_code === country.value);
  const partners = [...new Set(countryFiltered.map(sourceName))].sort();
  partner.innerHTML = `<option value="">All sources</option>` + partners.map(p => `<option>${esc(p)}</option>`).join("");
  const wantedPartner = f.partner || partner.dataset.pendingValue || "";
  partner.value = partners.includes(wantedPartner) ? wantedPartner : "";
  delete partner.dataset.pendingValue;
  const partnerFiltered = countryFiltered.filter(p => !partner.value || sourceName(p) === partner.value);
  const campaigns = [...new Set(partnerFiltered.map(p => p.campaign_network))].sort();
  campaign.innerHTML = `<option value="">All campaigns</option>` + campaigns.map(p => `<option>${esc(p)}</option>`).join("");
  const wantedCampaign = f.campaign || campaign.dataset.pendingValue || "";
  campaign.value = campaigns.includes(wantedCampaign) ? wantedCampaign : "";
  delete campaign.dataset.pendingValue;
}

function renderProxyDisclosure() {
  const labels = [...new Set((state.predictions || []).map(proxyLabel).filter(Boolean))];
  const note = "Bands show the 20th-80th percentile historical multiplier range for the selected traffic.";
  document.getElementById("curveNote").textContent = labels.length ? `${note} Proxy horizons: ${labels.join(", ")}.` : note;
}

function render() {
  const sync = state && state.latest_sync || {};
  if (state && state.status === "warming_up") {
    document.getElementById("syncStatus").textContent = `Preparing cohorts... ${state.row_count || 0} rows available.`;
  } else if (state) {
    document.getElementById("syncStatus").textContent = `${state.cohort_weeks || 0} weekly cohorts, ${state.row_count || 0} paid rows. Last sync: ${sync.finished_at || "n/a"} (${sync.status || "n/a"})`;
  }
  if (!state || !state.predictions || state.predictions.length === 0) {
    renderCampaignExclusions();
    document.getElementById("contextSummary").textContent = "No selected cohort";
    document.getElementById("cohortContext").innerHTML = "";
    document.getElementById("overviewSummary").textContent = "0 sources";
    document.getElementById("channelOverview").innerHTML = "";
    renderCurve();
    renderLtvCurve();
    renderRetentionCurve();
    renderQuality();
    document.getElementById("metrics").innerHTML = "";
    document.getElementById("rows").innerHTML = "";
    document.getElementById("cards").innerHTML = "";
    setView("empty");
    return;
  }
  setView("ready");
  populateFilters();
  renderCampaignExclusions();
  writeHash();
  latestSyncStamp = sync.finished_at || latestSyncStamp;
  renderProxyDisclosure();
  renderCohortContext();
  renderChannelOverview();
  renderMetrics();
  renderCurve();
  renderLtvCurve();
  renderRetentionCurve();
  renderQuality();
  renderTable();
}

async function load(force = false) {
  if (loadPromise) return loadPromise;
  loadPromise = (async () => {
    const params = summaryParams();
    const cacheKey = summaryCacheKey(params);
    const cached = force ? null : readSummaryCache(cacheKey);
    if (cached) {
      state = cached.payload;
      document.getElementById("syncStatus").textContent = "Showing cached dashboard data; refreshing in background...";
      render();
    } else {
      setView("loading");
      document.getElementById("syncStatus").textContent = "Loading dashboard data...";
    }
    try {
      const res = await fetch(`/api/summary?${params}`, {cache: "no-store"});
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || "API error");
      state = payload;
      writeSummaryCache(cacheKey, payload);
      render();
    } catch (err) {
      if (cached) {
        document.getElementById("syncStatus").textContent = `Showing cached dashboard data; refresh failed: ${err.message}`;
        return;
      }
      throw err;
    }
  })();
  try {
    return await loadPromise;
  } finally {
    loadPromise = null;
  }
}

async function pollStatus() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) return;
    const payload = await res.json();
    const badge = document.getElementById("syncBadge");
    if (payload.sync_in_progress) {
      const started = payload.sync_started_at ? Math.max(0, Math.round((Date.now() - Date.parse(payload.sync_started_at)) / 1000)) : 0;
      badge.hidden = false;
      badge.textContent = `Syncing... started ${started}s ago`;
    } else {
      badge.hidden = true;
      const stamp = payload.latest_sync && payload.latest_sync.finished_at;
      if (stamp && stamp !== latestSyncStamp) {
        latestSyncStamp = stamp;
        await load(true);
      }
    }
  } catch (_err) {
    return;
  }
}

function debounce(fn, delay) {
  let timer;
  return () => {
    clearTimeout(timer);
    timer = setTimeout(fn, delay);
  };
}

const debouncedClientRender = debounce(() => {
  page = 1;
  render();
}, 150);

function updateDateInputs() {
  const custom = document.getElementById("scopeFilter").value === "custom";
  document.getElementById("dateFrom").hidden = !custom;
  document.getElementById("dateTo").hidden = !custom;
}

function csvCell(value) {
  const text = String(value ?? "");
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, "\"\"")}"` : text;
}

function downloadFile(filename, content, type) {
  const blob = new Blob([content], {type});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function horizonExport(row, horizon) {
  const p = row.horizons.get(horizon);
  return {
    [`H${horizon} ROAS`]: p ? roasValue(p) : "",
    [`H${horizon} pLTV`]: p ? ltvValue(p) : "",
    [`H${horizon} low`]: p ? p.low_roas : "",
    [`H${horizon} high`]: p ? p.high_roas : "",
    [`H${horizon} status`]: p ? p.roas_source || "pred" : ""
  };
}

function campaignExportRows() {
  return tableGroups().map(row => ({
    cohort_start: row.cohort_start,
    cohort_end: row.cohort_end,
    platform: row.platform,
    country: row.country,
    country_code: row.country_code,
    source: row.source,
    partner_name: row.partner_name,
    campaign: row.campaign,
    campaign_id: row.campaign_id,
    cost: row.cost,
    confidence: row.confidence,
    error_mape: row.error,
    sample_size: row.samples,
    ...Object.assign({}, ...overviewHorizons.map(horizon => horizonExport(row, horizon)))
  }));
}

function overviewExportRows() {
  const grouped = new Map();
  for (const p of filteredPredictions()) grouped.set(sourceName(p), [...(grouped.get(sourceName(p)) || []), p]);
  return [...grouped.entries()].map(([source, rows]) => {
    const latestRows = latestCohortRows(rows);
    const aggregate = new Map(aggregateByHorizon(latestRows).map(row => [Number(row.horizon), row]));
    const exported = {
      source,
      cohort_start: latestRows[0]?.cohort_start || "",
      cohort_end: latestRows[0]?.cohort_end || "",
      cost: [...aggregate.values()][0]?.cost || 0,
      campaigns: new Set(latestRows.map(row => row.campaign_network)).size
    };
    for (const horizon of overviewHorizons) {
      const row = aggregate.get(horizon);
      exported[`H${horizon} ROAS`] = row ? row.roas : "";
      exported[`H${horizon} pLTV`] = row ? row.predictedLtv : "";
      exported[`H${horizon} low`] = row ? row.lowRoas : "";
      exported[`H${horizon} high`] = row ? row.highRoas : "";
    }
    return exported;
  });
}

function qualityExportRows() {
  return aggregateByHorizon(selectedCohortRows()).map(row => ({
    horizon: `H${row.horizon}`,
    roas: row.roas,
    low_roas: row.lowRoas,
    high_roas: row.highRoas,
    pLTV: row.predictedLtv,
    low_pLTV: row.lowLtv,
    high_pLTV: row.highLtv,
    error_mape: row.error,
    confidence: row.confidence,
    sample_size: row.samples
  }));
}

function backtestExportRows() {
  const roasRows = (backtestState?.rows || []).map(row => ({
    prediction_family: "pROAS",
    model: row.model,
    cohort_start: row.cohort_start,
    cohort_end: row.cohort_end,
    platform: row.platform,
    country: row.country,
    country_code: row.country_code,
    source: row.source_channel || row.partner_name,
    campaign: row.campaign_network,
    cost: row.cost,
    horizon: `H${row.horizon}`,
    anchor_day: row.anchor_day,
    anchor_roas: row.anchor_roas,
    actual_roas: row.actual_roas,
    predicted_roas: row.predicted_roas,
    low_roas: row.low_roas,
    high_roas: row.high_roas,
    ape: row.ape,
    covered: row.covered,
    sample_size: row.sample_size,
    model_group: row.model_group
  }));
  const retentionRows = (backtestState?.retention_rows || []).map(row => ({
    prediction_family: "pRetention",
    model: row.model,
    cohort_start: row.cohort_start,
    cohort_end: row.cohort_end,
    platform: row.platform,
    country: row.country,
    country_code: row.country_code,
    source: row.source_channel || row.partner_name,
    campaign: row.campaign_network,
    cost: row.cost,
    network_installs: row.network_installs,
    horizon: `H${row.horizon}`,
    anchor_day: row.anchor_day,
    anchor_retention: row.anchor_retention,
    actual_retention: row.actual_retention,
    predicted_retention: row.predicted_retention,
    low_retention: row.low_retention,
    high_retention: row.high_retention,
    ape: row.ape,
    covered: row.covered,
    sample_size: row.sample_size,
    model_group: row.model_group
  }));
  return [...roasRows, ...retentionRows];
}

function exportRows() {
  if (activeTab === "campaigns") return campaignExportRows();
  if (activeTab === "backtest") return backtestExportRows();
  if (activeTab === "quality") return qualityExportRows();
  return overviewExportRows();
}

function exportActiveTab(format) {
  if (activeTab === "backtest" && !backtestState) {
    loadBacktest().then(() => exportActiveTab(format)).catch(err => renderBacktestError(err.message));
    return;
  }
  const rows = exportRows();
  if (!rows.length) {
    document.getElementById("syncStatus").textContent = `Nothing to export for ${activeTab}.`;
    return;
  }
  const headers = [...rows.reduce((set, row) => {
    Object.keys(row).forEach(key => set.add(key));
    return set;
  }, new Set())];
  const stamp = new Date().toISOString().slice(0, 10);
  const filename = `mmpredictions-${activeTab}-${stamp}.${format}`;
  if (format === "csv") {
    const csv = [headers.map(csvCell).join(",")]
      .concat(rows.map(row => headers.map(header => csvCell(row[header])).join(",")))
      .join("\n");
    downloadFile(filename, csv, "text/csv;charset=utf-8");
    return;
  }
  const cells = rows.map(row => `<tr>${headers.map(header => `<td>${esc(row[header])}</td>`).join("")}</tr>`).join("");
  const html = `<!doctype html><html><head><meta charset="utf-8"></head><body><table><thead><tr>${headers.map(header => `<th>${esc(header)}</th>`).join("")}</tr></thead><tbody>${cells}</tbody></table></body></html>`;
  downloadFile(filename, html, "application/vnd.ms-excel;charset=utf-8");
}

document.getElementById("platformFilter").addEventListener("change", () => {
  for (const id of ["countryFilter", "partnerFilter", "campaignFilter"]) document.getElementById(id).value = "";
  page = 1;
  load().catch(err => setView("error", err.message));
});
for (const id of ["countryFilter", "partnerFilter", "campaignFilter"]) {
  document.getElementById(id).addEventListener("change", debouncedClientRender);
}
for (const id of ["scopeFilter", "dateFrom", "dateTo"]) {
  document.getElementById(id).addEventListener("change", () => {
    updateDateInputs();
    page = 1;
    load().catch(err => setView("error", err.message));
  });
}

document.querySelectorAll("[data-tab]").forEach(button => {
  button.addEventListener("click", () => setActiveTab(button.dataset.tab));
});
document.getElementById("exportCsv").addEventListener("click", () => exportActiveTab("csv"));
document.getElementById("exportXls").addEventListener("click", () => exportActiveTab("xls"));
document.getElementById("refreshButton").addEventListener("click", () => {
  if (activeTab === "backtest") loadBacktest(true).catch(err => renderBacktestError(err.message));
  else load(true).catch(err => setView("error", err.message));
});
document.getElementById("accessButton").addEventListener("click", () => {
  document.getElementById("accessModal").hidden = false;
  document.getElementById("accessEmail").focus();
});
document.getElementById("closeAccess").addEventListener("click", () => {
  document.getElementById("accessModal").hidden = true;
});
document.getElementById("accessModal").addEventListener("click", event => {
  if (event.target.id === "accessModal") document.getElementById("accessModal").hidden = true;
});
document.getElementById("accessForm").addEventListener("submit", addAccessUser);
document.getElementById("campaignExclusionsButton").addEventListener("click", () => {
  renderCampaignExclusions();
  document.getElementById("campaignExclusionModal").hidden = false;
  document.getElementById("campaignExclusionSearch").focus();
});
document.getElementById("closeCampaignExclusions").addEventListener("click", () => {
  document.getElementById("campaignExclusionModal").hidden = true;
});
document.getElementById("campaignExclusionModal").addEventListener("click", event => {
  if (event.target.id === "campaignExclusionModal") document.getElementById("campaignExclusionModal").hidden = true;
});
document.getElementById("campaignExclusionSearch").addEventListener("input", debounce(renderCampaignExclusions, 120));
document.getElementById("clearCampaignExclusions").addEventListener("click", () => {
  excludedCampaignSet = new Set();
  writeCampaignExclusions();
  page = 1;
  render();
});
document.getElementById("resetFilters").addEventListener("click", () => {
  for (const id of ["platformFilter", "countryFilter", "partnerFilter", "campaignFilter"]) {
    document.getElementById(id).value = "";
  }
  document.getElementById("scopeFilter").value = "week";
  document.getElementById("dateFrom").value = "";
  document.getElementById("dateTo").value = "";
  updateDateInputs();
  page = 1;
  load().catch(err => setView("error", err.message));
});
document.getElementById("prevPage").addEventListener("click", () => { page -= 1; renderTable(); });
document.getElementById("nextPage").addEventListener("click", () => { page += 1; renderTable(); });

function initResizableColumns() {
  const table = document.getElementById("campaignTable");
  const headers = [...table.querySelectorAll("thead th")];
  let savedWidths = [];
  try {
    savedWidths = JSON.parse(safeStorageGet(columnWidthsKey) || "[]");
  } catch (_err) {
    savedWidths = [];
  }
  if (!table.querySelector("colgroup")) {
    const group = document.createElement("colgroup");
    headers.forEach((_th, index) => {
      const col = document.createElement("col");
      col.style.width = `${Number(savedWidths[index] || defaultColumnWidths[index] || 120)}px`;
      group.appendChild(col);
    });
    table.prepend(group);
  }
  const cols = [...table.querySelectorAll("col")];
  headers.forEach((th, index) => {
    if (th.querySelector(".resize-handle")) return;
    const handle = document.createElement("span");
    handle.className = "resize-handle";
    handle.setAttribute("aria-hidden", "true");
    handle.addEventListener("pointerdown", event => {
      event.preventDefault();
      event.stopPropagation();
      const col = cols[index];
      const startX = event.clientX;
      const startWidth = parseFloat(col.style.width) || th.offsetWidth;
      document.body.classList.add("resizing-columns");
      const onMove = moveEvent => {
        const next = Math.max(72, startWidth + moveEvent.clientX - startX);
        col.style.width = `${next}px`;
      };
      const onUp = () => {
        document.body.classList.remove("resizing-columns");
        safeStorageSet(columnWidthsKey, JSON.stringify(cols.map(item => parseFloat(item.style.width) || 0)));
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
      };
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp, {once: true});
    });
    th.appendChild(handle);
  });
}

document.querySelectorAll("th[data-sort]").forEach(th => {
  th.setAttribute("role", "button");
  th.setAttribute("tabindex", "0");
  th.setAttribute("aria-sort", th.dataset.sort === sortKey ? "descending" : "none");
  const activate = () => {
    const key = th.dataset.sort;
    if (sortKey === key) sortDir = sortDir === "asc" ? "desc" : "asc";
    else {
      sortKey = key;
      sortDir = descDefaultSortKeys.has(key) || String(key).startsWith("h_") ? "desc" : "asc";
    }
    document.querySelectorAll("th[data-sort]").forEach(other => {
      other.setAttribute("aria-sort", "none");
    });
    th.setAttribute("aria-sort", sortDir === "asc" ? "ascending" : "descending");
    page = 1;
    renderTable();
  };
  th.addEventListener("click", activate);
  th.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") activate();
  });
});

initResizableColumns();
excludedCampaignSet = readCampaignExclusions();
readPreferences();
readHash();
readActiveTab();
setActiveTab(activeTab);
updateDateInputs();
load().catch(err => setView("error", err.message));
loadAccess();
statusTimer = setInterval(pollStatus, 15000);
pollStatus();
