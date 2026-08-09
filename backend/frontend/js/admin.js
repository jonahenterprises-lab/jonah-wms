import { get, post, put, del } from "./api.js";
import { getPosition, uuid, money, escapeHtml, formatDateTime, showMessage, statusBadge } from "./utils.js";

const DEFAULT_MAP_CENTER = [12.9716, 77.5946]; // Bengaluru fallback

function initLocationPicker(container, latInput, lngInput, initialLat, initialLng) {
  const hasInitial = typeof initialLat === "number" && typeof initialLng === "number";
  const start = hasInitial ? [initialLat, initialLng] : DEFAULT_MAP_CENTER;
  const map = L.map(container, { scrollWheelZoom: false }).setView(start, hasInitial ? 15 : 11);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(map);
  const marker = L.marker(start, { draggable: true }).addTo(map);

  function applyLatLng(latlng, panTo) {
    latInput.value = latlng.lat.toFixed(6);
    lngInput.value = latlng.lng.toFixed(6);
    marker.setLatLng(latlng);
    if (panTo) map.setView(latlng, Math.max(map.getZoom(), 15));
  }
  if (hasInitial) applyLatLng({ lat: initialLat, lng: initialLng }, false);

  marker.on("dragend", () => applyLatLng(marker.getLatLng(), false));
  map.on("click", (e) => applyLatLng(e.latlng, false));
  [latInput, lngInput].forEach((inp) =>
    inp.addEventListener("change", () => {
      const lat = parseFloat(latInput.value);
      const lng = parseFloat(lngInput.value);
      if (!isNaN(lat) && !isNaN(lng)) applyLatLng({ lat, lng }, true);
    })
  );
  return { map, marker, applyLatLng };
}

const TABS = [
  { key: "dashboard", icon: "📊", label: "Dashboard" },
  { key: "approvals", icon: "✅", label: "Approvals" },
  { key: "workers", icon: "👥", label: "Workers" },
  { key: "sites", icon: "🏢", label: "Sites" },
  { key: "more", icon: "⋯", label: "More" },
];

const MORE_TILES = [
  { key: "payout", icon: "💰", label: "Weekly Payout" },
  { key: "invoices", icon: "🧾", label: "GST Invoices" },
  { key: "leaves", icon: "📅", label: "Leaves" },
  { key: "attendance", icon: "🗺️", label: "Attendance & Map" },
  { key: "reports", icon: "⬇️", label: "Reports & Export" },
  { key: "audit", icon: "🛡️", label: "Audit Log" },
  { key: "doortypes", icon: "🚪", label: "Door Types" },
  { key: "settings", icon: "⚙️", label: "Settings" },
  { key: "changepwd", icon: "🔑", label: "Change Password" },
];

async function downloadWithToken(path) {
  const { token } = await post("/reports/download-token", {});
  const sep = path.includes("?") ? "&" : "?";
  window.open(`/api${path}${sep}token=${encodeURIComponent(token)}`, "_blank");
}

export function renderAdmin(frame, user, logout) {
  frame.innerHTML = `
    <div class="content" id="content"></div>
    <nav class="bottom-nav" id="bottom-nav">
      ${TABS.map((t) => `<button data-tab="${t.key}"><span class="nav-icon">${t.icon}</span>${t.label}</button>`).join("")}
    </nav>
  `;
  const content = frame.querySelector("#content");
  const navEl = frame.querySelector("#bottom-nav");
  const state = { tab: "dashboard", view: "dashboard", params: {} };

  const nav = {
    goTab(tab) {
      state.tab = tab;
      state.view = tab;
      state.params = {};
      render();
    },
    pushView(view, params = {}) {
      state.view = view;
      state.params = params;
      render();
    },
    back(tab) {
      state.view = tab || state.tab;
      state.params = {};
      render();
    },
  };

  const renderers = {
    dashboard: renderDashboard,
    approvals: renderApprovals,
    workers: renderWorkers,
    sites: renderSites,
    more: renderMore,
    payout: renderPayout,
    invoices: renderInvoices,
    leaves: renderLeaves,
    attendance: renderAttendance,
    reports: renderReportsExport,
    audit: renderAudit,
    doortypes: renderDoorTypes,
    settings: renderSettings,
    changepwd: renderChangePassword,
  };

  function render() {
    Array.from(navEl.children).forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === state.tab));
    content.innerHTML = '<div class="loading">Loading…</div>';
    renderers[state.view](content, nav, logout, state.params, user).catch((err) => {
      content.innerHTML = "";
      showMessage(content, err.message, "error");
    });
  }

  navEl.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => nav.goTab(btn.dataset.tab));
  });

  render();
}

function barChart(rows, valueKey, labelKey) {
  const max = Math.max(1, ...rows.map((r) => r[valueKey]));
  return `
    <div class="bar-chart">
      ${rows
        .map(
          (r) => `
        <div class="bar-col">
          <div class="bar" style="height:${Math.max(2, (r[valueKey] / max) * 90)}px"></div>
          <div class="bar-label">${escapeHtml(String(r[labelKey]).slice(-2))}</div>
        </div>
      `
        )
        .join("")}
    </div>
  `;
}

function progressList(rows, labelKey, valueKey, amountKey) {
  const max = Math.max(1, ...rows.map((r) => r[valueKey]));
  return rows
    .map(
      (r) => `
    <div class="progress-row">
      <div class="progress-row-top"><span>${escapeHtml(r[labelKey])}</span><span>${r[valueKey]}</span></div>
      <div class="progress-track"><div class="progress-fill" style="width:${(r[valueKey] / max) * 100}%"></div></div>
      <div class="list-card-meta">${money(r[amountKey])}</div>
    </div>
  `
    )
    .join("");
}

async function renderDashboard(content, nav) {
  const [d, charts] = await Promise.all([get("/admin/dashboard"), get("/admin/charts").catch(() => null)]);
  content.innerHTML = `
    <div class="page-header-row">
      <div>
        <h1 class="page-title">Admin Dashboard</h1>
        <p class="page-subtitle">JONAH ENTERPRISES</p>
      </div>
      <div class="avatar-circle">JE</div>
    </div>

    <div class="hero-block">
      <div class="hero-label">Pending Approvals</div>
      <div class="hero-value">${d.pending_reports}</div>
      <div class="hero-sub">Reports awaiting your review</div>
    </div>

    <div class="section-label">Workers</div>
    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-tile-label">Total</div><div class="stat-tile-value">${d.workers.total}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">Active Today</div><div class="stat-tile-value">${d.workers.active_today}</div></div>
    </div>
    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-tile-label">Checked In Now</div><div class="stat-tile-value">${d.workers.currently_checked_in}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">Inactive Today</div><div class="stat-tile-value">${d.workers.inactive_today}</div></div>
    </div>

    <div class="section-label">Sites</div>
    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-tile-label">Active Sites</div><div class="stat-tile-value">${d.sites.total_active}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">Sites Today</div><div class="stat-tile-value">${d.sites.with_workers_today}</div></div>
    </div>

    <div class="section-label">Doors Installed</div>
    <div class="stat-grid cols-3">
      <div class="stat-tile"><div class="stat-tile-label">Today</div><div class="stat-tile-value">${d.doors.today}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">This Month</div><div class="stat-tile-value">${d.doors.month}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">This Year</div><div class="stat-tile-value">${d.doors.year}</div></div>
    </div>

    <div class="section-label">Financials</div>
    <div class="hero-block">
      <div class="hero-label">Total Approved</div>
      <div class="hero-value">${money(d.financial.approved_total)}</div>
    </div>
    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-tile-label">Paid</div><div class="stat-tile-value">${money(d.financial.paid_total)}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">Pending</div><div class="stat-tile-value">${money(d.financial.pending_total)}</div></div>
    </div>

    ${
      charts
        ? `
      <div class="section-label">Trends</div>
      <div class="card-block">
        <strong>Doors Installed (last 6 months)</strong>
        ${barChart(charts.monthly_doors, "value", "month")}
      </div>
      <div class="card-block">
        <strong>Payments Paid Out (₹)</strong>
        ${barChart(charts.monthly_payments, "value", "month")}
      </div>
      <div class="card-block">
        <strong>Top Workers by Doors</strong>
        <div style="margin-top:0.6rem">${progressList(charts.top_workers, "worker_name", "doors", "amount") || "No data yet"}</div>
      </div>
      <div class="card-block">
        <strong>Site-wise Installations</strong>
        <div style="margin-top:0.6rem">${progressList(charts.site_installations, "site_name", "doors", "amount") || "No data yet"}</div>
      </div>
    `
        : ""
    }
  `;
}

