"use strict";
// Map view: MapLibre GL over OpenFreeMap tiles. One SVG marker per game:
//   - disc radius by impact tier (old buckets 7/15/25/40/50 via signal.size, or |edge_pts| in edge
//     mode), fill by signal tier (or by combined flag when a Signals preset is active)
//   - ring color = rain (grey/black) / heat (red) driver, otherwise faint
//   - hollow disc for dome / closed roof
//   - opacity = confidence (impact.v2.conf, else best-edge confidence) 0.4..1.0, or the static
//     stadium wind_vol bucket when the "static" toggle is on
//   - wind arrow: rotated to weather.wind_dir_deg (meteorological "from"; drawn blowing *toward*
//     dir+180), length ∝ wind_fg
//   - thin field-axis line rotated to stadium.orient_deg (0..180, both ends drawn)
//   - clustering below zoom 4 (pixel-grid buckets; click a cluster to zoom in)
// Every Phase 5 field is optional: missing → that element is simply not drawn.

const OPENFREEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";
const BLANK_STYLE = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#0b0f16" } }] };
const CLUSTER_ZOOM = 4;          // cluster when map.getZoom() < this
const CLUSTER_CELL_PX = 44;      // pixel grid used to bucket markers
const ARROW_PX_PER_MPH = 1.1;    // arrow length = wind_fg * this (clamped)
const MAP = {
  map: null, markers: [], popup: null, ready: false, sport: null, styleFailed: false,
  rows: [], opacityMode: "conf", showVectors: true, clustered: false, listeners: false,
};
const SPORT_VIEW = { nfl: { center: [-96.5, 38.5], zoom: 3.6 }, cfb: { center: [-93.5, 36.5], zoom: 3.8 } };

function ensureMap() {
  if (MAP.map || typeof maplibregl === "undefined") return MAP.map;
  const v = SPORT_VIEW[STATE.sport] || SPORT_VIEW.nfl;
  MAP.map = new maplibregl.Map({ container: "map", style: OPENFREEMAP_STYLE, center: v.center, zoom: v.zoom, attributionControl: true });
  MAP.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  MAP.map.on("load", () => { MAP.ready = true; });
  MAP.map.on("error", (e) => {
    // style / tile failure (offline, CSP): fall back to a blank dark canvas so markers still show
    if (!MAP.styleFailed && e && e.error && /style|fetch|Failed/i.test(String(e.error.message || e.error))) {
      MAP.styleFailed = true;
      try { MAP.map.setStyle(BLANK_STYLE); } catch (_) { /* ignore */ }
    }
  });
  // re-bucket clusters when the view changes (only matters at low zoom)
  MAP.map.on("moveend", () => {
    if (!MAP.rows.length) return;
    const low = MAP.map.getZoom() < CLUSTER_ZOOM;
    if (low || MAP.clustered) placeMarkers();
  });
  MAP.sport = STATE.sport;
  return MAP.map;
}

// ── per-marker geometry ───────────────────────────────────────────────────
function markerRadius(g) {
  const be = STATE.minEdge != null || STATE.book ? bestEdge(g, null, STATE.book || null) : null;
  if (be && isNum(be.edge_pts)) return Math.max(6, Math.min(26, 6 + Math.abs(be.edge_pts) * 5));
  const s = g.signal && isNum(g.signal.size) ? Number(g.signal.size) : null;
  if (s != null) return Math.max(6, Math.min(26, 5 + s * 0.42));   // 7..50 → ~8..26 px
  const v1 = g.impact && g.impact.v1;
  const pct = v1 && isNum(v1.gs_fg_pct) ? Math.abs(Number(v1.gs_fg_pct)) : 0;
  return Math.max(6, Math.min(26, 6 + pct * 1.6));
}
function markerRing(g) {
  const wx = g.weather || {};
  if (isNum(wx.rain_fg) && Number(wx.rain_fg) > 2) return "#d0d7de";
  const flags = gameFlags(g);
  if (flags.includes("Heat") || flags.includes("Alt+Heat")) return "#e5484d";
  return "rgba(255,255,255,.25)";
}
// fill: Signals preset → flag palette; otherwise impact tier palette
function markerFill(g) {
  const preset = typeof activePreset === "function" ? activePreset() : null;
  if (preset && FLAG_COLORS[preset.flag]) return FLAG_COLORS[preset.flag];
  return signalColor(g.signal);
}
// 0..1 confidence: v2 model conf first, then the best edge's confidence, else null
function gameConfidence(g) {
  const v2 = g.impact && g.impact.v2;
  if (v2 && isNum(v2.conf)) return Math.max(0, Math.min(1, Number(v2.conf)));
  const be = bestEdge(g);
  if (be && isNum(be.confidence)) return Math.max(0, Math.min(1, Number(be.confidence)));
  return null;
}
const STATIC_VOL_OPACITY = { low: 1.0, med: 0.7, medium: 0.7, mid: 0.7, moderate: 0.7, high: 0.45, "very high": 0.4 };
function markerOpacity(g) {
  if (MAP.opacityMode === "static") {
    const st = g.stadium || {};
    const key = String(st.wind_vol_static || "").toLowerCase().trim();
    return key && STATIC_VOL_OPACITY[key] != null ? STATIC_VOL_OPACITY[key] : 0.85;
  }
  const c = gameConfidence(g);
  return c == null ? 0.85 : Math.max(0.4, Math.min(1, 0.4 + 0.6 * c));
}
function windArrowLen(g) {
  const wx = g.weather || {};
  if (!isNum(wx.wind_fg) || !isNum(wx.wind_dir_deg)) return 0;
  const w = Number(wx.wind_fg);
  if (w < 1) return 0;
  return Math.max(6, Math.min(40, w * ARROW_PX_PER_MPH));
}

