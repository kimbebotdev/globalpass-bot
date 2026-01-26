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
const toggleTravelPartnersBtn = document.getElementById("toggle-travel-partners");
const addTravelPartnerBtn = document.getElementById("add-travel-partner");
const travelPartnersSection = document.getElementById("travel-partners-section");
const travelPartnersContainer = document.getElementById("travel-partners-container");
const travelPartnersCount = document.getElementById("travel-partners-count");
const inputSourceForm = document.getElementById("input-source-form");
const inputSourceJson = document.getElementById("input-source-json");
const formInputSection = document.getElementById("form-input-section");
const jsonInputSection = document.getElementById("json-input-section");
const headedToggle = document.getElementById("headed-toggle");
const findFlightToggle = document.getElementById("find-flight-toggle");
const findFlightContent = document.getElementById("find-flight-content");
const defaultContent = document.getElementById("default-content");
const findFlightAirlineSelect = document.getElementById("find-flight-airline");
const findFlightForm = document.getElementById("find-flight-form");
const findFlightResults = document.getElementById("find-flight-results");
const findFlightHeadedToggle = document.getElementById("find-flight-headed-toggle");

const classOptions = ["Economy", "Premium Economy", "Business", "First"];
const timeOptions = Array.from(
  { length: 24 },
  (_, h) => `${h.toString().padStart(2, "0")}:00`
);
const isoToMmddyyyy = (val) => {
  if (!val) return "";
  if (val.includes("/")) return val;
  const parts = val.split("-");
  if (parts.length === 3) {
    return `${parts[1].padStart(2, "0")}/${parts[2].padStart(2, "0")}/${
      parts[0]
    }`;
  }
  return val;
};
const mmddyyyyToIso = (val) => {
  if (!val) return "";
  if (val.includes("-")) return val;
  const parts = val.split("/");
  if (parts.length === 3) {
    return `${parts[2]}-${parts[0].padStart(2, "0")}-${parts[1].padStart(
      2,
      "0"
    )}`;
  }
  return val;
};

let ws;
let currentRunId = null;
let legs = [createLeg()];
let travelPartners = [];

const sampleInput = {
  flight_type: "one-way",
  nonstop_flights: true,
  airline: "",
  travel_status: "Bookable",
  trips: [
    {
      origin: "DXB",
      destination: "SIN",
    },
  ],
  itinerary: [
    {
      date: "02/01/2026",
      time: "00:00",
      class: "Economy",
    },
  ],
  traveller: [
    {
      name: "Rafael Cruz",
      salutation: "MR",
      checked: true,
    },
  ],
  travel_partner: [],
};

function createLeg(overrides = {}) {
  return {
    origin: "",
    destination: "",
    date: "",
    time: "",
    class: "Economy",
    ...overrides,
  };
}

function createPartner(overrides = {}) {
  return {
    type: "Adult",
    salutation: "MR",
    first_name: "",
    last_name: "",
    dob: "",
    own_seat: true,
    ...overrides,
  };
}

function setStatus(status, text) {
  statusPill.className =
    "pill " +
    (status === "completed" ? "ok" : status === "error" ? "error" : "pending");
  statusPill.textContent = text || status;
  statusLabel.textContent = text || status;
}

function appendLog(msg) {
  const line = document.createElement("div");
  line.textContent = msg;
  logFeed.appendChild(line);
  logFeed.scrollTop = logFeed.scrollHeight;
}

function showToast(message, type = "error") {
  if (window.Toastify) {
    Toastify({
      text: message,
      duration: 4500,
      gravity: "top",
      position: "right",
      close: true,
      style: {
        background: type === "error" ? "#d65b4a" : "#2e8b57",
      },
    }).showToast();
  } else {
    appendLog(message);
  }
}