async function renderApprovals(content) {
  const filters = ["Pending", "Approved", "Rejected", "Correction"];
  content.innerHTML = `
    <h1 class="page-title">Approval Queue</h1>
    <div class="filter-pills" id="filter-pills">
      ${filters.map((f, i) => `<button class="filter-pill ${i === 0 ? "active" : ""}" data-status="${f}">${f}</button>`).join("")}
    </div>
    <input type="search" id="report-search" placeholder="Search by worker or site…" style="margin-bottom:0.75rem" />
    <div id="report-list"></div>
  `;
  const listEl = content.querySelector("#report-list");
  const pillsEl = content.querySelector("#filter-pills");
  const searchEl = content.querySelector("#report-search");
  let currentStatus = "Pending";

  async function load(status) {
    currentStatus = status;
    listEl.innerHTML = '<div class="loading">Loading…</div>';
    const all = await get(`/work-reports?approval_status=${encodeURIComponent(status)}`);
    const q = searchEl.value.trim().toLowerCase();
    const reports = q
      ? all.filter((r) => r.worker_name.toLowerCase().includes(q) || r.site_name.toLowerCase().includes(q))
      : all;
    if (!reports.length) {
      listEl.innerHTML = `<div class="empty-state"><div class="title">Nothing here</div>No ${status.toLowerCase()} reports${q ? " match your search" : ""}.</div>`;
      return;
    }
    listEl.innerHTML = reports
      .map(
        (r, i) => `
      <div class="list-card" style="flex-direction:column;align-items:stretch">
        <div style="display:flex;justify-content:space-between">
          <div class="list-card-body">
            <div class="list-card-title">${escapeHtml(r.worker_name)}</div>
            <div class="list-card-sub">${escapeHtml(r.site_name)} · ${escapeHtml(r.work_date)}</div>
          </div>
          ${statusBadge(r.approval_status)}
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:0.5rem">
          <span>${r.door_count} doors</span>
          <span class="list-card-amount">${money(r.total_amount)}</span>
        </div>
        ${
          r.door_breakdown && r.door_breakdown.length
            ? `<div class="list-card-meta">${r.door_breakdown.map((d) => `${escapeHtml(d.door_type_name)} × ${d.count}`).join(", ")}</div>`
            : ""
        }
        <button class="btn-link" data-idx="${i}" data-action="photos" style="align-self:flex-start">Photos</button>
        <div class="photo-strip" id="photos-${i}" style="display:none">
          ${r.before_photos.map((p) => `<img src="${p}" alt="before" />`).join("")}
          ${r.after_photos.map((p) => `<img src="${p}" alt="after" />`).join("")}
        </div>
        ${
          status === "Pending" || status === "Correction"
            ? `
          <div class="actions" style="display:flex;gap:0.5rem;margin-top:0.6rem">
            <button class="pill-btn pill-btn-sm" data-idx="${i}" data-action="approve">Approve</button>
            <button class="pill-btn outline pill-btn-sm" data-idx="${i}" data-action="reject">Reject</button>
            <button class="pill-btn outline pill-btn-sm" data-idx="${i}" data-action="correction">Fix</button>
          </div>
        `
            : ""
        }
        ${
          status === "Approved"
            ? `
          <div class="actions" style="display:flex;gap:0.5rem;margin-top:0.6rem">
            <button class="pill-btn outline pill-btn-sm" data-idx="${i}" data-action="unapprove">Reopen</button>
          </div>
        `
            : ""
        }
      </div>
    `
      )
      .join("");

    listEl.querySelectorAll('[data-action="photos"]').forEach((btn) => {
      btn.addEventListener("click", () => {
        const strip = listEl.querySelector(`#photos-${btn.dataset.idx}`);
        strip.style.display = strip.style.display === "none" ? "flex" : "none";
      });
    });
    const map = { approve: "approve", reject: "reject", correction: "correction", unapprove: "unapprove" };
    const promptLabel = { approve: "approving", reject: "rejecting", correction: "requesting a fix", unapprove: "reopening" };
    Object.keys(map).forEach((action) => {
      listEl.querySelectorAll(`[data-action="${action}"]`).forEach((btn) => {
        btn.addEventListener("click", async () => {
          const report = reports[Number(btn.dataset.idx)];
          const remarks = prompt(`Remarks for ${promptLabel[action]} (optional):`, "") || "";
          try {
            await post(`/work-reports/${report.id}/${map[action]}`, { remarks });
            load(status);
          } catch (err) {
            showMessage(content, err.message, "error");
          }
        });
      });
    });
  }

  pillsEl.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      pillsEl.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      load(btn.dataset.status);
    });
  });
  searchEl.addEventListener("input", () => load(currentStatus));
  load("Pending");
}

