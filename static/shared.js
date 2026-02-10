const form = document.getElementById("run-form");
const logFeed = document.getElementById("log-feed");
const statusPill = document.getElementById("status-pill");
const statusLabel = document.getElementById("status-label");
const runIdEl = document.getElementById("run-id");
const resultsBlock = document.getElementById("results-block");
const fileList = document.getElementById("file-list");
const downloadXlsxBtn = document.getElementById("download-xlsx");
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
const formInputSection = document.getElementById("form-input-section");
const accountSelect = document.getElementById("account-select");
const accountDependent = document.getElementById("account-dependent");
const travelPartnersModal = document.getElementById("travel-partners-modal");
const modalCloseButtons = travelPartnersModal?.querySelectorAll("[data-modal-close]") || [];
const toggleTravellersBtn = document.getElementById("toggle-travellers");
const travellerList = document.getElementById("traveller-list");
const runBtn = document.getElementById("run-btn");
const headedToggle = document.getElementById("headed-toggle");
const findFlightToggle = document.getElementById("find-flight-toggle");
const findFlightContent = document.getElementById("find-flight-content");
const defaultContent = document.getElementById("default-content");
const findFlightAirlineSelect = document.getElementById("find-flight-airline");
const findFlightForm = document.getElementById("find-flight-form");
const findFlightResults = document.getElementById("find-flight-results");
const findFlightHeadedToggle = document.getElementById("find-flight-headed-toggle");
const findStatusLabel = document.getElementById("find-status-label");
const findFlightAccountSelect = document.getElementById("find-flight-account");
const findFlightDependent = document.getElementById("find-flight-dependent");
const findFlightAddLeg = document.getElementById("find-flight-add-leg");
const findFlightLegsContainer = document.getElementById("find-flight-legs");
const findFlightSearchBtn = document.getElementById("find-flight-search");
const findDownloadXlsxBtn = document.getElementById("download-find-xlsx");

const botProgress = {
  myidtravel: {
    bar: document.getElementById("bot-progress-myidtravel"),
    status: document.getElementById("bot-status-myidtravel"),
    caption: document.getElementById("bot-caption-myidtravel"),
    card: document.querySelector('[data-bot="myidtravel"]'),
  },
  google_flights: {
    bar: document.getElementById("bot-progress-google_flights"),
    status: document.getElementById("bot-status-google_flights"),
    caption: document.getElementById("bot-caption-google_flights"),
    card: document.querySelector('[data-bot="google_flights"]'),
  },
  stafftraveler: {
    bar: document.getElementById("bot-progress-stafftraveler"),
    status: document.getElementById("bot-status-stafftraveler"),
    caption: document.getElementById("bot-caption-stafftraveler"),
    card: document.querySelector('[data-bot="stafftraveler"]'),
  },
};

const findFlightBotProgress = {
  google_flights: {
    bar: document.getElementById("bot-progress-find-google_flights"),
    status: document.getElementById("bot-status-find-google_flights"),
    caption: document.getElementById("bot-caption-find-google_flights"),
  },
  stafftraveler: {
    bar: document.getElementById("bot-progress-find-stafftraveler"),
    status: document.getElementById("bot-status-find-stafftraveler"),
    caption: document.getElementById("bot-caption-find-stafftraveler"),
  },
};

const classOptions = ["Economy", "Premium Economy", "Business", "First"];
const timeOptions = Array.from(
  { length: 24 },
  (_, h) => `${h.toString().padStart(2, "0")}:00`
);
const { isoToMmddyyyy, mmddyyyyToIso, showToast } = window.GlobalpassCommon || {};

let ws;
let currentRunId = null;
let findFlightRunId = null;
let findWs;
let legs = [createLeg()];
let findFlightLegs = [createFindFlightLeg()];
let travelPartners = [];
let selectedTravellers = [];
let accountTravellers = [];
const accountById = new Map();

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

