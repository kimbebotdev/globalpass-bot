const form = document.getElementById("run-form");
const logFeed = document.getElementById("log-feed");
const statusPill = document.getElementById("status-pill");
const statusLabel = document.getElementById("status-label");
const runIdEl = document.getElementById("run-id");
const resultsBlock = document.getElementById("results-block");
const fileList = document.getElementById("file-list");
const downloadJsonBtn = document.getElementById("download-json");
const downloadXlsxBtn = document.getElementById("download-xlsx");
const rawJson = document.getElementById("raw-json");
const loadFileBtn = document.getElementById("load-file");
const fileInput = document.getElementById("file-input");
const loadSampleBtn = document.getElementById("load-sample");
const flightTypeSelect = document.getElementById("flight_type");
const airlineSelect = document.getElementById("airline");
const travelStatusSelect = document.getElementById("travel_status");
const addFlightBtn = document.getElementById("add-flight");
const legsContainer = document.getElementById("legs-container");
const classOptions = ["Economy", "Premium Economy", "Business", "First"];
const timeOptions = Array.from({ length: 24 }, (_, h) => `${h.toString().padStart(2, "0")}:00`);
const isoToMmddyyyy = (val) => {
  if (!val) return "";
  if (val.includes("/")) return val;
  const parts = val.split("-");
  if (parts.length === 3) {
    return `${parts[1].padStart(2, "0")}/${parts[2].padStart(2, "0")}/${parts[0]}`;
  }
  return val;
};
const mmddyyyyToIso = (val) => {
  if (!val) return "";
  if (val.includes("-")) return val;
  const parts = val.split("/");
  if (parts.length === 3) {
    return `${parts[2]}-${parts[0].padStart(2, "0")}-${parts[1].padStart(2, "0")}`;
  }
  return val;
};

let ws;
let currentRunId = null;
let legs = [createLeg()];

const sampleInput = {
  flight_type: "round-trip",
  trips: [{ origin: "DXB", destination: "SIN" }],
  itinerary: [
    { date: "01/30/2026", time: "09:00", class: "Business" },
    { date: "02/25/2026", time: "12:30", class: "Business" },
  ],
  airline: "UA",
  travel_status: "R2 Standby",
  nonstop_flights: true,
};

function createLeg(overrides = {}) {
  return { origin: "", destination: "", date: "", time: "", class: "Economy", ...overrides };
}

function setStatus(status, text) {
  statusPill.className =
    "pill " + (status === "completed" ? "ok" : status === "error" ? "error" : "pending");
  statusPill.textContent = text || status;
  statusLabel.textContent = text || status;
}

function appendLog(msg) {
  const line = document.createElement("div");
  line.textContent = msg;
  logFeed.appendChild(line);
  logFeed.scrollTop = logFeed.scrollHeight;
}

function buildPayload() {
  const raw = rawJson.value.trim();
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      return { input: parsed, headed: document.getElementById("headed").checked };
    } catch (err) {
      throw new Error("Invalid JSON: " + err.message);
    }
  }
  const flightType = flightTypeSelect.value;
  const trips = legs.map((leg) => ({
    origin: (leg.origin || "").trim(),
    destination: (leg.destination || "").trim(),
  }));
  const itinerary = legs.map((leg) => ({
    date: leg.date,
    time: leg.time,
    class: leg.class || "Economy",
  }));
  for (let i = 0; i < trips.length; i++) {
    const t = trips[i];
    const it = itinerary[i];
    if (!t.origin || !t.destination || !it.date || !it.time) {
      throw new Error(`Leg ${i + 1}: origin, destination, date, and time are required`);
    }
  }
  const input = {
    flight_type: flightType,
    trips,
    itinerary,
    airline: airlineSelect.value,
    travel_status: travelStatusSelect.value,
    nonstop_flights: document.getElementById("nonstop_flights").checked,
  };
  return { input, headed: document.getElementById("headed").checked };
}

async function startRun(event) {
  event.preventDefault();
  logFeed.innerHTML = "";
  resultsBlock.textContent = "Running...";
  fileList.innerHTML = "";
  setStatus("pending", "running");
  downloadJsonBtn.disabled = true;
  downloadXlsxBtn.disabled = true;

  let payload;
  try {
    payload = buildPayload();
  } catch (err) {
    appendLog(err.message);
    setStatus("error", "invalid input");
    return;
  }

  const res = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  currentRunId = data.run_id;
  runIdEl.textContent = currentRunId ? `Run ${currentRunId}` : "";
  appendLog(`Run started (${currentRunId})`);
  connectWebSocket(currentRunId);
}

function connectWebSocket(runId) {
  if (!runId) return;
  if (ws) ws.close();
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${protocol}://${location.host}/ws/${runId}`);

  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "log") {
      appendLog(`${payload.ts || ""} ${payload.message}`);
    } else if (payload.type === "status") {
      if (payload.status === "completed") {
        setStatus("completed", "done");
        fetchResults(runId);
      } else if (payload.status === "error") {
        setStatus("error", "error");
        fetchResults(runId);
      } else {
        setStatus("pending", payload.status);
      }
    }
  };
  ws.onclose = () => appendLog("WebSocket closed.");
}