async function renderWorkers(content) {
  const workers = await get("/workers");
  content.innerHTML = `
    <div class="page-header-row">
      <h1 class="page-title">Workers</h1>
      <button class="fab" id="add-btn">+</button>
    </div>
    <input type="search" id="worker-search" placeholder="Search by name, employee ID, or mobile…" style="margin-bottom:0.75rem" />
    <div id="add-form-wrap" style="display:none" class="card-block">
      <form id="add-form">
        <label>Employee ID<input name="employee_id" required /></label>
        <label>Name<input name="name" required /></label>
        <label>Mobile<input name="mobile" required /></label>
        <label>Username<input name="username" required /></label>
        <label>Password<input name="password" type="password" required minlength="8" /></label>
        <label>Rate/door<input name="default_rate" type="number" step="0.01" value="250" /></label>
        <div class="error" id="add-error"></div>
        <button type="submit" class="pill-btn">Create Worker</button>
      </form>
    </div>
    <div id="worker-list"></div>
  `;

  const listEl = content.querySelector("#worker-list");
  function renderList(items) {
    listEl.innerHTML =
      items
        .map(
          (w, i) => `
        <button class="list-card" data-idx="${i}" style="width:100%;text-align:left;border:1px solid var(--border);cursor:pointer">
          <div class="avatar-circle">${escapeHtml(w.name[0].toUpperCase())}</div>
          <div class="list-card-body" style="flex:1">
            <div class="list-card-title">${escapeHtml(w.name)}</div>
            <div class="list-card-sub">${escapeHtml(w.employee_id)} · ${escapeHtml(w.mobile)}</div>
          </div>
          <div class="list-card-right">
            <div class="list-card-amount">${money(w.default_rate)}</div>
            <div class="list-card-meta">/door</div>
          </div>
        </button>
        <div class="card-block" id="detail-${i}" style="display:none">
          <div style="margin-bottom:0.6rem">${statusBadge(w.status)}</div>
          <form class="edit-form" data-id="${w.id}">
            <label>Name<input name="name" value="${escapeHtml(w.name)}" /></label>
            <label>Mobile<input name="mobile" value="${escapeHtml(w.mobile)}" /></label>
            <label>Rate/door<input name="default_rate" type="number" step="0.01" value="${w.default_rate}" /></label>
            <button type="submit" class="pill-btn pill-btn-sm">Save</button>
          </form>
          <div class="actions" style="display:flex;gap:0.5rem;margin-top:0.75rem">
            <button class="pill-btn outline pill-btn-sm" data-id="${w.id}" data-action="reset-pwd">Reset Password</button>
            <button class="pill-btn outline pill-btn-sm" data-id="${w.id}" data-status="${w.status}" data-action="toggle-status">${w.status === "Active" ? "Disable" : "Activate"}</button>
          </div>
        </div>
      `
        )
        .join("") ||
      `<div class="empty-state"><div class="title">${workers.length ? "No matches" : "No workers yet"}</div></div>`;
    wireList(items);
  }

  function wireList(items) {
    listEl.querySelectorAll("[data-idx]").forEach((btn) => {
      if (!btn.dataset.action) {
        btn.addEventListener("click", () => {
          const d = listEl.querySelector(`#detail-${btn.dataset.idx}`);
          d.style.display = d.style.display === "none" ? "block" : "none";
        });
      }
    });
    listEl.querySelectorAll(".edit-form").forEach((form) => {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        try {
          await put(`/workers/${form.dataset.id}`, {
            name: fd.get("name"),
            mobile: fd.get("mobile"),
            default_rate: Number(fd.get("default_rate")),
          });
          renderWorkers(content);
        } catch (err) {
          showMessage(content, err.message, "error");
        }
      });
    });
    listEl.querySelectorAll('[data-action="reset-pwd"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        const pwd = prompt("New password (min 8 characters, leave blank for a generated default):");
        if (pwd === null) return;
        if (pwd && pwd.length < 8) {
          alert("Password must be at least 8 characters");
          return;
        }
        try {
          await post(`/workers/${id}/reset-password`, pwd ? { new_password: pwd } : {});
          showMessage(content, "Password reset", "success");
        } catch (err) {
          showMessage(content, err.message, "error");
        }
      });
    });
    listEl.querySelectorAll('[data-action="toggle-status"]').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const nextStatus = btn.dataset.status === "Active" ? "Disabled" : "Active";
        try {
          await put(`/workers/${btn.dataset.id}`, { status: nextStatus });
          renderWorkers(content);
        } catch (err) {
          showMessage(content, err.message, "error");
        }
      });
    });
  }

  renderList(workers);
  content.querySelector("#worker-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    renderList(
      q
        ? workers.filter(
            (w) =>
              w.name.toLowerCase().includes(q) ||
              w.employee_id.toLowerCase().includes(q) ||
              (w.mobile || "").toLowerCase().includes(q)
          )
        : workers
    );
  });

  content.querySelector("#add-btn").addEventListener("click", () => {
    const wrap = content.querySelector("#add-form-wrap");
    wrap.style.display = wrap.style.display === "none" ? "block" : "none";
  });
  content.querySelector("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = content.querySelector("#add-error");
    errorEl.textContent = "";
    const fd = new FormData(e.target);
    try {
      await post("/workers", {
        employee_id: fd.get("employee_id"),
        name: fd.get("name"),
        mobile: fd.get("mobile"),
        username: fd.get("username"),
        password: fd.get("password"),
        default_rate: Number(fd.get("default_rate")),
      });
      renderWorkers(content);
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });
}

function locationPickerHtml(idPrefix, lat, lng) {
  return `
    <label>Location</label>
    <div class="map-container" id="${idPrefix}-map"></div>
    <button type="button" class="pill-btn outline pill-btn-sm" id="${idPrefix}-locate-btn">📍 Use My Location</button>
    <div style="display:flex;gap:0.75rem;margin-top:0.75rem">
      <label style="flex:1">Latitude<input name="latitude" id="${idPrefix}-lat" type="number" step="0.000001" value="${lat ?? ""}" /></label>
      <label style="flex:1">Longitude<input name="longitude" id="${idPrefix}-lng" type="number" step="0.000001" value="${lng ?? ""}" /></label>
    </div>
  `;
}

function wireLocationPicker(content, idPrefix, initialLat, initialLng) {
  let picker = null;
  const latInput = content.querySelector(`#${idPrefix}-lat`);
  const lngInput = content.querySelector(`#${idPrefix}-lng`);
  function ensureInit() {
    if (!picker) {
      picker = initLocationPicker(content.querySelector(`#${idPrefix}-map`), latInput, lngInput, initialLat, initialLng);
    } else {
      picker.map.invalidateSize();
    }
  }
  content.querySelector(`#${idPrefix}-locate-btn`).addEventListener("click", async (e) => {
    const btn = e.target;
    btn.disabled = true;
    btn.textContent = "Locating…";
    try {
      const pos = await getPosition();
      ensureInit();
      picker.applyLatLng({ lat: pos.coords.latitude, lng: pos.coords.longitude }, true);
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "📍 Use My Location";
    }
  });
  return ensureInit;
}

function doorRateOverridesHtml(idPrefix, doorTypes, existingRates) {
  if (!doorTypes.length) return "";
  return `
    <label>Door Type Rate Overrides (blank = use default)</label>
    <div class="card-block" style="padding:0.75rem">
      ${doorTypes
        .map(
          (t) => `
        <div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.5rem">
          <span style="flex:1;font-size:0.85rem">${escapeHtml(t.name)} <span class="list-card-meta">(default ${money(t.default_rate)})</span></span>
          <input type="number" step="0.01" min="0" style="width:110px" name="${idPrefix}-rate-${t.id}"
            value="${existingRates[t.id] ?? ""}" placeholder="${t.default_rate}" />
        </div>
      `
        )
        .join("")}
    </div>
  `;
}

function collectDoorRateOverrides(form, idPrefix, doorTypes) {
  const rates = {};
  doorTypes.forEach((t) => {
    const input = form.querySelector(`[name="${idPrefix}-rate-${t.id}"]`);
    if (input && input.value !== "") rates[t.id] = Number(input.value);
  });
  return rates;
}