function validateInput(input) {
  const errors = [];
  if (!input || typeof input !== "object") {
    errors.push("Input must be a JSON object.");
    return errors;
  }
  const flightType = (input.flight_type || "").trim();
  const travelStatus = (input.travel_status || "").trim();
  if (!flightType) errors.push("flight_type is required.");
  if (!travelStatus) errors.push("travel_status is required.");

  const trips = Array.isArray(input.trips) ? input.trips : [];
  const itinerary = Array.isArray(input.itinerary) ? input.itinerary : [];
  const partners = Array.isArray(input.travel_partner) ? input.travel_partner : [];

  if (flightType === "one-way") {
    if (trips.length < 1) errors.push("one-way requires at least 1 trip.");
    if (itinerary.length < 1) errors.push("one-way requires at least 1 itinerary entry.");
  } else if (flightType === "round-trip") {
    if (trips.length < 2) errors.push("round-trip requires 2 trips.");
    if (itinerary.length < 2) errors.push("round-trip requires 2 itinerary entries.");
  } else if (flightType === "multiple-legs") {
    if (trips.length < 1) errors.push("multiple-legs requires at least 1 trip.");
    if (itinerary.length < 1) errors.push("multiple-legs requires at least 1 itinerary entry.");
  }

  partners.forEach((partner, idx) => {
    const label = `Travel partner ${idx + 1}`;
    if (!partner || typeof partner !== "object") {
      errors.push(`${label}: details are missing or invalid.`);
      return;
    }
    const type = (partner.type || "").trim();
    const firstName = (partner.first_name || "").trim();
    const lastName = (partner.last_name || "").trim();
    const salutation = (partner.salutation || "").trim();
    const dob = (partner.dob || "").trim();
    const ownSeat = partner.own_seat;
    if (!type) errors.push(`${label}: type is required.`);
    if (!firstName) errors.push(`${label}: first name is required.`);
    if (!lastName) errors.push(`${label}: last name is required.`);
    if (type === "Adult") {
      if (!salutation) errors.push(`${label}: salutation is required for adults.`);
    }
    if (type === "Child") {
      if (!dob) errors.push(`${label}: date of birth is required for children.`);
    }
    if (typeof ownSeat !== "boolean") {
      errors.push(`${label}: own seat must be checked or unchecked.`);
    }
  });

  return errors;
}

function buildPayload() {
  const useJson = inputSourceJson?.checked;
  const raw = rawJson.value.trim();
  if (useJson) {
    if (!raw) {
      throw new Error("Raw JSON is required when JSON input is selected.");
    }
    try {
      const parsed = JSON.parse(raw);
      return {
        input: parsed,
        headed: document.getElementById("headed")?.checked || false,
      };
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
      throw new Error(
        `Leg ${i + 1}: origin, destination, date, and time are required`
      );
    }
  }
  const input = {
    flight_type: flightType,
    trips,
    itinerary,
    airline: airlineSelect.value,
    travel_status: travelStatusSelect.value,
    nonstop_flights: document.getElementById("nonstop_flights").checked,
    travel_partner: travelPartners.map((p) => ({
      type: p.type,
      salutation: p.type === "Adult" ? p.salutation : undefined,
      first_name: p.first_name,
      last_name: p.last_name,
      dob: p.type === "Child" ? p.dob : undefined,
      own_seat: p.own_seat,
    })),
  };
  return { input, headed: document.getElementById("headed").checked };
}

function setInputMode() {
  const useJson = inputSourceJson?.checked;
  if (useJson) {
    formInputSection.style.display = 'none';
    jsonInputSection.style.display = 'block';
  } else {
    formInputSection.style.display = 'block';
    jsonInputSection.style.display = 'none';
  }
}

function buildFindFlightPayload() {
  const flightNumber = document.getElementById("find-flight-code")?.value.trim();
  const flightType = document.getElementById("find-flight-type")?.value.trim();
  const airline = document.getElementById("find-flight-airline")?.value.trim();
  const origin = document.getElementById("find-flight-origin")?.value.trim();
  const destination = document.getElementById("find-flight-destination")?.value.trim();
  const date = document.getElementById("find-flight-date")?.value;
  const time = document.getElementById("find-flight-time")?.value.trim();
  const travelClass = document.getElementById("find-flight-class")?.value.trim();

  const missing = [];
  if (!flightNumber) missing.push("Flight Number is required.");
  if (!flightType) missing.push("Flight Type is required.");
  const airlineValue = airline || "";
  if (!origin) missing.push("Origin is required.");
  if (!destination) missing.push("Destination is required.");
  if (!date) missing.push("Date is required.");
  if (!time) missing.push("Time is required.");
  if (!travelClass) missing.push("Class is required.");
  if (missing.length) {
    missing.forEach((msg) => showToast(msg));
    throw new Error("Missing required fields.");
  }

  return {
    input: {
      flight_type: flightType,
      nonstop_flights: document.getElementById("find-flight-nonstop")?.checked || false,
      airline: airlineValue,
      travel_status: "Bookable",
      trips: [{ origin, destination }],
      itinerary: [{ date: isoToMmddyyyy(date), time, class: travelClass }],
      flight_number: flightNumber.replace(/\s+/g, ""),
    },
    headed: document.getElementById("find-flight-headed")?.checked || false,
  };
}

