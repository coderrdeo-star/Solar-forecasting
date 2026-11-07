// SolarChip Dashboard Client Script
let liveChart = null;
let forecastChart = null;
let seasonChart = null;
let lastReadingTime = null;
let freshnessInterval = null;

// Safe Chart helper with graceful fallback
function renderOrFallback(canvasId, fallbackId, configCallback) {
  const canvas = document.getElementById(canvasId);
  const fallback = document.getElementById(fallbackId);

  if (typeof Chart === "undefined") {
    if (canvas) canvas.style.display = "none";
    if (fallback) {
      fallback.style.display = "flex";
      fallback.innerHTML = `
        <p><strong>Chart rendering is unavailable</strong></p>
        <p style="font-size: 0.8rem; margin-top: 4px;">Chart.js library could not be loaded (offline or CDN blocked).</p>
      `;
    }
    return null;
  }

  if (canvas) canvas.style.display = "block";
  if (fallback) fallback.style.display = "none";
  return configCallback(canvas);
}

// Fetch helper with error and timeout handling
async function fetchJson(url, options = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 12000);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    clearTimeout(timeoutId);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}: ${res.statusText}`);
    }
    return data;
  } catch (err) {
    clearTimeout(timeoutId);
    if (err.name === "AbortError") {
      throw new Error("Request timed out. Server or network may be slow.");
    }
    throw err;
  }
}

// Format relative time (e.g. "12s ago", "3m ago")
function formatSecondsAgo(seconds) {
  if (seconds == null || isNaN(seconds)) return "awaiting data";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ago`;
}

// Update the connection indicator pill
function setConnectionState(state, text) {
  const badge = document.getElementById("connectionBadge");
  const textEl = document.getElementById("connectionText");
  if (!badge || !textEl) return;

  badge.className = "status-indicator";
  if (state === "live") {
    badge.classList.add("status-live");
  } else if (state === "stale") {
    badge.classList.add("status-stale");
  } else if (state === "waiting") {
    badge.classList.add("status-waiting");
  } else {
    badge.classList.add("status-error");
  }
  textEl.textContent = text;
}

// Update the system status banner
function updateStatusBanner(statusData, sensorData) {
  const banner = document.getElementById("statusBanner");
  const icon = document.getElementById("statusIcon");
  const title = document.getElementById("statusTitle");
  const tag = document.getElementById("statusTag");
  const desc = document.getElementById("statusDesc");
  if (!banner) return;

  if (!sensorData || !sensorData.latest) {
    banner.className = "alert-banner alert-info";
    icon.textContent = "🔌";
    title.textContent = "Awaiting Sensor Hardware";
    tag.textContent = "Idle";
    desc.textContent = "No sensor telemetry received yet. Connect your ESP32 or send a sample reading below.";
    return;
  }

  const isStale = sensorData.connected === false;
  const devPercent = statusData && statusData.deviation_percent != null ? statusData.deviation_percent : null;
  const deviationEl = document.getElementById("diagDeviation");
  if (deviationEl) {
    deviationEl.textContent = devPercent !== null ? `${devPercent}%` : "-- %";
  }

  if (isStale) {
    banner.className = "alert-banner alert-warning";
    icon.textContent = "⚠️";
    title.textContent = "Sensor Telemetry Stale";
    tag.textContent = "Stale";
    desc.textContent = `Last reading was received ${formatSecondsAgo(sensorData.age_seconds)}. Check ESP32 power, sensor connections, or Wi-Fi.`;
  } else if (statusData && statusData.status === "possible_deviation") {
    banner.className = "alert-banner alert-warning";
    icon.textContent = "⚡";
    title.textContent = "Generation Below Rating";
    tag.textContent = "Deviation";
    desc.textContent = statusData.message || "Power generation is significantly below rated panel capacity. Check for cloud cover, soiling, or shading.";
  } else {
    banner.className = "alert-banner alert-success";
    icon.textContent = "✅";
    title.textContent = "System Healthy & Online";
    tag.textContent = "Active";
    desc.textContent = `ESP32 connected and streaming. Instantaneous output: ${Number(sensorData.latest.power).toFixed(2)} W.`;
  }
}