function targetDoorsHtml(idPrefix, doorTypes, existingTargets) {
  if (!doorTypes.length) return "";
  return `
    <label>Assigned Quantity (blank = no limit for that type)</label>
    <div class="card-block" style="padding:0.75rem">
      ${doorTypes
        .map(
          (t) => `
        <div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.5rem">
          <span style="flex:1;font-size:0.85rem">${escapeHtml(t.name)}</span>
          <input type="number" step="1" min="0" style="width:90px" name="${idPrefix}-target-${t.id}"
            value="${existingTargets[t.id] ?? ""}" placeholder="No limit" />
        </div>
      `
        )
        .join("")}
    </div>
  `;
}

function collectTargetDoors(form, idPrefix, doorTypes) {
  const targets = {};
  doorTypes.forEach((t) => {
    const input = form.querySelector(`[name="${idPrefix}-target-${t.id}"]`);
    if (input && input.value !== "") targets[t.id] = Number(input.value);
  });
  return targets;
}

function assignedWorkersHtml(idPrefix, workers, existingIds) {
  if (!workers.length) return "";
  return `
    <label>Team (blank = open to every worker)</label>
    <div class="card-block" style="padding:0.75rem">
      ${workers
        .map(
          (w) => `
        <label class="checklist-row">
          <input type="checkbox" name="${idPrefix}-worker-${w.id}" ${existingIds.includes(w.id) ? "checked" : ""} />
          <span>${escapeHtml(w.name)} <span class="list-card-meta">(${escapeHtml(w.employee_id)})</span></span>
        </label>
      `
        )
        .join("")}
    </div>
  `;
}

function collectAssignedWorkers(form, idPrefix, workers) {
  return workers.filter((w) => form.querySelector(`[name="${idPrefix}-worker-${w.id}"]`)?.checked).map((w) => w.id);
}

async function renderSites(content) {
  const [sites, doorTypes, workers] = await Promise.all([get("/sites"), get("/door-types"), get("/workers")]);
  const workerName = (id) => workers.find((w) => w.id === id)?.name || "Unknown";
  content.innerHTML = `
    <div class="page-header-row">
      <h1 class="page-title">Sites</h1>
      <button class="fab" id="add-btn">+</button>
    </div>
    <input type="search" id="site-search" placeholder="Search by name, client, or address…" style="margin-bottom:0.75rem" />
    <div id="add-form-wrap" style="display:none" class="card-block">
      <form id="add-form">
        <label>Site Name<input name="site_name" required /></label>
        <label>Client Name<input name="client_name" required /></label>
        <label>Address<input name="address" required /></label>
        ${locationPickerHtml("add-site", null, null)}
        <label>Contact Person<input name="contact_person" /></label>
        <label>Contact Number<input name="contact_number" /></label>
        ${doorRateOverridesHtml("add-site", doorTypes, {})}
        ${targetDoorsHtml("add-site", doorTypes, {})}
        ${assignedWorkersHtml("add-site", workers, [])}
        <div class="error" id="add-error"></div>
        <button type="submit" class="pill-btn">Create Site</button>
      </form>
    </div>
    <div class="empty-state" id="site-no-match" style="display:none"><div class="title">No matches</div></div>
    <div id="site-list">
      ${sites
        .map(
          (s, i) => `
        <button class="list-card" data-idx="${i}" style="width:100%;text-align:left;border:1px solid var(--border);cursor:pointer">
          <div class="list-card-body">
            <div class="list-card-title">${escapeHtml(s.site_name)}</div>
            <div class="list-card-sub">${escapeHtml(s.client_name)}</div>
            <div class="list-card-meta">📍 ${escapeHtml(s.address)}</div>
            <div class="list-card-meta">${
              s.assigned_worker_ids && s.assigned_worker_ids.length
                ? `👷 ${s.assigned_worker_ids.map(workerName).map(escapeHtml).join(", ")}`
                : "👷 Open to every worker"
            }</div>
          </div>
          <div class="list-card-right">${statusBadge(s.status)}</div>
        </button>
        <div class="card-block" id="detail-${i}" style="display:none">
          <form class="edit-form" data-id="${s.id}" data-idx="${i}">
            <label>Site Name<input name="site_name" value="${escapeHtml(s.site_name)}" required /></label>
            <label>Client Name<input name="client_name" value="${escapeHtml(s.client_name)}" required /></label>
            <label>Address<input name="address" value="${escapeHtml(s.address)}" required /></label>
            ${locationPickerHtml(`edit-site-${i}`, s.latitude, s.longitude)}
            <label>Contact Person<input name="contact_person" value="${escapeHtml(s.contact_person || "")}" /></label>
            <label>Contact Number<input name="contact_number" value="${escapeHtml(s.contact_number || "")}" /></label>
            ${doorRateOverridesHtml(`edit-site-${i}`, doorTypes, s.door_type_rates || {})}
            ${targetDoorsHtml(`edit-site-${i}`, doorTypes, s.target_doors || {})}
            ${assignedWorkersHtml(`edit-site-${i}`, workers, s.assigned_worker_ids || [])}
            <label>Status
              <select name="status">
                <option ${s.status === "Active" ? "selected" : ""}>Active</option>
                <option ${s.status === "Disabled" ? "selected" : ""}>Disabled</option>
              </select>
            </label>
            <button type="submit" class="pill-btn pill-btn-sm">Save</button>
          </form>
        </div>
      `
        )
        .join("") || '<div class="empty-state"><div class="title">No sites yet</div></div>'}
    </div>
  `;

  const ensureAddMap = wireLocationPicker(content, "add-site", null, null);
  content.querySelector("#add-btn").addEventListener("click", () => {
    const wrap = content.querySelector("#add-form-wrap");
    const opening = wrap.style.display === "none";
    wrap.style.display = opening ? "block" : "none";
    if (opening) ensureAddMap();
  });
  content.querySelector("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = content.querySelector("#add-error");
    errorEl.textContent = "";
    const fd = new FormData(e.target);
    try {
      await post("/sites", {
        site_name: fd.get("site_name"),
        client_name: fd.get("client_name"),
        address: fd.get("address"),
        latitude: fd.get("latitude") ? Number(fd.get("latitude")) : null,
        longitude: fd.get("longitude") ? Number(fd.get("longitude")) : null,
        contact_person: fd.get("contact_person"),
        contact_number: fd.get("contact_number"),
        door_type_rates: collectDoorRateOverrides(e.target, "add-site", doorTypes),
        target_doors: collectTargetDoors(e.target, "add-site", doorTypes),
        assigned_worker_ids: collectAssignedWorkers(e.target, "add-site", workers),
      });
      renderSites(content);
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });

  const ensureEditMaps = sites.map((s, i) =>
    wireLocationPicker(content, `edit-site-${i}`, s.latitude ?? null, s.longitude ?? null)
  );
  content.querySelectorAll("[data-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.idx);
      const d = content.querySelector(`#detail-${idx}`);
      const opening = d.style.display === "none";
      d.style.display = opening ? "block" : "none";
      if (opening) ensureEditMaps[idx]();
    });
  });
  content.querySelectorAll(".edit-form").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await put(`/sites/${form.dataset.id}`, {
          site_name: fd.get("site_name"),
          client_name: fd.get("client_name"),
          address: fd.get("address"),
          latitude: fd.get("latitude") ? Number(fd.get("latitude")) : null,
          longitude: fd.get("longitude") ? Number(fd.get("longitude")) : null,
          contact_person: fd.get("contact_person"),
          contact_number: fd.get("contact_number"),
          status: fd.get("status"),
          door_type_rates: collectDoorRateOverrides(form, `edit-site-${form.dataset.idx}`, doorTypes),
          target_doors: collectTargetDoors(form, `edit-site-${form.dataset.idx}`, doorTypes),
          assigned_worker_ids: collectAssignedWorkers(form, `edit-site-${form.dataset.idx}`, workers),
        });
        renderSites(content);
      } catch (err) {
        showMessage(content, err.message, "error");
      }
    });
  });

  // Show/hide rather than re-render — a full re-render would remap each site's
  // index and desync the location-picker map instances wired to edit-site-${i}.
  content.querySelector("#site-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    let anyVisible = false;
    sites.forEach((s, i) => {
      const match =
        !q ||
        s.site_name.toLowerCase().includes(q) ||
        s.client_name.toLowerCase().includes(q) ||
        (s.address || "").toLowerCase().includes(q);
      if (match) anyVisible = true;
      const card = content.querySelector(`[data-idx="${i}"].list-card`);
      if (card) card.style.display = match ? "" : "none";
      if (!match) {
        const detail = content.querySelector(`#detail-${i}`);
        if (detail) detail.style.display = "none";
      }
    });
    content.querySelector("#site-no-match").style.display = anyVisible || !sites.length ? "none" : "block";
  });
}