function renderFindFlightCard(data, title, isStaff) {
  if (!data) {
    return `
      <div class="dashboard-card">
        <div class="status-header">
          <div class="flight-meta">
            <span class="airline-tag">${title}</span>
            <span class="on-time">● NO DATA</span>
          </div>
        </div>
      </div>
    `;
  }

  const airline = data.airline || "Unknown Airline";
  const flightNumber = data.flight_number || data.flightNumber || "N/A";
  const origin = data.origin || "N/A";
  const destination = data.destination || "N/A";
  const depart = data.depart_time || data.departure_time || data.time || "N/A";
  const arrive = data.arrival_time || data.arrival || "N/A";
  const aircraft = data.aircraft || "N/A";
  const duration = data.duration || "N/A";

  let loadsHtml = "";
  if (isStaff && data.seats) {
    loadsHtml = `
      <div class="loads-container">
        <span class="section-label">Staff Availability (Loads)</span>
        <div class="loads-grid">
          <div class="load-pill">
            <span>BUSINESS</span>
            <strong class="load-low">${data.seats.bus || "-"}</strong>
          </div>
          <div class="load-pill">
            <span>ECONOMY</span>
            <strong class="load-high">${data.seats.eco || "-"}</strong>
          </div>
          <div class="load-pill">
            <span>NON-REV</span>
            <strong>${data.seats.non_rev || "-"}</strong>
          </div>
        </div>
      </div>
    `;
  } else {
    loadsHtml = `
      <div class="loads-container">
        <span class="section-label">Commercial Details</span>
        <div class="loads-grid">
          <div class="load-pill">
            <span>SEATS</span>
            <strong>${data.seats_available || "-"}</strong>
          </div>
          <div class="load-pill">
            <span>STOPS</span>
            <strong>${data.stops || "-"}</strong>
          </div>
          <div class="load-pill">
            <span>EMISSIONS</span>
            <strong>${data.emissions || "-"}</strong>
          </div>
        </div>
      </div>
    `;
  }

  return `
    <div class="dashboard-card">
      <div class="status-header">
        <div class="flight-meta">
          <span class="airline-tag">${title}: ${airline} · ${flightNumber}</span>
          <span class="on-time">● ON TIME</span>
        </div>
        <div class="timeline">
          <div class="node">
            <h2>${origin}</h2>
            <p>${depart}</p>
          </div>
          <div class="line"></div>
          <div class="node">
            <h2>${destination}</h2>
            <p>${arrive}</p>
          </div>
        </div>
      </div>
      ${loadsHtml}
      <div class="footer-info">
        <span><strong>Aircraft:</strong> ${aircraft}</span>
        <span><strong>Duration:</strong> ${duration}</span>
      </div>
    </div>
  `;
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
    showToast(err.message);
    appendLog(err.message);
    setStatus("error", "invalid input");
    return;
  }

  const validationErrors = validateInput(payload.input);
  if (validationErrors.length) {
    validationErrors.forEach((msg) => showToast(msg));
    appendLog(validationErrors.join(" "));
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

async function startFindFlight(event) {
  event?.preventDefault();
  if (!findFlightResults) return;
  findFlightResults.classList.remove("empty-state");
  findFlightResults.innerHTML = "Searching for matching flight details...";
  let payload;
  try {
    payload = buildFindFlightPayload();
  } catch (err) {
    return;
  }

  const res = await fetch("/api/find-flight", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok || data.status === "error") {
    showToast("Find Flight failed. Check server logs.");
    findFlightResults.classList.add("empty-state");
    findFlightResults.textContent = "Run a search to load flight details.";
    return;
  }

  const googleEntry = (data.google_flights || [])[0];
  const googleFlight = googleEntry?.flights?.top_flights?.[0] || null;
  const staffFlight = (data.stafftraveler || [])[0] || null;

  findFlightResults.innerHTML = `
    ${renderFindFlightCard(googleFlight, "Google Flights", false)}
    ${renderFindFlightCard(staffFlight, "StaffTraveler", true)}
  `;
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
      title.textContent =
        idx === 0
          ? "Departure leg"
          : idx === 1
          ? "Return leg"
          : `Leg ${idx + 1}`;
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

    grid.appendChild(
      makeInput("Origin", leg.origin, "e.g. DXB", (val) => (leg.origin = val))
    );
    grid.appendChild(
      makeInput(
        "Destination",
        leg.destination,
        "e.g. SIN",
        (val) => (leg.destination = val)
      )
    );
    grid.appendChild(makeDateInput(leg));
    grid.appendChild(makeTimeSelect(leg));
    grid.appendChild(makeClassSelect(leg));

    card.appendChild(grid);
    legsContainer.appendChild(card);
  });
}