function createFindFlightLeg(overrides = {}) {
  return {
    flight_number: "",
    origin: "",
    destination: "",
    date: "",
    time: "",
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

function setFindBotProgress(bot, status = "running", percent = 0, caption = "") {
  const entry = findFlightBotProgress[bot];
  if (!entry) return;
  if (entry.bar) entry.bar.style.width = `${percent}%`;
  if (entry.status) entry.status.textContent = status;
  if (entry.caption) entry.caption.textContent = caption || "";
}

function appendLog(msg) {
  if (!logFeed) return;
  const line = document.createElement("div");
  line.textContent = msg;
  logFeed.appendChild(line);
  logFeed.scrollTop = logFeed.scrollHeight;
}

function setBotProgress(botKey, state, percentOverride = null, captionText = "") {
  const entry = botProgress[botKey];
  if (!entry) return;
  const { bar, status, caption, card } = entry;
  let percent = 0;
  let label = "idle";
  if (state === "running") {
    percent = 55;
    label = "running";
  } else if (state === "done") {
    percent = 100;
    label = "done";
  } else if (state === "error") {
    percent = 100;
    label = "error";
  }
  if (typeof percentOverride === "number") {
    percent = percentOverride;
  }
  if (bar) {
    bar.style.width = `${percent}%`;
    bar.classList.toggle("is-running", state === "running");
  }
  if (status) {
    status.textContent = label;
  }
  if (caption) {
    if (captionText) {
      caption.textContent = captionText;
    } else if (state === "idle") {
      caption.textContent = "Waiting for a new run to start.";
    } else if (state === "done") {
      caption.textContent = "";
    } else {
      caption.textContent = "Working on the current step.";
    }
  }
  if (card) {
    card.dataset.state = state === "error" ? "error" : "";
  }
}

function resetBotProgress() {
  Object.keys(botProgress).forEach((key) =>
    setBotProgress(key, "idle", 0, "Waiting for a new run to start.")
  );
}

function setAccountDependentVisibility() {
  if (!accountSelect || !accountDependent) return;
  const hasAccount = Boolean(accountSelect.value);
  accountDependent.style.display = hasAccount ? "block" : "none";
  if (runBtn) {
    runBtn.disabled = !hasAccount;
  }
  if (addTravelPartnerBtn) {
    addTravelPartnerBtn.disabled = !hasAccount || travelPartners.length >= 2;
  }
  if (!hasAccount) {
    selectedTravellers = [];
    accountTravellers = [];
    renderTravellerList();
    closeTravelPartnersModal();
  }
  updateTravelPartnersCount();
}

function setFindFlightDependentVisibility() {
  if (!findFlightAccountSelect || !findFlightDependent) return;
  const hasAccount = Boolean(findFlightAccountSelect.value);
  findFlightDependent.style.display = hasAccount ? "block" : "none";
  if (findFlightSearchBtn) {
    findFlightSearchBtn.disabled = !hasAccount;
  }
}

async function loadAccounts() {
  if (!accountSelect) return;
  try {
    const res = await fetch("/api/accounts");
    const data = await res.json();
    const accounts = data.accounts || [];
    accountById.clear();
    accountSelect.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select account";
    accountSelect.appendChild(placeholder);
    accounts.forEach((account) => {
      const opt = document.createElement("option");
      opt.value = account.id;
      opt.textContent = account.employee_name;
      accountSelect.appendChild(opt);
      accountById.set(Number(account.id), account);
    });
  } catch (err) {
    accountSelect.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Accounts unavailable";
    accountSelect.appendChild(opt);
    appendLog("Could not load accounts: " + err.message);
  }
  setAccountDependentVisibility();
}

async function loadStafftravelerAccounts() {
  if (!findFlightAccountSelect) return;
  try {
    const res = await fetch("/api/stafftraveler-accounts");
    const data = await res.json();
    const accounts = data.accounts || [];
    findFlightAccountSelect.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select account";
    findFlightAccountSelect.appendChild(placeholder);
    accounts.forEach((account) => {
      const opt = document.createElement("option");
      opt.value = account.id;
      opt.textContent = account.employee_name;
      findFlightAccountSelect.appendChild(opt);
    });
  } catch (err) {
    findFlightAccountSelect.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Accounts unavailable";
    findFlightAccountSelect.appendChild(opt);
    appendLog("Could not load StaffTraveler accounts: " + err.message);
  }
  setFindFlightDependentVisibility();
}

function updateProgressFromLog(message) {
  const match = message.match(/\[(myidtravel|google_flights|stafftraveler)\]/i);
  if (!match) return;
  const botKey = match[1].toLowerCase();
  if (message.toLowerCase().includes("starting")) {
    setBotProgress(botKey, "running");
    return;
  }
  if (message.toLowerCase().includes("error")) {
    setBotProgress(botKey, "error");
    return;
  }
  if (message.toLowerCase().includes("finished") || message.toLowerCase().includes("completed")) {
    setBotProgress(botKey, "done");
  }
}

function openTravelPartnersModal() {
  if (!travelPartnersModal) return;
  travelPartnersModal.classList.add("is-open");
  travelPartnersModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  if (travelPartnersSection) {
    travelPartnersSection.style.display = "block";
  }
  if (travellerList) {
    travellerList.style.display = "grid";
  }
}

function closeTravelPartnersModal() {
  if (!travelPartnersModal) return;
  travelPartnersModal.classList.remove("is-open");
  travelPartnersModal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

function updateTravelPartnersCount() {
  if (!travelPartnersCount) return;
  const base = accountSelect?.value ? 1 : 0;
  travelPartnersCount.textContent = String(
    base + travelPartners.length + selectedTravellers.length
  );
}

function cleanTravellerBirthday(value) {
  if (!value) return "Not provided";
  return String(value).replace(/\s*\([^)]*\)\s*/g, "").trim() || "Not provided";
}

function getTravellerKey(traveller, idx) {
  const name = traveller?.name || traveller?.full_name || "";
  const birthday = traveller?.birthday || traveller?.dob || "";
  const relationship = traveller?.relationship || "";
  return `${idx}-${name}-${birthday}-${relationship}`;
}

function renderTravellerList() {
  if (!travellerList) return;
  travellerList.innerHTML = "";
  if (!accountTravellers.length) {
    travellerList.innerHTML = '<div class="leg-subtitle">No travellers found for this account.</div>';
    updateTravelPartnersCount();
    return;
  }
  accountTravellers.forEach((traveller, idx) => {
    const key = getTravellerKey(traveller, idx);
    const name = traveller?.name || traveller?.full_name || "Unnamed traveller";
    const birthday = cleanTravellerBirthday(traveller?.birthday || traveller?.dob);
    const relationship = traveller?.relationship || "Relationship not provided";
    const current = selectedTravellers.find((item) => item.key === key);

    const row = document.createElement("div");
    row.className = "traveller-item";

    const checkWrap = document.createElement("label");
    checkWrap.style.display = "flex";
    checkWrap.style.alignItems = "center";
    checkWrap.style.gap = "10px";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = Boolean(current);
    checkWrap.appendChild(checkbox);

    const meta = document.createElement("div");
    meta.className = "traveller-meta";
    const title = document.createElement("div");
    title.className = "traveller-name";
    title.textContent = name;
    const detail = document.createElement("div");
    detail.className = "traveller-details";
    detail.textContent = `Birthday: ${birthday} · Relationship: ${relationship}`;
    meta.appendChild(title);
    meta.appendChild(detail);

    const actions = document.createElement("div");
    actions.className = "traveller-actions";
    const select = document.createElement("select");
    ["", "MR", "MS"].forEach((opt) => {
      const option = document.createElement("option");
      option.value = opt;
      option.textContent = opt ? opt : "Select";
      select.appendChild(option);
    });
    select.value = current?.salutation || traveller?.salutation || "";
    select.disabled = !current;
    actions.appendChild(select);

    checkbox.addEventListener("change", (event) => {
      if (event.target.checked) {
        if (selectedTravellers.length >= 8) {
          showToast("You can select up to 8 travellers.");
          checkbox.checked = false;
          return;
        }
        selectedTravellers.push({
          key,
          name,
          salutation: select.value || "",
        });
        select.disabled = false;
      } else {
        selectedTravellers = selectedTravellers.filter((item) => item.key !== key);
        select.disabled = true;
      }
      updateTravelPartnersCount();
    });

    select.addEventListener("change", (event) => {
      const entry = selectedTravellers.find((item) => item.key === key);
      if (entry) {
        entry.salutation = event.target.value;
      }
    });

    row.addEventListener("click", (event) => {
      const target = event.target;
      if (target instanceof HTMLSelectElement || target instanceof HTMLOptionElement) {
        return;
      }
      if (target instanceof HTMLInputElement && target.type === "checkbox") {
        return;
      }
      checkbox.checked = !checkbox.checked;
      checkbox.dispatchEvent(new Event("change", { bubbles: true }));
    });

    row.appendChild(checkWrap);
    row.appendChild(meta);
    row.appendChild(actions);
    travellerList.appendChild(row);
  });
  updateTravelPartnersCount();
}

function validateInput(input) {
  const errors = [];
  if (!input || typeof input !== "object") {
    errors.push("Input must be a JSON object.");
    return errors;
  }
  const flightType = (input.flight_type || "").trim();
  const travelStatus = (input.travel_status || "").trim();
  if (!input.account_id) errors.push("Account selection is required.");
  if (!flightType) errors.push("flight_type is required.");
  if (!travelStatus) errors.push("travel_status is required.");

  const trips = Array.isArray(input.trips) ? input.trips : [];
  const itinerary = Array.isArray(input.itinerary) ? input.itinerary : [];
  const partners = Array.isArray(input.travel_partner) ? input.travel_partner : [];
  const travellers = Array.isArray(input.traveller) ? input.traveller : [];

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
  if (travellers.length > 8) errors.push("You can select up to 8 travellers.");
  if (partners.length > 2) errors.push("You can add up to 2 travel partners.");

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
  const flightType = flightTypeSelect.value;
  const accountId = accountSelect?.value?.trim() || "";
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
    account_id: accountId ? Number(accountId) : null,
    flight_type: flightType,
    trips,
    itinerary,
    airline: airlineSelect.value,
    travel_status: travelStatusSelect.value,
    nonstop_flights: document.getElementById("nonstop_flights").checked,
    auto_request_stafftraveler: document.getElementById("auto_request_stafftraveler").checked,
    traveller: selectedTravellers.map((traveller) => ({
      name: traveller.name,
      salutation: traveller.salutation || "",
      checked: true,
    })),
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

function buildFindFlightPayload() {
  const accountId = findFlightAccountSelect?.value?.trim() || "";
  const flightType = document.getElementById("find-flight-type")?.value.trim();
  const airline = document.getElementById("find-flight-airline")?.value.trim();
  const travelClass = document.getElementById("find-flight-class")?.value.trim();

  const missing = [];
  if (!accountId) missing.push("StaffTraveler account is required.");
  if (!flightType) missing.push("Flight Type is required.");
  if (!travelClass) missing.push("Class is required.");
  const airlineValue = airline || "";
  findFlightLegs.forEach((leg, idx) => {
    if (!leg.flight_number) missing.push(`Leg ${idx + 1}: Flight number is required.`);
    if (!leg.origin) missing.push(`Leg ${idx + 1}: Origin is required.`);
    if (!leg.destination) missing.push(`Leg ${idx + 1}: Destination is required.`);
    if (!leg.date) missing.push(`Leg ${idx + 1}: Date is required.`);
    if (!leg.time) missing.push(`Leg ${idx + 1}: Time is required.`);
  });
  if (missing.length) {
    missing.forEach((msg) => showToast(msg));
    throw new Error("Missing required fields.");
  }

  return {
    input: {
      account_id: Number(accountId),
      flight_type: flightType,
      nonstop_flights: document.getElementById("find-flight-nonstop")?.checked || false,
      auto_request_stafftraveler: document.getElementById("find-flight-auto-request")?.checked || false,
      airline: airlineValue,
      travel_status: "Bookable",
      trips: findFlightLegs.map((leg) => ({
        origin: leg.origin.trim(),
        destination: leg.destination.trim(),
      })),
      itinerary: findFlightLegs.map((leg) => ({
        date: isoToMmddyyyy(leg.date),
        time: leg.time.trim(),
        class: travelClass,
      })),
      flight_numbers: findFlightLegs.map((leg) => leg.flight_number.replace(/\s+/g, "")),
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

  if (data.economy || data.business) {
    const econ = data.economy || null;
    const bus = data.business || null;
    const base = econ || bus || {};
    const airline = base.airline || "Unknown Airline";
    const flightNumber = base.flight_number || base.flightNumber || "N/A";
    const origin = base.origin || "N/A";
    const destination = base.destination || "N/A";
    const depart = base.depart_time || base.departure_time || base.time || "N/A";
    const arrive = base.arrival_time || base.arrival || "N/A";
    const aircraft = base.aircraft || "N/A";
    const duration = base.duration || "N/A";
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
        <div class="loads-container">
          <span class="section-label">Google Seats</span>
          <div class="loads-grid">
            <div class="load-pill">
              <span>ECONOMY</span>
              <strong class="load-high">${econ?.seats_available || "-"}</strong>
            </div>
            <div class="load-pill">
              <span>BUSINESS</span>
              <strong class="load-low">${bus?.seats_available || "-"}</strong>
            </div>
          </div>
        </div>
        <div class="footer-info">
          <span><strong>Aircraft:</strong> ${aircraft}</span>
          <span><strong>Duration:</strong> ${duration}</span>
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
            <strong class="load-low">${data.seats.bus || data.seats.business || "-"}</strong>
          </div>
          <div class="load-pill">
            <span>ECONOMY</span>
            <strong class="load-high">${data.seats.eco || "-"}</strong>
          </div>
          <div class="load-pill">
            <span>NON-REV</span>
            <strong>${data.seats.non_rev || data.seats.nonrev || "-"}</strong>
          </div>
          <div class="load-pill">
            <span>ECONOMY+</span>
            <strong>${data.seats.eco_plus || data.seats.ecoplus || "-"}</strong>
          </div>
        </div>
      </div>
    `;
  } else {
    const googleSeats = data.seats?.google_flights || {};
    const hasBuckets = googleSeats.economy || googleSeats.business || googleSeats.first;
    const singleSeat = data.seats_available || "";
    const singleClass = (data.class || "").toUpperCase();
    loadsHtml = `
      <div class="loads-container">
        <span class="section-label">Google Seats</span>
        <div class="loads-grid">
          <div class="load-pill">
            <span>ECONOMY</span>
            <strong class="load-high">${hasBuckets ? googleSeats.economy || "-" : singleClass === "ECONOMY" ? singleSeat || "-" : "-"}</strong>
          </div>
          <div class="load-pill">
            <span>BUSINESS</span>
            <strong class="load-low">${hasBuckets ? googleSeats.business || "-" : singleClass === "BUSINESS" ? singleSeat || "-" : "-"}</strong>
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
  if (logFeed) {
    logFeed.innerHTML = "";
  }
  if (resultsBlock) {
    resultsBlock.textContent = "Running...";
  }
  if (fileList) {
    fileList.innerHTML = "";
  }
  setStatus("pending", "running");
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
  resetBotProgress();
  appendLog(`Run started (${currentRunId})`);
  connectWebSocket(currentRunId);
}

async function startFindFlight(event) {
  event?.preventDefault();
  if (!findFlightResults) return;
  findFlightResults.classList.remove("empty-state");
  findFlightResults.innerHTML = "Searching for matching flight details...";
  findFlightRunId = null;
  if (findDownloadXlsxBtn) {
    findDownloadXlsxBtn.disabled = true;
  }
  if (findStatusLabel) {
    findStatusLabel.textContent = "running";
  }
  setFindBotProgress("google_flights", "running", 25, "Searching for matching flights.");
  setFindBotProgress("stafftraveler", "running", 25, "Checking staff availability.");
  let payload;
  try {
    payload = buildFindFlightPayload();
  } catch (err) {
    setFindBotProgress("google_flights", "error", 0, "Missing required fields.");
    setFindBotProgress("stafftraveler", "error", 0, "Missing required fields.");
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
    setFindBotProgress("google_flights", "error", 0, "Lookup failed. Check logs.");
    setFindBotProgress("stafftraveler", "error", 0, "Lookup failed. Check logs.");
    if (findStatusLabel) {
      findStatusLabel.textContent = "error";
    }
    return;
  }
  findFlightRunId = data.run_id || null;
  if (findFlightRunId) {
    connectFindFlightWebSocket(findFlightRunId);
  }
}

function connectWebSocket(runId) {
  if (!runId) return;
  if (ws) ws.close();
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${protocol}://${location.host}/ws/${runId}`);

  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "log") {
      if (payload.message) {
        updateProgressFromLog(payload.message);
      }
      appendLog(`${payload.ts || ""} ${payload.message}`);
    } else if (payload.type === "progress") {
      const botKey = payload.bot;
      const percent = Number(payload.percent || 0);
      const stepKey = payload.status || "running";
      const state =
        stepKey === "done" ? "done" : stepKey === "error" ? "error" : "running";
      const captions = {
        launching: "Launching the browser and preparing the session.",
        loaded: "Page loaded successfully. Preparing to fill the form.",
        "form filled": "Form inputs completed. Getting ready to submit the search.",
        submitted: "Search submitted. Waiting for results to load.",
        "results loaded": "Results are loaded. Extracting flight details.",
        parsed: "Parsing results into a structured response.",
        screenshot: "Capturing the final screenshot for this bot.",
        done: "Finished successfully and ready for review.",
        error: "Stopped due to an error. Check logs for details.",
        running: "Working on the current step.",
        starting: "Starting the bot workflow.",
      };
      const captionText = captions[stepKey] || "Working on the current step.";
      setBotProgress(botKey, state, percent, captionText);
    } else if (payload.type === "status") {
      if (payload.status === "completed") {
        setStatus("completed", "done");
        Object.keys(botProgress).forEach((key) => {
          const current = botProgress[key]?.status?.textContent;
          if (current && current !== "done") {
            setBotProgress(key, "done");
          }
        });
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

async function fetchFindFlightResults(runId) {
  if (!runId || !findFlightResults) return;
  const res = await fetch(`/api/find-flight/${runId}`);
  if (!res.ok) {
    findFlightResults.classList.add("empty-state");
    findFlightResults.textContent = "Unable to load flight results.";
    return;
  }
  const data = await res.json();
  const legsResults = data.legs_results || [];
  if (!legsResults.length) {
    findFlightResults.classList.add("empty-state");
    findFlightResults.textContent = "No flight results returned.";
    return;
  }

  findFlightResults.innerHTML = legsResults
    .map((leg, idx) => {
  const googleFlight = leg.google_flights || null;
  const staffFlight = (leg.stafftraveler || [])[0] || null;
      return `
        <div class="leg-card">
          <div class="leg-row">
            <div class="leg-title">Leg ${idx + 1}</div>
          </div>
          ${renderFindFlightCard(googleFlight, "Google Flights", false)}
          ${renderFindFlightCard(staffFlight, "StaffTraveler", true)}
        </div>
      `;
    })
    .join("");
}

function connectFindFlightWebSocket(runId) {
  if (!runId) return;
  if (findWs) findWs.close();
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  findWs = new WebSocket(`${protocol}://${location.host}/ws/${runId}`);

  findWs.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "progress") {
      const botKey = payload.bot;
      const percent = Number(payload.percent || 0);
      const stepKey = payload.status || "running";
      const state =
        stepKey === "done" ? "done" : stepKey === "error" ? "error" : "running";
      const captions = {
        launching: "Launching the browser and preparing the session.",
        loaded: "Page loaded successfully. Preparing to fill the form.",
        "form filled": "Form inputs completed. Getting ready to submit the search.",
        submitted: "Search submitted. Waiting for results to load.",
        "results loaded": "Results are loaded. Extracting flight details.",
        parsed: "Parsing results into a structured response.",
        screenshot: "Capturing the final screenshot for this bot.",
        done: "Finished successfully and ready for review.",
        error: "Stopped due to an error. Check logs for details.",
        running: "Working on the current step.",
        starting: "Starting the bot workflow.",
      };
      const captionText = captions[stepKey] || "Working on the current step.";
      if (botKey === "google_flights" || botKey === "stafftraveler") {
        setFindBotProgress(botKey, state, percent, captionText);
      }
    } else if (payload.type === "status") {
      if (payload.status === "completed") {
        setFindBotProgress("google_flights", "done", 100, "Finished successfully and ready for review.");
        setFindBotProgress("stafftraveler", "done", 100, "Finished successfully and ready for review.");
        if (findDownloadXlsxBtn) {
          findDownloadXlsxBtn.disabled = false;
        }
        if (findStatusLabel) {
          findStatusLabel.textContent = "done";
        }
        fetchFindFlightResults(runId);
      } else if (payload.status === "error") {
        setFindBotProgress("google_flights", "error", 0, "Lookup failed. Check logs.");
        setFindBotProgress("stafftraveler", "error", 0, "Lookup failed. Check logs.");
        findFlightResults.classList.add("empty-state");
        findFlightResults.textContent = "Lookup failed. Check server logs.";
        if (findStatusLabel) {
          findStatusLabel.textContent = "error";
        }
      } else if (findStatusLabel) {
        findStatusLabel.textContent = payload.status || "running";
      }
    }
  };

  findWs.onclose = () => {
    findWs = null;
  };
}

async function fetchResults(runId) {
  if (!runId) return;
  try {
    const res = await fetch(`/api/runs/${runId}`);
    const data = await res.json();
    if (fileList) {
      fileList.innerHTML = "";
      if (data.files) {
        Object.entries(data.files).forEach(([name, path]) => {
          const chip = document.createElement("span");
          chip.className = "chip";
          chip.textContent = `${name}: ${path}`;
          fileList.appendChild(chip);
        });
      }
    }
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
  link.download = `${currentRunId}.xlsx`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function downloadFindXlsx() {
  if (!findFlightRunId) return;
  const url = `/api/runs/${findFlightRunId}/download/excel`;
  const res = await fetch(url);
  if (!res.ok) {
    appendLog("Download failed (excel)");
    return;
  }
  const blob = await res.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${findFlightRunId}.xlsx`;
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

function renderFindFlightLegs() {
  if (!findFlightLegsContainer) return;
  const type = document.getElementById("find-flight-type")?.value || "one-way";
  findFlightLegsContainer.innerHTML = "";
  findFlightLegs.forEach((leg, idx) => {
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

    if (type === "multiple-legs" && findFlightLegs.length > 1) {
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "remove-btn";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => {
        findFlightLegs.splice(idx, 1);
        renderFindFlightLegs();
      });
      row.appendChild(removeBtn);
    }
    card.appendChild(row);

    const grid = document.createElement("div");
    grid.className = "leg-grid";
    grid.appendChild(
      makeInput(
        "Flight Number",
        leg.flight_number,
        "e.g. EY1",
        (val) => (leg.flight_number = val)
      )
    );
    grid.appendChild(
      makeInput("Origin", leg.origin, "e.g. AUH", (val) => (leg.origin = val))
    );
    grid.appendChild(
      makeInput(
        "Destination",
        leg.destination,
        "e.g. JFK",
        (val) => (leg.destination = val)
      )
    );
    grid.appendChild(makeDateInput(leg));
    grid.appendChild(makeTimeSelect(leg));

    card.appendChild(grid);
    findFlightLegsContainer.appendChild(card);
  });
}

function renderTravelPartners() {
  if (!travelPartnersContainer) return;
  travelPartnersContainer.innerHTML = "";
  updateTravelPartnersCount();
  if (addTravelPartnerBtn) {
    addTravelPartnerBtn.disabled = travelPartners.length >= 2;
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

function ensureFindFlightLegsMatchType() {
  const type = document.getElementById("find-flight-type")?.value || "one-way";
  if (type === "one-way") {
    findFlightLegs = [findFlightLegs[0] || createFindFlightLeg()];
  } else if (type === "round-trip") {
    while (findFlightLegs.length < 2) findFlightLegs.push(createFindFlightLeg());
    findFlightLegs = findFlightLegs.slice(0, 2);
  } else if (type === "multiple-legs" && findFlightLegs.length === 0) {
    findFlightLegs = [createFindFlightLeg()];
  }
  renderFindFlightLegs();
  if (findFlightAddLeg) {
    findFlightAddLeg.style.display = type === "multiple-legs" ? "inline-flex" : "none";
  }
}

async function loadAirlines() {
  try {
    const res = await fetch("/api/airlines");
    const data = await res.json();
    const airlines = data.airlines || [];
    airlineSelect.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "Select airline (optional)";
    airlineSelect.appendChild(blank);
    airlines.forEach((item) => {
      const opt = document.createElement("option");
      opt.value = item.value;
      opt.textContent = item.label
        ? `${item.value} - ${item.label}`
        : item.value;
      opt.disabled = item.disabled;
      airlineSelect.appendChild(opt);
    });
    if (findFlightAirlineSelect) {
      findFlightAirlineSelect.innerHTML = "";
      const blankFind = document.createElement("option");
      blankFind.value = "";
      blankFind.textContent = "Select airline (optional)";
      findFlightAirlineSelect.appendChild(blankFind);
      airlines.forEach((item) => {
        const opt = document.createElement("option");
        opt.value = item.value;
        opt.textContent = item.label
          ? `${item.value} - ${item.label}`
          : item.value;
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
    appendLog("Could not load airlines: " + err.message);
  }
}

function initShared() {
  if (window.__globalpassSharedInit) {
    return;
  }
  window.__globalpassSharedInit = true;
  loadAirlines();
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
}

function initStandardRun() {
  downloadXlsxBtn.addEventListener("click", () => download("excel"));
  form.addEventListener("submit", startRun);
  flightTypeSelect.addEventListener("change", ensureLegsMatchType);
  accountSelect?.addEventListener("change", setAccountDependentVisibility);
  accountSelect?.addEventListener("change", () => {
    const accountId = Number(accountSelect?.value || 0);
    const account = accountById.get(accountId);
    accountTravellers = Array.isArray(account?.travellers) ? account.travellers : [];
    selectedTravellers = [];
    if (toggleTravellersBtn) {
      toggleTravellersBtn.textContent = " + ";
    }
    if (travellerList) {
      travellerList.style.display = "none";
    }
    renderTravellerList();
  });
  addFlightBtn.addEventListener("click", () => {
    if (flightTypeSelect.value !== "multiple-legs") return;
    legs.push(createLeg());
    renderLegs();
  });
  toggleTravelPartnersBtn?.addEventListener("click", () => {
    if (!accountSelect?.value) {
      showToast("Select an account first.");
      return;
    }
    openTravelPartnersModal();
  });
  addTravelPartnerBtn?.addEventListener("click", () => {
    if (travelPartners.length >= 2) {
      showToast("You can add up to 2 travel partners.");
      return;
    }
    travelPartners.push(createPartner());
    renderTravelPartners();
  });
  toggleTravellersBtn?.addEventListener("click", () => {
    if (!travellerList) return;
    const isHidden = travellerList.style.display === "none";
    travellerList.style.display = isHidden ? "grid" : "none";
    if (toggleTravellersBtn) {
      toggleTravellersBtn.textContent = " + ";
    }
  });
  if (travelPartnersModal && modalCloseButtons.length) {
    modalCloseButtons.forEach((btn) => {
      btn.addEventListener("click", closeTravelPartnersModal);
    });
    travelPartnersModal.addEventListener("click", (event) => {
      if (event.target === travelPartnersModal) {
        closeTravelPartnersModal();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && travelPartnersModal.classList.contains("is-open")) {
        closeTravelPartnersModal();
      }
    });
  }

  ensureLegsMatchType();
  renderLegs();
  renderTravelPartners();
  renderTravellerList();
  loadAccounts();
  appendLog("Ready. Fill the form to run the bots.");
}

function initFindFlight() {
  findDownloadXlsxBtn?.addEventListener("click", downloadFindXlsx);
  findFlightForm?.addEventListener("submit", startFindFlight);
  document.getElementById("find-flight-search")?.addEventListener("click", startFindFlight);
  document.getElementById("find-flight-type")?.addEventListener("change", ensureFindFlightLegsMatchType);
  findFlightAccountSelect?.addEventListener("change", setFindFlightDependentVisibility);
  findFlightAddLeg?.addEventListener("click", () => {
    findFlightLegs.push(createFindFlightLeg());
    renderFindFlightLegs();
  });
  ensureFindFlightLegsMatchType();
  loadStafftravelerAccounts();
}

window.Globalpass = {
  initShared,
  initStandardRun,
  initFindFlight,
};