async function renderMore(content, nav, logout, params, user) {
  const [attendance, payments] = await Promise.all([
    get("/attendance").then((a) => a.slice(0, 5)),
    get("/payments").then((p) => p.slice(0, 5)),
  ]);
  content.innerHTML = `
    <h1 class="page-title">More</h1>
    <p class="page-subtitle">${escapeHtml(user.username)}</p>
    <div class="tile-grid">
      ${MORE_TILES.map((t) => `<button class="tile" data-view="${t.key}"><span class="tile-icon">${t.icon}</span><span class="tile-label">${t.label}</span></button>`).join("")}
    </div>

    <div class="section-label">Recent Attendance</div>
    ${
      attendance
        .map(
          (a) => `
      <div class="list-card">
        <div class="list-card-body">
          <div class="list-card-title">${escapeHtml(a.worker_name)}</div>
          <div class="list-card-sub">${escapeHtml(a.site_name)}</div>
          <div class="list-card-meta">In: ${formatDateTime(a.check_in_time)}</div>
        </div>
        <div class="list-card-right">${a.check_out_time ? `<div class="list-card-meta">Out: ${formatDateTime(a.check_out_time).split(",")[1]}</div>` : statusBadge("Pending")}</div>
      </div>
    `
        )
        .join("") || '<div class="empty-state">No attendance yet</div>'
    }

    <div class="section-label">Recent Payments</div>
    ${
      payments
        .map(
          (p) => `
      <div class="list-card">
        <div class="list-card-body">
          <div class="list-card-title">${escapeHtml(p.worker_name)}</div>
          <div class="list-card-sub">${escapeHtml(p.payment_method)} · ${escapeHtml(p.payment_date)}</div>
        </div>
        <div class="list-card-amount">${money(p.amount)}</div>
      </div>
    `
        )
        .join("") || '<div class="empty-state">No payments yet</div>'
    }

    <button class="pill-btn outline" id="logout-btn" style="margin-top:1rem">Logout</button>
  `;
  content.querySelectorAll("[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => nav.pushView(btn.dataset.view));
  });
  content.querySelector("#logout-btn").addEventListener("click", logout);
}

async function renderPayout(content, nav) {
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title">Weekly Payout</h1>
    </div>
    <div id="payout-body"><div class="loading">Loading…</div></div>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.back("more"));
  const body = content.querySelector("#payout-body");
  const data = await get("/admin/payout-preview");
  if (!data.rows.length) {
    body.innerHTML = '<div class="empty-state"><div class="title">All settled</div>No pending payouts.</div>';
    return;
  }
  // Generated once per screen load and reused across retries of the same click —
  // if a request fails and the admin tries again, it's still treated as one batch.
  const batchSyncId = uuid();
  body.innerHTML = `
    <p class="page-subtitle">${money(data.total_pending)} pending across ${data.worker_count} worker(s)</p>
    ${data.rows
      .map(
        (r, i) => `
      <div class="list-card">
        <label class="checklist-row" style="flex:1">
          <input type="checkbox" class="payout-check" data-idx="${i}" checked />
          <span class="list-card-body">
            <span class="list-card-title">${escapeHtml(r.worker_name)}</span>
            <span class="list-card-sub">${escapeHtml(r.preferred_method)} · last paid ${escapeHtml(r.last_paid_date || "-")}</span>
          </span>
        </label>
        <div class="list-card-amount">${money(r.pending_amount)}</div>
      </div>
    `
      )
      .join("")}
    <button class="pill-btn" id="pay-btn" style="margin-top:0.75rem">Pay Selected</button>
  `;
  body.querySelector("#pay-btn").addEventListener("click", async (e) => {
    const btn = e.target;
    const selected = Array.from(body.querySelectorAll(".payout-check:checked")).map((c) => data.rows[Number(c.dataset.idx)]);
    if (!selected.length) return;
    if (!confirm(`Record ${selected.length} payment(s) totaling ${money(selected.reduce((s, r) => s + r.pending_amount, 0))}?`)) return;
    btn.disabled = true;
    btn.textContent = "Recording…";
    try {
      await post("/payments/batch", {
        payments: selected.map((r) => ({ worker_id: r.worker_id, amount: r.pending_amount, payment_method: r.preferred_method, notes: "Batch payout" })),
        client_sync_id: batchSyncId,
      });
      renderPayout(content, nav);
    } catch (err) {
      showMessage(content, err.message, "error");
      btn.disabled = false;
      btn.textContent = "Pay Selected";
    }
  });
}

function addInvoiceItemRow(itemsWrap, item) {
  const row = document.createElement("div");
  row.className = "card-block item-row";
  row.style.position = "relative";
  row.innerHTML = `
    <label>Description<input name="description" value="${escapeHtml(item?.description || "")}" required /></label>
    <label>Quantity<input name="quantity" type="number" step="1" value="${item?.quantity ?? 1}" required /></label>
    <label>Unit Price<input name="unit_price" type="number" step="0.01" value="${item?.unit_price ?? ""}" required /></label>
    <label>HSN Code<input name="hsn_code" value="${escapeHtml(item?.hsn_code || "")}" /></label>
    <button type="button" class="btn-link text-danger remove-item-btn">Remove</button>
  `;
  row.querySelector(".remove-item-btn").addEventListener("click", () => {
    if (itemsWrap.querySelectorAll(".item-row").length > 1) row.remove();
    else alert("An invoice needs at least one line item");
  });
  itemsWrap.appendChild(row);
}

function collectInvoiceItems(itemsWrap) {
  return Array.from(itemsWrap.querySelectorAll(".item-row")).map((row) => ({
    description: row.querySelector('[name="description"]').value,
    quantity: Number(row.querySelector('[name="quantity"]').value),
    unit_price: Number(row.querySelector('[name="unit_price"]').value),
    hsn_code: row.querySelector('[name="hsn_code"]').value,
  }));
}

