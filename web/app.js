/* global L */
(() => {
  const map = L.map('map', { zoomControl: true }).setView([30.32016, -89.25093], 14);

  let baseLayers = {};
  let activeBaseLayer = null;

  async function initBasemaps() {
    const r = await fetch("/basemaps", { cache: "no-store" });
    const maps = await r.json();

    maps.forEach(m => {
      const url = `/tiles/${m.id}/{z}/{x}/{y}.${m.format}`;
      baseLayers[m.label] = L.tileLayer(url, { maxZoom: 19, minZoom: 0, opacity: 1.0 });
    });

    const firstKey = Object.keys(baseLayers)[0];
    if (firstKey) {
      activeBaseLayer = baseLayers[firstKey];
      activeBaseLayer.addTo(map);
    }
    L.control.layers(baseLayers, null, { collapsed: false }).addTo(map);

    map.on("baselayerchange", (e) => {
      activeBaseLayer = e.layer;
      applyOpacityFromSlider();
    });

    applyOpacityFromSlider();
  }

  let marker = null;
  function makeArrowIcon(headingDeg) {
    return L.divIcon({
      className: "",
      html: `<div class="arrow-icon" style="transform-origin:50% 75%;transform: rotate(${headingDeg}deg);"></div>`,
      iconSize: [20, 24],
      iconAnchor: [10, 18]
    });
  }

  const TRACK_MAX_POINTS = 4000;
  const TRACK_MIN_STEP_M = 1.5;
  let trackLine = L.polyline([], { weight: 3 }).addTo(map);
  let lastTrackLL = null;
  let followVessel = true;

  const SWATH_WIDTH_M = 12;
  const MAX_SWATH_POINTS = 5000;
  let SWATH_OPACITY = 0.35;
  let swathLayer = L.layerGroup().addTo(map);
  let lastSwathLL = null;
  const SWATH_MIN_STEP_M = 1.5;

  let dMin = null, dMax = null;
  function depthToColor(depth) {
    if (dMin == null || dMax == null || dMax <= dMin) return "#00aaff";
    let t = (depth - dMin) / (dMax - dMin);
    t = Math.max(0, Math.min(1, t));
    const hue = (1 - t) * 200;
    return `hsl(${hue}, 100%, 45%)`;
  }

  let unit = "m";
  const M_TO_FT = 3.280839895;
  function toDisplayDepth(meters) { return meters == null ? null : (unit === "ft" ? meters * M_TO_FT : meters); }
  function depthSuffix() { return unit === "ft" ? " ft" : " m"; }
  function setUnit(newUnit) {
    unit = newUnit;
    try { localStorage.setItem("depth_unit", unit); } catch {}
    document.getElementById("unitM").classList.toggle("btn-on", unit === "m");
    document.getElementById("unitFT").classList.toggle("btn-on", unit === "ft");
  }
  try { const saved = localStorage.getItem("depth_unit"); if (saved === "m" || saved === "ft") unit = saved; } catch {}
  document.getElementById("unitM").onclick = () => setUnit("m");
  document.getElementById("unitFT").onclick = () => setUnit("ft");
  setUnit(unit);

  let posSource = "corr";
  function setPosSource(src) {
    posSource = src;
    try { localStorage.setItem("pos_source", posSource); } catch {}
    document.getElementById("posRaw").classList.toggle("btn-on", posSource === "raw");
    document.getElementById("posCorr").classList.toggle("btn-on", posSource === "corr");
  }
  try { const saved = localStorage.getItem("pos_source"); if (saved === "raw" || saved === "corr") posSource = saved; } catch {}
  document.getElementById("posRaw").onclick = () => setPosSource("raw");
  document.getElementById("posCorr").onclick = () => setPosSource("corr");
  setPosSource(posSource);

  const slider = document.getElementById("opacitySlider");
  const opacityVal = document.getElementById("opacityVal");
  function applyOpacityFromSlider() {
    const op = Math.max(0, Math.min(1, slider.value / 100));
    opacityVal.textContent = `${slider.value}%`;
    if (activeBaseLayer && activeBaseLayer.setOpacity) activeBaseLayer.setOpacity(op);
  }
  try { const saved = localStorage.getItem("basemap_opacity"); if (saved !== null) slider.value = saved; } catch {}
  slider.addEventListener("input", () => { try { localStorage.setItem("basemap_opacity", slider.value); } catch {} applyOpacityFromSlider(); });

  const swathSlider = document.getElementById("swathOpacitySlider");
  const swathOpacityVal = document.getElementById("swathOpacityVal");
  function applySwathOpacityFromSlider() {
    SWATH_OPACITY = Math.max(0, Math.min(1, swathSlider.value / 100));
    swathOpacityVal.textContent = `${swathSlider.value}%`;
    swathLayer.eachLayer(layer => { if (layer && layer.setStyle) layer.setStyle({ fillOpacity: SWATH_OPACITY }); });
  }
  try { const saved = localStorage.getItem("swath_opacity"); if (saved !== null) swathSlider.value = saved; } catch {}
  swathSlider.addEventListener("input", () => { try { localStorage.setItem("swath_opacity", swathSlider.value); } catch {} applySwathOpacityFromSlider(); });
  applySwathOpacityFromSlider();

  document.getElementById("followToggle").addEventListener("change", (e) => { followVessel = !!e.target.checked; });
  document.getElementById("clearTrack").onclick = () => { trackLine.setLatLngs([]); swathLayer.clearLayers(); lastTrackLL = null; lastSwathLL = null; };

  async function postJSON(url, body) {
    const r = await fetch(url, { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body || {}) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "request failed");
    return j;
  }

  document.getElementById("btnStartSurvey").onclick = async () => { await postJSON("/survey/start", { notes: "", line_label: "Line 1" }); };
  document.getElementById("btnStopSurvey").onclick = async () => { await postJSON("/survey/stop"); };
  document.getElementById("btnPauseSurvey").onclick = async () => {
    const st = await (await fetch("/survey/status", {cache:"no-store"})).json();
    if (st.paused) await postJSON("/survey/resume"); else await postJSON("/survey/pause");
  };
  document.getElementById("btnNewLine").onclick = async () => {
    const label = prompt("New line label (optional):") || "";
    await postJSON("/survey/new_line", { label });
  };

  async function refreshSurveyUI() {
    try {
      const st = await (await fetch("/survey/status", {cache:"no-store"})).json();
      const startBtn = document.getElementById("btnStartSurvey");
      const pauseBtn = document.getElementById("btnPauseSurvey");
      const newLineBtn = document.getElementById("btnNewLine");
      const stopBtn = document.getElementById("btnStopSurvey");
      const label = document.getElementById("surveyState");

      startBtn.disabled = st.active;
      pauseBtn.disabled = !st.active;
      newLineBtn.disabled = !st.active;
      stopBtn.disabled = !st.active;

      pauseBtn.textContent = st.paused ? "Resume" : "Pause";
      label.textContent = st.active ? `Session ${st.session_id} • Line ${st.line_number} • ${st.paused ? "PAUSED" : "RUNNING"}` : "No active survey";
    } catch (e) {}
    setTimeout(refreshSurveyUI, 800);
  }
  refreshSurveyUI();

  const btns = ["btnCsv","btnGJ","btnTif","btnAll"].map(id => document.getElementById(id));
  const busy = document.getElementById("exportBusy");
  const busyMsg = document.getElementById("busyMsg");
  const bar = document.getElementById("bar");
  const downloads = document.getElementById("downloads");
  const footerStatus = document.getElementById("footerStatus");

  function setExportButtonsEnabled(enabled) { btns.forEach(b => b.disabled = !enabled); }

  async function startExport(fmt) {
    downloads.innerHTML = "";
    setExportButtonsEnabled(false);
    busy.style.display = "block";
    busyMsg.textContent = "Starting export…";
    bar.style.width = "0%";
    footerStatus.textContent = "Exporting…";

    const r = await fetch("/export/start", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ format: fmt, export_dir: "exports", grid_m: 2.0, method: "mean", position_source: posSource })
    });
    const j = await r.json();
    if (!r.ok) {
      busyMsg.textContent = "Export failed: " + (j.error || "unknown");
      setExportButtonsEnabled(true);
      footerStatus.textContent = "Export failed";
      return;
    }
    pollExport(j.job_id);
  }

  async function pollExport(jobId) {
    try {
      const r = await fetch(`/export/status/${jobId}`, { cache: "no-store" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "status error");

      busyMsg.textContent = j.message || j.status;
      bar.style.width = `${j.progress || 0}%`;

      if (j.status === "done") {
        busy.style.display = "none";
        setExportButtonsEnabled(true);
        footerStatus.textContent = "Export complete";

        if (j.outputs && j.outputs.length) {
          const items = j.outputs.map(o => {
            const url = `/export/download?path=${encodeURIComponent(o.path)}`;
            return `<div><a style="color:#d8ecff;text-decoration:underline;" href="${url}">Download ${o.label}</a></div>`;
          }).join("");
          downloads.innerHTML = `<div style="font-weight:700;margin-top:6px;">Downloads</div>${items}`;
        } else {
          downloads.textContent = "Export complete.";
        }
        return;
      }

      if (j.status === "error") {
        busyMsg.textContent = "Export error: " + (j.message || "unknown");
        busy.style.display = "none";
        setExportButtonsEnabled(true);
        footerStatus.textContent = "Export error";
        return;
      }

      setTimeout(() => pollExport(jobId), 600);
    } catch (e) {
      busyMsg.textContent = "Export status error: " + e.message;
      setTimeout(() => pollExport(jobId), 1200);
    }
  }

  document.getElementById("btnCsv").onclick = () => startExport("csv");
  document.getElementById("btnGJ").onclick  = () => startExport("geojson");
  document.getElementById("btnTif").onclick = () => startExport("geotiff");
  document.getElementById("btnAll").onclick = () => startExport("all");

  async function tick() {
    try {
      const r = await fetch('/data', { cache: 'no-store' });
      const d = await r.json();

      const rlat = d.gps_raw_lat_deg, rlon = d.gps_raw_lon_deg;
      const clat = d.gps_corr_lat_deg, clon = d.gps_corr_lon_deg;
      document.getElementById("rawLatLon").textContent = (rlat!=null && rlon!=null) ? `${rlat.toFixed(6)}, ${rlon.toFixed(6)}` : "—";
      document.getElementById("corrLatLon").textContent = (clat!=null && clon!=null) ? `${clat.toFixed(6)}, ${clon.toFixed(6)}` : "—";

      let lat=null, lon=null;
      if (posSource === "raw") { lat=rlat; lon=rlon; } else { lat=clat; lon=clon; }

      const depth_m = d.ping_distance_m;
      const depth_disp = toDisplayDepth(depth_m);
      const speed_kn = d.gps_sog_knots;

      const gpsStatus = (d.gps_fix_quality != null) ? ((d.gps_fix_quality > 0) ? "3D Fix" : "No Fix") : "—";
      const sats = d.gps_num_sats != null ? d.gps_num_sats : "—";
      const hdop = d.gps_hdop != null ? d.gps_hdop.toFixed(1) : "—";
      const latTxt = (lat != null) ? lat.toFixed(6) : "—";
      const lonTxt = (lon != null) ? lon.toFixed(6) : "—";
      const spdTxt = (speed_kn != null) ? `${speed_kn.toFixed(1)} kn` : "—";
      const dTxt = (depth_disp != null) ? `${depth_disp.toFixed(2)}${depthSuffix()}` : "—";
      document.getElementById("headerText").textContent =
        `GPS: ${gpsStatus} • Sats: ${sats} (HDOP ${hdop}) • Lat: ${latTxt} • Lon: ${lonTxt} • Speed: ${spdTxt} • Depth: ${dTxt} • Pos: ${posSource === "raw" ? "raw" : "corrected"}`;

      const heading = (d.gps_cog_deg != null) ? d.gps_cog_deg : 0;

      if (lat != null && lon != null) {
        const ll = [lat, lon];
        const llObj = L.latLng(lat, lon);

        if (!marker) marker = L.marker(ll, { icon: makeArrowIcon(heading) }).addTo(map);
        else {
          marker.setLatLng(ll);
          const el = marker.getElement();
          if (el) {
            const arrow = el.querySelector(".arrow-icon");
            if (arrow) arrow.style.transform = `rotate(${heading}deg)`;
          }
        }

        if (!lastTrackLL || llObj.distanceTo(lastTrackLL) >= TRACK_MIN_STEP_M) {
          lastTrackLL = llObj;
          trackLine.addLatLng(llObj);
          const pts = trackLine.getLatLngs();
          if (pts.length > TRACK_MAX_POINTS) trackLine.setLatLngs(pts.slice(pts.length - TRACK_MAX_POINTS));
        }

        const pingOk = !d.ping_stale && depth_m != null;
        if (pingOk) {
          if (dMin == null || depth_m < dMin) dMin = depth_m;
          if (dMax == null || depth_m > dMax) dMax = depth_m;

          const dminDisp = toDisplayDepth(dMin);
          const dmaxDisp = toDisplayDepth(dMax);
          document.getElementById("legendMin").textContent = (dminDisp != null) ? `${dminDisp.toFixed(2)}${depthSuffix()}` : "—";
          document.getElementById("legendMax").textContent = (dmaxDisp != null) ? `${dmaxDisp.toFixed(2)}${depthSuffix()}` : "—";

          const tickEl = document.getElementById("depthTick");
          if (tickEl && dMin != null && dMax != null && dMax > dMin) {
            let t = (depth_m - dMin) / (dMax - dMin);
            t = Math.max(0, Math.min(1, t));
            tickEl.style.left = `${t * 100}%`;
          }

          if (!lastSwathLL || llObj.distanceTo(lastSwathLL) >= SWATH_MIN_STEP_M) {
            lastSwathLL = llObj;
            const fill = depthToColor(depth_m);
            const c = L.circle(ll, { radius: SWATH_WIDTH_M / 2, stroke:false, fillColor: fill, fillOpacity: SWATH_OPACITY });
            c.addTo(swathLayer);
            const layers = swathLayer.getLayers();
            if (layers.length > MAX_SWATH_POINTS) swathLayer.removeLayer(layers[0]);
          }
        }

        if (followVessel) map.panTo(llObj, { animate: true });
      }

      const gpsOk = !d.gps_stale && (lat != null && lon != null);
      const pingOk2 = !d.ping_stale && (depth_m != null);
      footerStatus.textContent = gpsOk && pingOk2 ? "Live" : (gpsOk ? "Ping stale" : "GPS stale");

    } catch (e) {}
    setTimeout(tick, 800);
  }

  initBasemaps();
  tick();
})();