// 1. Load Live Dashboard Telemetry
async function loadDashboard() {
  try {
    const data = await fetchJson("/api/dashboard");
    const sensor = data.sensor;
    const latest = sensor ? sensor.latest : null;
    const emptyState = document.getElementById("emptyState");

    if (!latest) {
      setConnectionState("waiting", "Awaiting ESP32");
      if (emptyState) emptyState.style.display = "block";
      updateStatusBanner(null, sensor);
      return;
    }

    if (emptyState) emptyState.style.display = "none";

    // Track timestamp for client-side freshness ticking
    if (latest.timestamp) {
      lastReadingTime = new Date(latest.timestamp.replace("Z", "+00:00"));
    }

    const isLive = sensor.connected;
    setConnectionState(isLive ? "live" : "stale", isLive ? "Sensor Streaming" : `Stale (${formatSecondsAgo(sensor.age_seconds)})`);

    // Populate Key Metric Cards
    document.getElementById("cardPower").textContent = Number(latest.power).toFixed(2);
    document.getElementById("cardVoltage").textContent = Number(latest.voltage).toFixed(2);
    document.getElementById("cardCurrent").textContent = Number(latest.current).toFixed(3);
    document.getElementById("cardTemp").textContent = latest.panel_temp != null ? Number(latest.panel_temp).toFixed(1) : "--";

    // Today's Energy Card
    if (data.today_energy) {
      document.getElementById("cardEnergyWh").textContent = Number(data.today_energy.energy_wh).toFixed(1);
      document.getElementById("cardEnergyKwh").textContent = `Equivalent to ${Number(data.today_energy.energy_kwh).toFixed(3)} kWh today`;
    }

    // Freshness indicator
    const freshnessEl = document.getElementById("freshnessIndicator");
    if (freshnessEl) {
      freshnessEl.textContent = `Last reading: ${formatSecondsAgo(sensor.age_seconds)}`;
    }

    // Render Live Chart
    const readings = data.recent_readings || [];
    if (readings.length > 0) {
      const labels = readings.map(r => {
        try {
          const d = new Date(r.timestamp.replace("Z", "+00:00"));
          return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        } catch {
          return r.timestamp;
        }
      });
      const powers = readings.map(r => Number(r.power));
      const voltages = readings.map(r => Number(r.voltage));

      if (liveChart) liveChart.destroy();
      liveChart = renderOrFallback("liveChart", "liveChartFallback", canvas => {
        return new Chart(canvas, {
          type: "line",
          data: {
            labels: labels,
            datasets: [
              {
                label: "Power (W)",
                data: powers,
                borderColor: "#10b981",
                backgroundColor: "rgba(16, 185, 129, 0.12)",
                fill: true,
                tension: 0.25,
                yAxisID: "yPower"
              },
              {
                label: "Voltage (V)",
                data: voltages,
                borderColor: "#3b82f6",
                borderDash: [4, 4],
                backgroundColor: "transparent",
                tension: 0.25,
                yAxisID: "yVoltage"
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            scales: {
              yPower: {
                type: "linear",
                position: "left",
                beginAtZero: true,
                title: { display: true, text: "Power (Watts)" }
              },
              yVoltage: {
                type: "linear",
                position: "right",
                beginAtZero: true,
                grid: { drawOnChartArea: false },
                title: { display: true, text: "Voltage (Volts)" }
              }
            },
            plugins: {
              legend: { position: "top" }
            }
          }
        });
      });
    }

    // Fetch status diagnostics
    try {
      const statusData = await fetchJson("/api/status");
      updateStatusBanner(statusData, sensor);
    } catch {
      updateStatusBanner(null, sensor);
    }

  } catch (err) {
    console.error("Dashboard update error:", err);
    setConnectionState("error", "Backend Offline");
  }
}

// 2. Load 24-Hour Forecast
async function loadForecast() {
  const badge = document.getElementById("weatherStatusBadge");
  try {
    const data = await fetchJson("/api/forecast");

    if (data.weather_available === false) {
      if (badge) {
        badge.innerHTML = `<span class="badge" style="background:#fee2e2; color:#991b1b;">Weather API: Offline</span>`;
      }
      document.getElementById("cardForecastKwh").textContent = "--";
      document.getElementById("cardForecastWh").textContent = "Weather forecast unavailable";
      renderOrFallback("forecastChart", "forecastChartFallback", () => null);
      const fallback = document.getElementById("forecastChartFallback");
      if (fallback) {
        fallback.style.display = "flex";
        fallback.innerHTML = `
          <p><strong>Weather Service Temporarily Unavailable</strong></p>
          <p style="font-size: 0.85rem; margin-top: 4px;">Open-Meteo could not be reached. Local hardware monitoring remains operational.</p>
        `;
      }
      return;
    }

    if (badge) {
      badge.innerHTML = `<span class="badge" style="background:#ecfdf5; color:#065f46;">Weather: Live (${data.location_name})</span>`;
    }

    // Update Forecast Metric Card
    document.getElementById("cardForecastKwh").textContent = Number(data.total_forecast_energy_kwh).toFixed(3);
    document.getElementById("cardForecastWh").textContent = `Total: ${Number(data.total_forecast_energy_wh).toFixed(1)} Wh estimated`;

    const hourly = data.hourly || [];
    const labels = hourly.map(h => {
      try {
        const d = new Date(h.time);
        return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      } catch {
        return `Hour ${h.hour}`;
      }
    });
    const energies = hourly.map(h => h.predicted_energy_wh);
    const radiations = hourly.map(h => h.radiation_w_m2);

    if (forecastChart) forecastChart.destroy();
    forecastChart = renderOrFallback("forecastChart", "forecastChartFallback", canvas => {
      return new Chart(canvas, {
        type: "bar",
        data: {
          labels: labels,
          datasets: [
            {
              type: "bar",
              label: "Predicted Energy (Wh)",
              data: energies,
              backgroundColor: "rgba(245, 158, 11, 0.75)",
              borderColor: "#d97706",
              borderWidth: 1,
              yAxisID: "yWh"
            },
            {
              type: "line",
              label: "Solar Irradiance (W/m²)",
              data: radiations,
              borderColor: "#f59e0b",
              borderDash: [3, 3],
              pointRadius: 2,
              fill: false,
              yAxisID: "yRad"
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            yWh: {
              type: "linear",
              position: "left",
              beginAtZero: true,
              title: { display: true, text: "Energy (Watt-hours / Wh)" }
            },
            yRad: {
              type: "linear",
              position: "right",
              beginAtZero: true,
              grid: { drawOnChartArea: false },
              title: { display: true, text: "Irradiance (W/m²)" }
            }
          },
          plugins: {
            legend: { position: "top" }
          }
        }
      });
    });

  } catch (err) {
    console.warn("Forecast load failure:", err);
    if (badge) {
      badge.innerHTML = `<span class="badge" style="background:#fee2e2; color:#991b1b;">Weather: Unavailable</span>`;
    }
  }
}

// 3. Load 12-Month Seasonal Estimate
async function loadSeasonal() {
  try {
    const data = await fetchJson("/api/seasonal");
    const months = data.months || [];
    const labels = months.map(m => m.month);
    const dailyWhs = months.map(m => m.estimated_daily_wh);

    if (seasonChart) seasonChart.destroy();
    seasonChart = renderOrFallback("seasonChart", "seasonChartFallback", canvas => {
      return new Chart(canvas, {
        type: "bar",
        data: {
          labels: labels,
          datasets: [
            {
              label: "Estimated Daily Energy (Wh/day)",
              data: dailyWhs,
              backgroundColor: "rgba(30, 64, 175, 0.75)",
              borderColor: "#1e3a8a",
              borderWidth: 1,
              borderRadius: 4
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            y: {
              beginAtZero: true,
              title: { display: true, text: "Energy per Day (Wh/day)" }
            }
          },
          plugins: {
            legend: { position: "top" }
          }
        }
      });
    });

  } catch (err) {
    console.warn("Seasonal load failure:", err);
  }
}

// Send a sample reading from the UI for quick testing
async function sendSampleTelemetry() {
  try {
    const sample = {
      voltage: +(4.8 + Math.random() * 0.8).toFixed(2),
      current: +(0.30 + Math.random() * 0.15).toFixed(3),
      panel_temp: +(34.0 + Math.random() * 6.0).toFixed(1)
    };

    const res = await fetchJson("/api/readings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sample)
    });

    console.log("Sample sent:", res);
    await refreshAll();
  } catch (err) {
    alert("Error sending test reading: " + err.message);
  }
}

// Refresh all sections independently
async function refreshAll() {
  const btn = document.getElementById("refreshBtn");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="btn-icon">⏳</span> Refreshing...`;
  }

  // Use allSettled so one component failure doesn't block the others
  await Promise.allSettled([
    loadDashboard(),
    loadForecast(),
    loadSeasonal()
  ]);

  if (btn) {
    btn.disabled = false;
    btn.innerHTML = `<span class="btn-icon">↻</span> Refresh Now`;
  }
}

// Client-side ticking freshness timer
function startFreshnessTicker() {
  if (freshnessInterval) clearInterval(freshnessInterval);
  freshnessInterval = setInterval(() => {
    if (!lastReadingTime) return;
    const now = new Date();
    const diffSeconds = (now - lastReadingTime) / 1000;
    const freshnessEl = document.getElementById("freshnessIndicator");
    if (freshnessEl) {
      freshnessEl.textContent = `Last reading: ${formatSecondsAgo(diffSeconds)}`;
    }
  }, 1000);
}

// Initial Boot
document.addEventListener("DOMContentLoaded", () => {
  refreshAll();
  startFreshnessTicker();
  // Automatic dashboard poll every 15 seconds
  setInterval(loadDashboard, 15000);
  // Forecast and seasonal update every 10 minutes
  setInterval(() => {
    loadForecast();
    loadSeasonal();
  }, 600000);
});