function invoiceFormHtml(idPrefix, inv) {
  return `
    <label>Client Name<input name="client_name" value="${escapeHtml(inv?.client_name || "")}" required /></label>
    <label>Client GSTIN<input name="client_gstin" value="${escapeHtml(inv?.client_gstin || "")}" /></label>
    <label>Client Address<input name="client_address" value="${escapeHtml(inv?.client_address || "")}" /></label>
    <label>GST Rate %<input name="gst_rate" type="number" step="0.1" value="${inv?.gst_rate ?? 18}" /></label>
    <div id="${idPrefix}-items"></div>
    <button type="button" class="btn-link" id="${idPrefix}-add-item">+ Add line item</button>
  `;
}

async function renderInvoices(content, nav) {
  const invoices = await get("/invoices");
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title" style="flex:1">GST Invoices</h1>
      <button class="fab" id="add-btn">+</button>
    </div>
    <div id="invoice-form-wrap" style="display:none" class="card-block">
      <form id="invoice-form">
        ${invoiceFormHtml("new", null)}
        <div class="error" id="invoice-error"></div>
        <button type="submit" class="pill-btn" style="margin-top:0.75rem">Create Invoice</button>
      </form>
    </div>
    <div id="invoice-list">
      ${
        invoices
          .map(
            (inv, i) => `
        <div class="list-card" style="flex-direction:column;align-items:stretch">
          <div style="display:flex;justify-content:space-between">
            <div class="list-card-body">
              <div class="list-card-title">${escapeHtml(inv.invoice_number)}</div>
              <div class="list-card-sub">${escapeHtml(inv.client_name)} · ${escapeHtml(inv.invoice_date)}</div>
            </div>
            <div class="list-card-amount">${money(inv.total)}</div>
          </div>
          <div class="actions" style="display:flex;gap:0.5rem;margin-top:0.5rem">
            <button class="pill-btn outline pill-btn-sm" data-idx="${i}" data-action="pdf">Download PDF</button>
            <button class="pill-btn outline pill-btn-sm" data-idx="${i}" data-action="edit">Edit</button>
            <button class="pill-btn outline pill-btn-sm text-danger" data-idx="${i}" data-action="delete">Delete</button>
          </div>
        </div>
        <div class="card-block" id="inv-detail-${i}" style="display:none">
          <form class="invoice-edit-form" data-id="${inv.id}" data-idx="${i}">
            ${invoiceFormHtml(`edit-inv-${i}`, inv)}
            <div class="error" id="edit-inv-${i}-error"></div>
            <button type="submit" class="pill-btn pill-btn-sm" style="margin-top:0.75rem">Save Changes</button>
          </form>
        </div>
      `
          )
          .join("") || '<div class="empty-state"><div class="title">No invoices yet</div></div>'
      }
    </div>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.back("more"));

  // --- Create form ---
  const newItemsWrap = content.querySelector("#new-items");
  addInvoiceItemRow(newItemsWrap, null);
  content.querySelector("#new-add-item").addEventListener("click", () => addInvoiceItemRow(newItemsWrap, null));

  const formWrap = content.querySelector("#invoice-form-wrap");
  content.querySelector("#add-btn").addEventListener("click", () => {
    formWrap.style.display = formWrap.style.display === "none" ? "block" : "none";
  });

  content.querySelector("#invoice-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = content.querySelector("#invoice-error");
    errorEl.textContent = "";
    const fd = new FormData(e.target);
    try {
      await post("/invoices", {
        client_name: fd.get("client_name"),
        client_gstin: fd.get("client_gstin"),
        client_address: fd.get("client_address"),
        gst_rate: Number(fd.get("gst_rate")),
        items: collectInvoiceItems(newItemsWrap),
      });
      renderInvoices(content, nav);
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });

  // --- Per-invoice edit forms (lazy-initialize item rows only when opened) ---
  const editItemsInitialized = {};
  function ensureEditItems(i) {
    if (editItemsInitialized[i]) return;
    const wrap = content.querySelector(`#edit-inv-${i}-items`);
    invoices[i].items.forEach((item) => addInvoiceItemRow(wrap, item));
    content.querySelector(`#edit-inv-${i}-add-item`).addEventListener("click", () => addInvoiceItemRow(wrap, null));
    editItemsInitialized[i] = true;
  }

  content.querySelectorAll('[data-action="pdf"]').forEach((btn) => {
    btn.addEventListener("click", () => downloadWithToken(`/invoices/${invoices[Number(btn.dataset.idx)].id}/pdf`));
  });
  content.querySelectorAll('[data-action="edit"]').forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.idx);
      ensureEditItems(idx);
      const d = content.querySelector(`#inv-detail-${idx}`);
      d.style.display = d.style.display === "none" ? "block" : "none";
    });
  });
  content.querySelectorAll('[data-action="delete"]').forEach((btn) => {
    btn.addEventListener("click", async () => {
      const inv = invoices[Number(btn.dataset.idx)];
      if (!confirm(`Delete invoice ${inv.invoice_number}? This cannot be undone.`)) return;
      try {
        await del(`/invoices/${inv.id}`);
        renderInvoices(content, nav);
      } catch (err) {
        showMessage(content, err.message, "error");
      }
    });
  });
  content.querySelectorAll(".invoice-edit-form").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const idx = form.dataset.idx;
      const errorEl = content.querySelector(`#edit-inv-${idx}-error`);
      errorEl.textContent = "";
      const fd = new FormData(form);
      try {
        await put(`/invoices/${form.dataset.id}`, {
          client_name: fd.get("client_name"),
          client_gstin: fd.get("client_gstin"),
          client_address: fd.get("client_address"),
          gst_rate: Number(fd.get("gst_rate")),
          items: collectInvoiceItems(content.querySelector(`#edit-inv-${idx}-items`)),
        });
        renderInvoices(content, nav);
      } catch (err) {
        errorEl.textContent = err.message;
      }
    });
  });
}

async function renderLeaves(content, nav) {
  const filters = ["Pending", "Approved", "Rejected"];
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title">Leave Requests</h1>
    </div>
    <div class="filter-pills" id="filter-pills">
      ${filters.map((f, i) => `<button class="filter-pill ${i === 0 ? "active" : ""}" data-status="${f}">${f}</button>`).join("")}
    </div>
    <div id="leave-list"></div>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.back("more"));
  const listEl = content.querySelector("#leave-list");
  async function load(status) {
    listEl.innerHTML = '<div class="loading">Loading…</div>';
    const leaves = await get(`/leaves?status=${encodeURIComponent(status)}`);
    listEl.innerHTML =
      leaves
        .map(
          (l, i) => `
      <div class="list-card" style="flex-direction:column;align-items:stretch">
        <div style="display:flex;justify-content:space-between">
          <div class="list-card-body">
            <div class="list-card-title">${escapeHtml(l.worker_name)}</div>
            <div class="list-card-sub">${escapeHtml(l.start_date)} → ${escapeHtml(l.end_date)} · ${escapeHtml(l.leave_type)}</div>
            <div class="list-card-meta">${escapeHtml(l.reason)}</div>
          </div>
          ${statusBadge(l.status)}
        </div>
        ${
          status === "Pending"
            ? `
          <div class="actions" style="display:flex;gap:0.5rem;margin-top:0.6rem">
            <button class="pill-btn pill-btn-sm" data-idx="${i}" data-action="approve">Approve</button>
            <button class="pill-btn outline pill-btn-sm" data-idx="${i}" data-action="reject">Reject</button>
          </div>
        `
            : ""
        }
      </div>
    `
        )
        .join("") || `<div class="empty-state"><div class="title">Nothing here</div>No ${status.toLowerCase()} leave requests.</div>`;
    const map = { approve: "approve", reject: "reject" };
    Object.keys(map).forEach((action) => {
      listEl.querySelectorAll(`[data-action="${action}"]`).forEach((btn) => {
        btn.addEventListener("click", async () => {
          const remarks = prompt(`Remarks for ${action} (optional):`, "") || "";
          try {
            await post(`/leaves/${leaves[Number(btn.dataset.idx)].id}/${map[action]}`, { remarks });
            load(status);
          } catch (err) {
            showMessage(content, err.message, "error");
          }
        });
      });
    });
  }
  content.querySelector("#filter-pills").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      content.querySelectorAll(".filter-pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      load(btn.dataset.status);
    });
  });
  load("Pending");
}