// SVG marker: <circle> (disc), optional axis line, optional arrow. The svg box is large enough
// for the longest arrow so nothing clips; only the disc takes pointer events.
function markerEl(g) {
  const r = markerRadius(g);
  const color = markerFill(g);
  const dome = isDome(g);
  const wx = g.weather || {}, st = g.stadium || {};
  const arrow = MAP.showVectors && !dome ? windArrowLen(g) : 0;
  const axis = MAP.showVectors && isNum(st.orient_deg) ? r + 6 : 0;
  const half = Math.ceil(Math.max(r + 3, arrow + 6, axis + 2));
  const size = half * 2;
  const parts = [];
  if (axis) {
    // field axis: bearing measured clockwise from north; SVG y grows downward so rotate as-is
    parts.push(`<line class="axis" x1="0" y1="${-axis}" x2="0" y2="${axis}" transform="rotate(${Number(st.orient_deg).toFixed(1)})" />`);
  }
  parts.push(`<circle class="disc" r="${r}" fill="${dome ? "transparent" : color}" stroke="${dome ? color : markerRing(g)}" stroke-width="2" />`);
  if (arrow) {
    // wind_dir_deg = direction the wind comes FROM → arrow points where it blows TO
    const rot = (Number(wx.wind_dir_deg) + 180) % 360;
    const tip = -(arrow + r * 0.35);
    const base = -(r * 0.35);
    parts.push(`<g class="arrow" transform="rotate(${rot.toFixed(1)})">`
      + `<line x1="0" y1="${base}" x2="0" y2="${tip + 4}" />`
      + `<polygon points="0,${tip} -4,${tip + 7} 4,${tip + 7}" /></g>`);
  }
  const el = document.createElement("div");
  el.className = `marker${dome ? " dome" : ""}`;
  el.style.cssText = `width:${size}px;height:${size}px;`;
  // opacity lives on the <svg>: maplibre's Marker owns the wrapper's style.opacity (terrain occlusion)
  el.innerHTML = `<svg viewBox="${-half} ${-half} ${size} ${size}" width="${size}" height="${size}" style="opacity:${markerOpacity(g).toFixed(2)}">${parts.join("")}</svg>`;
  el.title = `${gameLabel(g)} · ${signalLabel(g.signal)}`
    + (isNum(wx.wind_fg) ? ` · ${fmtNum(wx.wind_fg, 0)} mph${wx.wind_dir_fg ? " " + wx.wind_dir_fg : ""}` : "");
  return el;
}

function clusterEl(items) {
  const n = items.length;
  const order = ["No", "Low", "Mid", "High", "Very High"];
  let top = items[0];
  for (const g of items) if (order.indexOf(signalTier(g.signal)) > order.indexOf(signalTier(top.signal))) top = g;
  const el = document.createElement("div");
  const size = Math.max(26, Math.min(46, 22 + n * 2));
  el.className = "marker cluster";
  el.style.cssText = `width:${size}px;height:${size}px;border-color:${markerFill(top)};`;
  el.textContent = String(n);
  el.title = `${n} games — click to zoom`;
  return el;
}