function renderTravelPartners() {
  if (!travelPartnersContainer) return;
  travelPartnersContainer.innerHTML = "";
  if (travelPartnersCount) {
    travelPartnersCount.textContent = String(travelPartners.length);
  }
  travelPartners.forEach((partner, idx) => {
    const card = document.createElement("div");
    card.className = "leg-card";

    const row = document.createElement("div");
    row.className = "leg-row";
    const title = document.createElement("div");
    title.className = "leg-title";
    title.textContent = `Partner ${idx + 1}`;
    row.appendChild(title);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "remove-btn";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => {
      travelPartners.splice(idx, 1);
      renderTravelPartners();
    });
    row.appendChild(removeBtn);
    card.appendChild(row);

    const grid = document.createElement("div");
    grid.className = "leg-grid";

    grid.appendChild(
      makeSelect("Type", ["Adult", "Child"], partner.type, (val) => {
        partner.type = val;
        if (partner.type === "Adult" && !partner.salutation) {
          partner.salutation = "MR";
        }
        renderTravelPartners();
      })
    );

    if (partner.type === "Adult") {
      grid.appendChild(
        makeSelect("Salutation", ["MR", "MS"], partner.salutation || "MR", (val) => {
          partner.salutation = val;
        })
      );
    }

    grid.appendChild(
      makeInput("First Name", partner.first_name, "e.g. Alex", (val) => {
        partner.first_name = val;
      })
    );
    grid.appendChild(
      makeInput("Last Name", partner.last_name, "e.g. Cruz", (val) => {
        partner.last_name = val;
      })
    );

    if (partner.type === "Child") {
      grid.appendChild(makePartnerDateInput(partner));
    }

    grid.appendChild(
      makeCheckbox("Own Seat", partner.own_seat, (val) => {
        partner.own_seat = val;
      })
    );

    card.appendChild(grid);
    travelPartnersContainer.appendChild(card);
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

function makeSelect(labelText, options, value, onChange) {
  const wrap = document.createElement("label");
  wrap.textContent = labelText;
  const select = document.createElement("select");
  options.forEach((opt) => {
    const option = document.createElement("option");
    option.value = opt;
    option.textContent = opt;
    select.appendChild(option);
  });
  select.value = value || options[0];
  select.addEventListener("change", (e) => onChange(e.target.value));
  wrap.appendChild(select);
  return wrap;
}

function makeCheckbox(labelText, checked, onChange) {
  const wrap = document.createElement("label");
  wrap.style.flexDirection = "row";
  wrap.style.alignItems = "center";
  wrap.style.gap = "10px";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(checked);
  input.addEventListener("change", (e) => onChange(e.target.checked));
  const text = document.createElement("span");
  text.textContent = labelText;
  wrap.appendChild(input);
  wrap.appendChild(text);
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

function makePartnerDateInput(partner) {
  const wrap = document.createElement("label");
  wrap.textContent = "DOB";
  const input = document.createElement("input");
  input.type = "date";
  input.value = partner.dob ? mmddyyyyToIso(partner.dob) : "";
  input.addEventListener("change", (e) => {
    partner.dob = isoToMmddyyyy(e.target.value);
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
      <div class="tab-title">${sheetName
        .replace(/_/g, " ")
        .replace(/ All$/i, "")}</div>
      <div class="table-responsive">
        <table class="data-table" id="data-table">
          <thead>
            <tr>${headers
              .map((h) => `<th data-key="${h}">${h}</th>`)
              .join("")}</tr>
          </thead>
          <tbody>
            ${rows
              .map(
                (row) =>
                  `<tr>${headers
                    .map((h) => `<td>${row[h] ?? ""}</td>`)
                    .join("")}</tr>`
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
      document
        .querySelectorAll(".tab")
        .forEach((el) => el.classList.remove("active"));
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
  addFlightBtn.style.display =
    type === "multiple-legs" ? "inline-flex" : "none";
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
    if (findFlightAirlineSelect) {
      findFlightAirlineSelect.innerHTML = "";
      const blankFind = document.createElement("option");
      blankFind.value = "";
      blankFind.textContent = "Select airline (optional)";
      findFlightAirlineSelect.appendChild(blankFind);
      data.forEach((item) => {
        const opt = document.createElement("option");
        opt.value = item.value;
        opt.textContent = item.label || item.value;
        opt.disabled = item.disabled;
        findFlightAirlineSelect.appendChild(opt);
      });
    }
  } catch (err) {
    airlineSelect.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Airlines unavailable";
    airlineSelect.appendChild(opt);
    if (findFlightAirlineSelect) {
      findFlightAirlineSelect.innerHTML = "";
      const fallbackOpt = document.createElement("option");
      fallbackOpt.value = "";
      fallbackOpt.textContent = "Airlines unavailable";
      findFlightAirlineSelect.appendChild(fallbackOpt);
    }
    appendLog("Could not load airlines.json: " + err.message);
  }
}

loadSampleBtn.addEventListener("click", () => {
  rawJson.value = JSON.stringify(sampleInput, null, 2);
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

downloadJsonBtn.addEventListener("click", () => download("json"));
downloadXlsxBtn.addEventListener("click", () => download("excel"));
form.addEventListener("submit", startRun);
findFlightForm?.addEventListener("submit", startFindFlight);
document.getElementById("find-flight-search")?.addEventListener("click", startFindFlight);
flightTypeSelect.addEventListener("change", ensureLegsMatchType);
inputSourceForm?.addEventListener("change", setInputMode);
inputSourceJson?.addEventListener("change", setInputMode);
addFlightBtn.addEventListener("click", () => {
  if (flightTypeSelect.value !== "multiple-legs") return;
  legs.push(createLeg());
  renderLegs();
});
toggleTravelPartnersBtn?.addEventListener("click", () => {
  if (!travelPartnersSection) return;
  const isHidden = travelPartnersSection.style.display === "none";
  travelPartnersSection.style.display = isHidden ? "block" : "none";
  if (toggleTravelPartnersBtn) {
    toggleTravelPartnersBtn.textContent = isHidden ? "Hide" : "Show";
  }
});
addTravelPartnerBtn?.addEventListener("click", () => {
  travelPartners.push(createPartner());
  renderTravelPartners();
});

ensureLegsMatchType();
renderLegs();
renderTravelPartners();
loadAirlines();
setInputMode();
const isDevHost = ["localhost", "127.0.0.1"].includes(window.location.hostname);
if (headedToggle && !isDevHost) {
  headedToggle.style.display = "none";
}
if (findFlightHeadedToggle && !isDevHost) {
  findFlightHeadedToggle.style.display = "none";
}
if (findFlightToggle && findFlightContent && defaultContent) {
  findFlightToggle.addEventListener("click", () => {
    const showFind = findFlightContent.style.display === "none";
    findFlightContent.style.display = showFind ? "block" : "none";
    defaultContent.style.display = showFind ? "none" : "grid";
    findFlightToggle.textContent = showFind ? "Search Flights" : "Search Flight Number";
  });
}
appendLog("Ready. Fill the form or drop a JSON payload.");