let _attendanceMap = null;

async function renderAttendance(content, nav) {
  const sessions = await get("/attendance");
  const shown = sessions.slice(0, 100);
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title">Attendance & Map</h1>
    </div>
    <p class="page-subtitle">Check-in locations for the most recent sessions</p>
    <div class="map-container" id="attendance-map"></div>
    <div id="attendance-list">
      ${shown
        .map(
          (a, i) => `
        <div class="list-card" style="flex-direction:column;align-items:stretch">
          <div style="display:flex;justify-content:space-between">
            <div class="list-card-body">
              <div class="list-card-title">${escapeHtml(a.worker_name)}</div>
              <div class="list-card-sub">${escapeHtml(a.site_name)}</div>
              <div class="list-card-meta">In: ${formatDateTime(a.check_in_time)} @ ${a.check_in_latitude?.toFixed(4)}, ${a.check_in_longitude?.toFixed(4)}${a.check_in_geo_warning ? ` (${a.check_in_geo_distance_m}m away ⚠️)` : ""}</div>
              ${a.check_out_time ? `<div class="list-card-meta">Out: ${formatDateTime(a.check_out_time)}${a.check_out_geo_warning ? ` (${a.check_out_geo_distance_m}m away ⚠️)` : ""}</div>` : ""}
            </div>
            ${a.check_out_time ? "" : statusBadge("Pending")}
          </div>
          <div style="display:flex;gap:0.4rem;flex-wrap:wrap;margin-top:0.4rem">
            ${a.check_in_geo_warning || a.check_out_geo_warning ? `<div class="badge badge-warning">Far from site</div>` : ""}
            ${a.late_flag ? `<div class="badge badge-warning">Late ${a.late_by_minutes}m</div>` : ""}
            ${a.early_leave_flag ? `<div class="badge badge-warning">Left early ${a.early_leave_by_minutes}m</div>` : ""}
          </div>
          ${
            a.check_out_time
              ? ""
              : `<button class="pill-btn outline pill-btn-sm text-danger" data-idx="${i}" style="align-self:flex-start;margin-top:0.5rem">Force Check-Out</button>`
          }
        </div>
      `
        )
        .join("") || '<div class="empty-state">No attendance records</div>'}
    </div>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.back("more"));
  initAttendanceMap(shown);

  content.querySelectorAll("[data-idx]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sess = shown[Number(btn.dataset.idx)];
      const remarks = prompt(
        `Close ${sess.worker_name}'s session at ${sess.site_name}? They'll be notified. Optional remarks:`,
        ""
      );
      if (remarks === null) return;
      btn.disabled = true;
      btn.textContent = "Closing…";
      try {
        await post(`/attendance/${sess.id}/force-checkout`, { remarks });
        renderAttendance(content, nav);
      } catch (err) {
        showMessage(content, err.message, "error");
        btn.disabled = false;
        btn.textContent = "Force Check-Out";
      }
    });
  });
}

function initAttendanceMap(sessions) {
  if (_attendanceMap) {
    _attendanceMap.remove();
    _attendanceMap = null;
  }
  const points = sessions.filter((a) => typeof a.check_in_latitude === "number" && typeof a.check_in_longitude === "number");
  const map = L.map("attendance-map", { scrollWheelZoom: false });
  _attendanceMap = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(map);

  if (!points.length) {
    map.setView([12.9716, 77.5946], 11); // Bengaluru fallback
    return;
  }

  const markers = points.map((a) => {
    const marker = L.marker([a.check_in_latitude, a.check_in_longitude]);
    const status = a.check_out_time ? `Checked out ${formatDateTime(a.check_out_time)}` : "Still checked in";
    marker.bindPopup(
      `<strong>${escapeHtml(a.worker_name)}</strong><br/>${escapeHtml(a.site_name)}<br/>In: ${formatDateTime(a.check_in_time)}<br/>${status}`
    );
    return marker;
  });
  const group = L.featureGroup(markers).addTo(map);
  map.fitBounds(group.getBounds().pad(0.2), { maxZoom: 15 });
}

const REPORT_TYPES = [
  { key: "worker_earnings", label: "Worker Earnings" },
  { key: "payments", label: "Payments" },
  { key: "attendance", label: "Attendance" },
  { key: "site_installations", label: "Site Installations" },
  { key: "worker_performance", label: "Worker Performance" },
  { key: "attendance_summary", label: "Attendance Summary" },
];

async function renderReportsExport(content, nav) {
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title">Reports & Export</h1>
    </div>
    <div class="card-block">
      <label>From<input type="date" id="date-from" /></label>
      <label>To<input type="date" id="date-to" /></label>
    </div>
    ${REPORT_TYPES.map(
      (r) => `
      <div class="list-card">
        <div class="list-card-body"><div class="list-card-title">${r.label}</div></div>
        <div class="actions" style="display:flex;gap:0.5rem">
          <button class="pill-btn outline pill-btn-sm" data-report="${r.key}" data-fmt="xlsx">XLSX</button>
          <button class="pill-btn outline pill-btn-sm" data-report="${r.key}" data-fmt="pdf">PDF</button>
        </div>
      </div>
    `
    ).join("")}
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.back("more"));
  content.querySelectorAll("[data-report]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const dateFrom = content.querySelector("#date-from").value;
      const dateTo = content.querySelector("#date-to").value;
      const params = new URLSearchParams({ report: btn.dataset.report, fmt: btn.dataset.fmt });
      if (dateFrom) params.set("date_from", dateFrom);
      if (dateTo) params.set("date_to", dateTo);
      downloadWithToken(`/reports/export?${params.toString()}`);
    });
  });
}

async function renderAudit(content, nav) {
  const logs = await get("/audit-logs?limit=100");
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title">Audit Log</h1>
    </div>
    <input type="search" id="audit-search" placeholder="Search by action, entity, or user…" style="margin-bottom:0.75rem" />
    <div id="audit-list"></div>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.back("more"));

  const listEl = content.querySelector("#audit-list");
  const searchEl = content.querySelector("#audit-search");
  function renderList(items) {
    listEl.innerHTML =
      items
        .map(
          (l) => `
      <div class="list-card">
        <div class="list-card-body">
          <div class="list-card-title">${escapeHtml(l.action)} — ${escapeHtml(l.entity_type)}</div>
          <div class="list-card-sub">${escapeHtml(l.user_name)}</div>
          <div class="list-card-meta">${formatDateTime(l.created_at)}</div>
        </div>
      </div>
    `
        )
        .join("") ||
      `<div class="empty-state">${logs.length ? "No entries match your search" : "No audit entries yet"}</div>`;
  }
  searchEl.addEventListener("input", () => {
    const q = searchEl.value.trim().toLowerCase();
    const filtered = q
      ? logs.filter(
          (l) =>
            l.action.toLowerCase().includes(q) ||
            l.entity_type.toLowerCase().includes(q) ||
            l.user_name.toLowerCase().includes(q)
        )
      : logs;
    renderList(filtered);
  });
  renderList(logs);
}