// ── popup ─────────────────────────────────────────────────────────────────
function popupHtml(g) {
  const wx = g.weather || {}, st = g.stadium || {}, c = g.consensus || {};
  const v1 = (g.impact && g.impact.v1) || {}, v2 = g.impact && g.impact.v2;
  const be = bestEdge(g);
  const flags = gameFlags(g);
  const row = (t, o) => `<div class="hc-li"><span class="t">${esc(t)}</span><span>${o}</span></div>`;
  const pp = isNum(wx.precip_prob) ? ` (${Math.round(Number(wx.precip_prob) * (wx.precip_prob <= 1 ? 100 : 1))}%)` : "";
  const band = isNum(wx.wind_p10) && isNum(wx.wind_p90) ? ` <span class="sub">${fmtNum(wx.wind_p10, 0)}–${fmtNum(wx.wind_p90, 0)}</span>` : "";
  const comp = isNum(wx.cross_mph) || isNum(wx.head_mph) ? row("Cross / Head", `${fmtNum(wx.cross_mph, 0)} / ${fmtNum(wx.head_mph, 0)} mph`) : "";
  const conf = gameConfidence(g);
  const src = wx.source || isNum(wx.lead_hours) ? row("Source", `${esc(wx.source || "—")}${isNum(wx.lead_hours) ? ` · ${Math.round(Number(wx.lead_hours))}h out` : ""}`) : "";
  return `<div class="popup">
    <div class="hc-h">${esc(gameLabel(g))} <span class="sub">${esc(kickoffLabel(g))}</span></div>
    ${row("Signal", `<span class="sig" style="background:${signalColor(g.signal)}">${esc(signalLabel(g.signal))}</span>${flags.length ? " " + esc(flags.join(", ")) : ""}`)}
    ${row("Wind", `${fmtNum(wx.wind_fg, 1)} mph ${esc(wx.wind_dir_fg || "")}${band}`)}
    ${row("Gust", `${fmtNum(wx.gust_fg, 0)} mph`)}
    ${comp}
    ${row("Temp", `${fmtNum(wx.temp_fg, 0)} °F`)}
    ${row("Rain", `${fmtNum(wx.rain_fg, 1)} mm${pp}`)}
    ${row("Impact", isDome(g) ? "dome" : `${fmtNum(v1.gs_fg_pct, 1)}% / away ${fmtNum(v1.away_fg_pct, 1)}%`)}
    ${v2 && !isDome(g) ? row("Impact v2", `${fmtNum(v2.gs_fg_pct, 1)}% / away ${fmtNum(v2.away_fg_pct, 1)}%`) : ""}
    ${row("Total", `${fmtTotal(c.total_open)} → ${fmtTotal(c.total_now)}`)}
    ${row("Spread", `${fmtLine(c.spread_open)} → ${fmtLine(c.spread_now)}${c.spread_src ? ` <span class="sub">(${esc(c.spread_src)})</span>` : ""}`)}
    ${row("Location", `${esc(st.name || "")}${isNum(st.orient_deg) ? ` <span class="sub">axis ${Math.round(Number(st.orient_deg))}°</span>` : ""}`)}
    ${row("Volatility", `${esc(st.wind_vol_static || "—")}${isNum(wx.wind_vol_fc) ? ` · fc ${fmtNum(wx.wind_vol_fc, 1)}` : ""}${conf != null ? ` · conf ${conf.toFixed(2)}` : ""}`)}
    ${src}
    ${be ? row("Best edge", `${esc(bookLabel(be.book))} ${esc(be.market)} ${esc(be.side || "")} ${be.market === "total" ? fmtTotal(be.line) : fmtLine(be.line)} <b>${be.edge_pts >= 0 ? "+" : ""}${Number(be.edge_pts).toFixed(1)}</b> ${esc(be.tier)}`) : ""}
    ${(typeof backtestHover === "function" ? backtestHover(g) : []).map(([k, v]) => row(k, esc(v))).join("")}
    <span class="open" data-game="${esc(g.game_id)}">Open detail →</span>
  </div>`;
}

function clearMarkers() {
  for (const m of MAP.markers) m.remove();
  MAP.markers = [];
  if (MAP.popup) { MAP.popup.remove(); MAP.popup = null; }
}