async function fetchResults(runId) {
  if (!runId) return;
  try {
    const res = await fetch(`/api/runs/${runId}`);
    const data = await res.json();
    buildReportTabs(data.report);
    fileList.innerHTML = "";
    if (data.files) {
      Object.entries(data.files).forEach(([name, path]) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = `${name}: ${path}`;
        fileList.appendChild(chip);
      });
    }
    downloadJsonBtn.disabled = false;
    downloadXlsxBtn.disabled = false;
  } catch (err) {
    appendLog("Failed to load results: " + err.message);
  }
}

async function download(kind) {
  if (!currentRunId) return;
  const url = `/api/runs/${currentRunId}/download/${kind}`;
  const res = await fetch(url);
  if (!res.ok) {
    appendLog(`Download failed (${kind})`);
    return;
  }
  const blob = await res.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${currentRunId}.${kind === "excel" ? "xlsx" : "json"}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function renderReport(report) {
  // Deprecated by tab view
}

function renderLegs() {
  const type = flightTypeSelect.value;
  legsContainer.innerHTML = "";
  legs.forEach((leg, idx) => {
    const card = document.createElement("div");
    card.className = "leg-card";

    const row = document.createElement("div");
    row.className = "leg-row";
    const title = document.createElement("div");
    title.className = "leg-title";
    if (type === "round-trip") {
      title.textContent = idx === 0 ? "Departure leg" : idx === 1 ? "Return leg" : `Leg ${idx + 1}`;
    } else {
      title.textContent = `Leg ${idx + 1}`;
    }
    row.appendChild(title);

    if (type === "multiple-legs" && legs.length > 1) {
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "remove-btn";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => {
        legs.splice(idx, 1);
        renderLegs();
      });
      row.appendChild(removeBtn);
    }
    card.appendChild(row);

    const grid = document.createElement("div");
    grid.className = "leg-grid";

    grid.appendChild(makeInput("Origin", leg.origin, "e.g. DXB", (val) => (leg.origin = val)));
    grid.appendChild(
      makeInput("Destination", leg.destination, "e.g. SIN", (val) => (leg.destination = val))
    );
    grid.appendChild(makeDateInput(leg));
    grid.appendChild(makeTimeSelect(leg));
    grid.appendChild(makeClassSelect(leg));

    card.appendChild(grid);
    legsContainer.appendChild(card);
  });
}

function makeInput(labelText, value, placeholder, onChange, type = "text") {
  const wrap = document.createElement("label");
  wrap.textContent = labelText;
  const input = document.createElement("input");
  input.type = type;
  input.placeholder = placeholder;
  input.value = value || "";
  input.addEventListener("input", (e) => onChange(e.target.value));
  wrap.appendChild(input);
  return wrap;
}

function makeDateInput(leg) {
  const wrap = document.createElement("label");
  wrap.textContent = "Date";
  const input = document.createElement("input");
  input.type = "date";
  input.value = leg.date ? mmddyyyyToIso(leg.date) : "";
  input.addEventListener("change", (e) => {
    leg.date = isoToMmddyyyy(e.target.value);
  });
  wrap.appendChild(input);
  return wrap;
}

function makeClassSelect(leg) {
  const wrap = document.createElement("label");
  wrap.textContent = "Class";
  const select = document.createElement("select");
  classOptions.forEach((opt) => {
    const option = document.createElement("option");
    option.value = opt;
    option.textContent = opt;
    select.appendChild(option);
  });
  select.value = leg.class || classOptions[0];
  select.addEventListener("change", (e) => (leg.class = e.target.value));
  wrap.appendChild(select);
  return wrap;
}

function makeTimeSelect(leg) {
  const wrap = document.createElement("label");
  wrap.textContent = "Time";
  const select = document.createElement("select");
  timeOptions.forEach((opt) => {
    const option = document.createElement("option");
    option.value = opt;
    option.textContent = opt;
    select.appendChild(option);
  });
  select.value = leg.time || timeOptions[0];
  if (!leg.time) {
    leg.time = select.value;
  }
  select.addEventListener("change", (e) => (leg.time = e.target.value));
  wrap.appendChild(select);
  return wrap;
}