async function renderSettings(content, nav) {
  const s = await get("/settings");
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title">Settings</h1>
    </div>
    <form class="card-block" id="settings-form">
      <label>Company Name<input name="company_name" value="${escapeHtml(s.company_name || "")}" /></label>
      <label>Address<input name="address" value="${escapeHtml(s.address || "")}" /></label>
      <label>Mobile<input name="mobile" value="${escapeHtml(s.mobile || "")}" /></label>
      <label>Email<input name="email" value="${escapeHtml(s.email || "")}" /></label>
      <label>Currency<input name="currency" value="${escapeHtml(s.currency || "₹")}" /></label>
      <label>Default Rate/Door<input name="default_rate" type="number" step="0.01" value="${s.default_rate || 250}" /></label>
      <label>Shift Start (blank = no late flagging)<input name="shift_start_time" type="time" value="${escapeHtml(s.shift_start_time || "")}" /></label>
      <label>Late Grace (minutes)<input name="late_grace_minutes" type="number" step="1" min="0" value="${s.late_grace_minutes ?? 15}" /></label>
      <label>Shift End (blank = no early-leave flagging)<input name="shift_end_time" type="time" value="${escapeHtml(s.shift_end_time || "")}" /></label>
      <label>Early Leave Grace (minutes)<input name="early_leave_grace_minutes" type="number" step="1" min="0" value="${s.early_leave_grace_minutes ?? 15}" /></label>
      <div class="error" id="settings-error"></div>
      <button type="submit" class="pill-btn">Save Settings</button>
    </form>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.back("more"));
  content.querySelector("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = content.querySelector("#settings-error");
    errorEl.textContent = "";
    const fd = new FormData(e.target);
    try {
      await put("/settings", {
        company_name: fd.get("company_name"),
        address: fd.get("address"),
        mobile: fd.get("mobile"),
        email: fd.get("email"),
        currency: fd.get("currency"),
        default_rate: Number(fd.get("default_rate")),
        shift_start_time: fd.get("shift_start_time"),
        shift_end_time: fd.get("shift_end_time"),
        late_grace_minutes: Number(fd.get("late_grace_minutes")),
        early_leave_grace_minutes: Number(fd.get("early_leave_grace_minutes")),
      });
      showMessage(content, "Settings saved", "success");
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });
}

async function renderChangePassword(content, nav, logout) {
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title">Change Password</h1>
    </div>
    <form class="card-block" id="pwd-form">
      <label for="pwd-current">Current password</label>
      <input type="password" id="pwd-current" autocomplete="current-password" required />
      <label for="pwd-new">New password</label>
      <input type="password" id="pwd-new" autocomplete="new-password" required minlength="8" />
      <label for="pwd-confirm">Confirm new password</label>
      <input type="password" id="pwd-confirm" autocomplete="new-password" required minlength="8" />
      <div class="error" id="pwd-error"></div>
      <button type="submit" class="pill-btn">Update Password</button>
    </form>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.back("more"));
  content.querySelector("#pwd-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = content.querySelector("#pwd-error");
    errorEl.textContent = "";
    const current = content.querySelector("#pwd-current").value;
    const next = content.querySelector("#pwd-new").value;
    const confirm = content.querySelector("#pwd-confirm").value;
    if (next !== confirm) {
      errorEl.textContent = "New passwords don't match";
      return;
    }
    const btn = e.target.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.textContent = "Updating…";
    try {
      await post("/auth/change-password", { current_password: current, new_password: next });
      alert("Password updated. Please log in again with your new password.");
      logout();
    } catch (err) {
      errorEl.textContent = err.message;
      btn.disabled = false;
      btn.textContent = "Update Password";
    }
  });
}

async function renderDoorTypes(content, nav) {
  const types = await get("/door-types");
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title" style="flex:1">Door Types</h1>
      <button class="fab" id="add-btn">+</button>
    </div>
    <p class="page-subtitle">Base rate per door type — sites can override these individually</p>
    <div id="add-form-wrap" style="display:none" class="card-block">
      <form id="add-form">
        <label>Name<input name="name" placeholder="e.g. Steel Door" required /></label>
        <label>Rate per door<input name="default_rate" type="number" step="0.01" min="0.01" required /></label>
        <div class="error" id="add-error"></div>
        <button type="submit" class="pill-btn">Create Door Type</button>
      </form>
    </div>
    <div id="type-list">
      ${types
        .map(
          (t, i) => `
        <button class="list-card" data-idx="${i}" style="width:100%;text-align:left;border:1px solid var(--border);cursor:pointer">
          <div class="list-card-body">
            <div class="list-card-title">${escapeHtml(t.name)}</div>
          </div>
          <div class="list-card-right">
            <div class="list-card-amount">${money(t.default_rate)}</div>
            ${statusBadge(t.status)}
          </div>
        </button>
        <div class="card-block" id="detail-${i}" style="display:none">
          <form class="edit-form" data-id="${t.id}">
            <label>Name<input name="name" value="${escapeHtml(t.name)}" required /></label>
            <label>Rate per door<input name="default_rate" type="number" step="0.01" min="0.01" value="${t.default_rate}" required /></label>
            <label>Status
              <select name="status">
                <option ${t.status === "Active" ? "selected" : ""}>Active</option>
                <option ${t.status === "Disabled" ? "selected" : ""}>Disabled</option>
              </select>
            </label>
            <button type="submit" class="pill-btn pill-btn-sm">Save</button>
          </form>
        </div>
      `
        )
        .join("") || '<div class="empty-state"><div class="title">No door types yet</div></div>'}
    </div>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.back("more"));
  content.querySelector("#add-btn").addEventListener("click", () => {
    const wrap = content.querySelector("#add-form-wrap");
    wrap.style.display = wrap.style.display === "none" ? "block" : "none";
  });
  content.querySelector("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = content.querySelector("#add-error");
    errorEl.textContent = "";
    const fd = new FormData(e.target);
    try {
      await post("/door-types", { name: fd.get("name"), default_rate: Number(fd.get("default_rate")) });
      renderDoorTypes(content, nav);
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });
  content.querySelectorAll("[data-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const d = content.querySelector(`#detail-${btn.dataset.idx}`);
      d.style.display = d.style.display === "none" ? "block" : "none";
    });
  });
  content.querySelectorAll(".edit-form").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await put(`/door-types/${form.dataset.id}`, {
          name: fd.get("name"),
          default_rate: Number(fd.get("default_rate")),
          status: fd.get("status"),
        });
        renderDoorTypes(content, nav);
      } catch (err) {
        showMessage(content, err.message, "error");
      }
    });
  });
}