// ── legend + toggles ──────────────────────────────────────────────────────
function renderLegend(rows) {
  const el = document.getElementById("maplegend");
  const preset = typeof activePreset === "function" ? activePreset() : null;
  const tiers = STATE.sport === "cfb"
    ? ["Very High", "High", "Mid", "Low (Wind)", "Low (Rain)", "Low (Temp)", "No"]
    : ["High", "Mid", "Low", "No"];
  const domes = rows.filter(isDome).length;
  const hasVectors = rows.some((g) => (g.weather && isNum(g.weather.wind_dir_deg)) || (g.stadium && isNum(g.stadium.orient_deg)));
  const hasConf = rows.some((g) => gameConfidence(g) != null);
  const fillRows = preset
    ? `<div class="lg"><span class="dot" style="background:${FLAG_COLORS[preset.flag] || "#8b949e"}"></span>${esc(preset.label)} (preset)</div>`
    : tiers.map((t) => `<div class="lg"><span class="dot" style="background:${TIER_COLORS[t]}"></span>${esc(t)}</div>`).join("");
  el.innerHTML = fillRows
    + `<div class="lg"><span class="dot hollow"></span>dome / closed${domes ? ` (${domes})` : ""}</div>`
    + `<div class="lg sub">size = impact${STATE.minEdge != null || STATE.book ? " (edge mode)" : ""}</div>`
    + (hasVectors ? `<div class="lg sub"><svg width="14" height="14" viewBox="-7 -7 14 14"><line class="axis" x1="0" y1="-6" x2="0" y2="6"/></svg>field axis · <svg width="14" height="14" viewBox="-7 -7 14 14"><g class="arrow"><line x1="0" y1="5" x2="0" y2="-2"/><polygon points="0,-6 -3,-1 3,-1"/></g></svg>wind (to) ∝ mph</div>` : "")
    + `<div class="lg-ctl">
        <label class="chk" title="Marker opacity: forecast confidence (v2 conf / edge confidence) or the static stadium wind-volatility bucket">
          <input type="checkbox" id="map-static" ${MAP.opacityMode === "static" ? "checked" : ""} /> static vol opacity${hasConf || MAP.opacityMode === "static" ? "" : " (no conf yet)"}</label>
        <label class="chk" title="Draw wind arrows and field-axis lines"><input type="checkbox" id="map-vectors" ${MAP.showVectors ? "checked" : ""} /> arrows + axis</label>
       </div>`;
  const st = el.querySelector("#map-static");
  const vec = el.querySelector("#map-vectors");
  if (st) st.addEventListener("change", (e) => { MAP.opacityMode = e.target.checked ? "static" : "conf"; placeMarkers(); });
  if (vec) vec.addEventListener("change", (e) => { MAP.showVectors = e.target.checked; placeMarkers(); });
}

// ── placement (with low-zoom clustering) ──────────────────────────────────
function placeMarkers() {
  const map = MAP.map;
  if (!map) return;
  clearMarkers();
  const rows = MAP.rows;
  const zoom = map.getZoom();
  if (zoom < CLUSTER_ZOOM && rows.length > 1) {
    MAP.clustered = true;
    const buckets = new Map();
    for (const g of rows) {
      const pt = map.project([Number(g.stadium.lon), Number(g.stadium.lat)]);
      const key = `${Math.floor(pt.x / CLUSTER_CELL_PX)}:${Math.floor(pt.y / CLUSTER_CELL_PX)}`;
      (buckets.get(key) || buckets.set(key, []).get(key)).push(g);
    }
    for (const items of buckets.values()) {
      if (items.length === 1) { addGameMarker(items[0]); continue; }
      const lon = items.reduce((s, g) => s + Number(g.stadium.lon), 0) / items.length;
      const lat = items.reduce((s, g) => s + Number(g.stadium.lat), 0) / items.length;
      const el = clusterEl(items);
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        map.easeTo({ center: [lon, lat], zoom: Math.max(CLUSTER_ZOOM + 0.5, zoom + 1.5), duration: 400 });
      });
      MAP.markers.push(new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(map));
    }
    return;
  }
  MAP.clustered = false;
  // big markers underneath, small on top, so nothing hides a small signal
  const ordered = rows.slice().sort((a, b) => markerRadius(b) - markerRadius(a));
  for (const g of ordered) addGameMarker(g);
}

function addGameMarker(g) {
  const map = MAP.map;
  const el = markerEl(g);
  const lngLat = [Number(g.stadium.lon), Number(g.stadium.lat)];
  const marker = new maplibregl.Marker({ element: el }).setLngLat(lngLat).addTo(map);
  el.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (MAP.popup) MAP.popup.remove();
    MAP.popup = new maplibregl.Popup({ offset: markerRadius(g) + 4, maxWidth: "320px" })
      .setLngLat(lngLat).setHTML(popupHtml(g)).addTo(map);
    const open = MAP.popup.getElement().querySelector(".open");
    if (open) open.addEventListener("click", () => openDrawer(open.dataset.game));
  });
  MAP.markers.push(marker);
}

function renderMap(rows) {
  const map = ensureMap();
  if (!map) {
    document.getElementById("map").innerHTML = '<div class="empty">MapLibre failed to load (vendor/maplibre-gl.js missing?)</div>';
    return;
  }
  if (MAP.sport !== STATE.sport) {
    const v = SPORT_VIEW[STATE.sport] || SPORT_VIEW.nfl;
    map.jumpTo({ center: v.center, zoom: v.zoom });
    MAP.sport = STATE.sport;
  }
  renderLegend(rows);
  MAP.rows = rows.filter((g) => g.stadium && isNum(g.stadium.lat) && isNum(g.stadium.lon));
  placeMarkers();
  // container was display:none while on the table tab → force a size recompute
  setTimeout(() => { map.resize(); if (MAP.map.getZoom() < CLUSTER_ZOOM) placeMarkers(); }, 0);
}