function buildReportTabs(report) {
  const tabsContainer = document.getElementById("report-tabs");
  tabsContainer.innerHTML = "";
  if (!report || typeof report !== "object") {
    resultsBlock.textContent = "Report not available yet.";
    return;
  }

  const sheets = Object.keys(report)
    .filter((key) => Array.isArray(report[key]) && report[key].length)
    .filter((key) => key !== "Input_Summary");
  if (!sheets.length) {
    resultsBlock.textContent = "Report not available yet.";
    return;
  }

  let active = sheets[0];

  const renderSheet = (sheetName) => {
    const rows = report[sheetName] || [];
    if (!rows.length) {
      resultsBlock.textContent = "No data for " + sheetName;
      return;
    }
    const headers = Object.keys(rows[0]);
    resultsBlock.innerHTML = `
      <div><strong>${sheetName.replace(/_/g, " ").replace(/ All$/i, "")}</strong></div>
      <div style="overflow-x:auto;margin-top:8px;">
        <table class="data-table" id="data-table">
          <thead>
            <tr>${headers.map((h) => `<th data-key="${h}">${h}</th>`).join("")}</tr>
          </thead>
          <tbody>
            ${rows
              .map(
                (row) =>
                  `<tr>${headers.map((h) => `<td>${row[h] ?? ""}</td>`).join("")}</tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `;
    enableSort("data-table");
  };

  sheets.forEach((name) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tab" + (name === active ? " active" : "");
    btn.textContent = name.replace(/_/g, " ");
    btn.addEventListener("click", () => {
      active = name;
      document.querySelectorAll(".tab").forEach((el) => el.classList.remove("active"));
      btn.classList.add("active");
      renderSheet(name);
    });
    tabsContainer.appendChild(btn);
  });

  renderSheet(active);
}

function enableSort(tableId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const headers = table.querySelectorAll("th");
  headers.forEach((th, idx) => {
    th.addEventListener("click", () => {
      const rows = Array.from(table.querySelectorAll("tbody tr"));
      const asc = th.classList.toggle("asc");
      headers.forEach((h) => {
        if (h !== th) h.classList.remove("asc", "desc");
      });
      th.classList.toggle("desc", !asc);
      rows.sort((a, b) => {
        const av = a.children[idx].textContent || "";
        const bv = b.children[idx].textContent || "";
        const na = parseFloat(av.replace(/[^\d.-]/g, ""));
        const nb = parseFloat(bv.replace(/[^\d.-]/g, ""));
        const aNum = !isNaN(na);
        const bNum = !isNaN(nb);
        if (aNum && bNum) {
          return asc ? na - nb : nb - na;
        }
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      });
      const tbody = table.querySelector("tbody");
      rows.forEach((row) => tbody.appendChild(row));
    });
  });
}

function ensureLegsMatchType() {
  const type = flightTypeSelect.value;
  if (type === "one-way") {
    legs = [legs[0] || createLeg()];
  } else if (type === "round-trip") {
    while (legs.length < 2) legs.push(createLeg());
    legs = legs.slice(0, 2);
  } else if (type === "multiple-legs" && legs.length === 0) {
    legs = [createLeg()];
  }
  renderLegs();
  addFlightBtn.style.display = type === "multiple-legs" ? "inline-flex" : "none";
}

async function loadAirlines() {
  try {
    const res = await fetch("/airlines.json");
    const data = await res.json();
    airlineSelect.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "Select airline (optional)";
    airlineSelect.appendChild(blank);
    data.forEach((item) => {
      const opt = document.createElement("option");
      opt.value = item.value;
      opt.textContent = item.label || item.value;
      opt.disabled = item.disabled;
      airlineSelect.appendChild(opt);
    });
  } catch (err) {
    airlineSelect.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Airlines unavailable";
    airlineSelect.appendChild(opt);
    appendLog("Could not load airlines.json: " + err.message);
  }
}

function applySampleToForm(sample) {
  flightTypeSelect.value = sample.flight_type || "one-way";
  travelStatusSelect.value = sample.travel_status || "R2 Standby";
  airlineSelect.value = sample.airline || "";
  legs = (sample.trips || []).map((trip, idx) =>
    createLeg({
      origin: trip.origin,
      destination: trip.destination,
      date: (sample.itinerary || [])[idx]?.date || "",
      time: (sample.itinerary || [])[idx]?.time || "",
      class: (sample.itinerary || [])[idx]?.class || "Economy",
    })
  );
  if (!legs.length) legs = [createLeg()];
  ensureLegsMatchType();
}

loadSampleBtn.addEventListener("click", () => {
  rawJson.value = JSON.stringify(sampleInput, null, 2);
  applySampleToForm(sampleInput);
});

loadFileBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (evt) => {
    rawJson.value = evt.target.result;
  };
  reader.readAsText(file);
});

downloadJsonBtn.addEventListener("click", () => download("report_json"));
downloadXlsxBtn.addEventListener("click", () => download("report_excel"));
form.addEventListener("submit", startRun);
flightTypeSelect.addEventListener("change", ensureLegsMatchType);
addFlightBtn.addEventListener("click", () => {
  if (flightTypeSelect.value !== "multiple-legs") return;
  legs.push(createLeg());
  renderLegs();
});

ensureLegsMatchType();
renderLegs();
loadAirlines();
appendLog("Ready. Fill the form or drop a JSON payload.");
