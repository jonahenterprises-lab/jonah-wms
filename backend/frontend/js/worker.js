import { get, post } from "./api.js";
import {
  getPosition,
  filesToDataUris,
  uuid,
  money,
  escapeHtml,
  formatDateTime,
  showMessage,
  statusBadge,
} from "./utils.js";

let _sitesMap = null;

function renderMap(containerId, points) {
  if (_sitesMap) {
    _sitesMap.remove();
    _sitesMap = null;
  }
  const map = L.map(containerId, { scrollWheelZoom: false });
  _sitesMap = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(map);

  if (!points.length) {
    map.setView([12.9716, 77.5946], 11); // fallback center
    return map;
  }
  const markers = points.map((p) => {
    const marker = L.marker([p.lat, p.lng], p.icon ? { icon: p.icon } : undefined);
    if (p.popup) marker.bindPopup(p.popup);
    return marker;
  });
  const group = L.featureGroup(markers).addTo(map);
  if (points.length === 1) {
    map.setView([points[0].lat, points[0].lng], 16);
  } else {
    map.fitBounds(group.getBounds().pad(0.2), { maxZoom: 15 });
  }
  return map;
}

const meIcon = L.divIcon({ className: "me-marker", html: "🧍", iconSize: [26, 26] });

const TABS = [
  { key: "home", icon: "🏠", label: "Home" },
  { key: "sites", icon: "📍", label: "Sites" },
  { key: "reports", icon: "📄", label: "Reports" },
  { key: "payments", icon: "💳", label: "Payments" },
  { key: "profile", icon: "👤", label: "Profile" },
];

export function renderWorker(frame, user, logout) {
  frame.innerHTML = `
    <div class="content" id="content"></div>
    <nav class="bottom-nav" id="bottom-nav">
      ${TABS.map((t) => `<button data-tab="${t.key}"><span class="nav-icon">${t.icon}</span>${t.label}</button>`).join("")}
    </nav>
  `;
  const content = frame.querySelector("#content");
  const navEl = frame.querySelector("#bottom-nav");

  const state = { tab: "home", view: "home", params: {} };

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
    back() {
      state.view = state.tab;
      state.params = {};
      render();
    },
  };

  const renderers = {
    home: renderHome,
    sites: renderSites,
    reports: renderReports,
    payments: renderPayments,
    profile: renderProfile,
    leave: renderLeave,
    reportform: renderReportForm,
  };

  function render() {
    Array.from(navEl.children).forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === state.tab));
    content.innerHTML = '<div class="loading">Loading…</div>';
    renderers[state.view](content, user, nav, logout, state.params).catch((err) => {
      content.innerHTML = "";
      showMessage(content, err.message, "error");
    });
  }

  navEl.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => nav.goTab(btn.dataset.tab));
  });

  render();
}

function todayLabel() {
  return new Date().toLocaleDateString("en-US", { weekday: "short", month: "short", day: "2-digit", year: "numeric" });
}

async function renderHome(content, user, nav) {
  const d = await get("/worker/dashboard");
  const active = d.active_session;
  content.innerHTML = `
    <div class="page-header-row">
      <div>
        <h1 class="page-title">Hello, ${escapeHtml(user.name || user.username)}</h1>
        <p class="page-subtitle">${todayLabel()}</p>
      </div>
      <div class="avatar-circle">${escapeHtml((user.name || user.username)[0].toUpperCase())}</div>
    </div>

    <div class="hero-block">
      <div class="hero-label">Check-in Status</div>
      <div class="hero-value"><span class="status-dot" style="background:${active ? "#6fcf73" : "#c0392b"}"></span>${active ? "Checked In" : "Checked Out"}</div>
      ${active ? `<div class="hero-sub">Active Site<br/><strong>${escapeHtml(active.site_name)}</strong></div>
        <button class="pill-btn" id="go-to-site">Go to Site</button>` : ""}
    </div>

    <div class="section-label">Today</div>
    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-tile-label">Sites Visited</div><div class="stat-tile-value">${d.today.sites_visited}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">Doors Installed</div><div class="stat-tile-value">${d.today.doors_installed}</div></div>
    </div>

    <div class="hero-block">
      <div class="hero-label">Today's Approved Earnings</div>
      <div class="hero-value">${money(d.today.approved_amount)}</div>
    </div>

    <div class="section-label">This Month</div>
    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-tile-label">Sites</div><div class="stat-tile-value">${d.month.sites_visited}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">Doors</div><div class="stat-tile-value">${d.month.doors_installed}</div></div>
    </div>
    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-tile-label">Approved</div><div class="stat-tile-value">${money(d.month.approved_earnings)}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">Paid</div><div class="stat-tile-value">${money(d.month.paid_amount)}</div></div>
    </div>

    <div class="hero-block">
      <div class="hero-label">Pending Payment</div>
      <div class="hero-value">${money(d.month.pending_amount)}</div>
      <div class="hero-sub">You will receive this soon</div>
    </div>
  `;
  const goBtn = content.querySelector("#go-to-site");
  if (goBtn) goBtn.addEventListener("click", () => nav.goTab("sites"));
}

async function renderSites(content, user, nav) {
  const active = await get("/attendance/active");
  if (active) {
    content.innerHTML = `
      <h1 class="page-title">Active Site</h1>
      <p class="page-subtitle">You are checked in</p>

      <div class="hero-block">
        <div class="hero-label">Currently At</div>
        <div class="hero-value">${escapeHtml(active.site_name)}</div>
        <div class="hero-sub">Since ${formatDateTime(active.check_in_time).split(",")[1] || formatDateTime(active.check_in_time)}</div>
        <div class="hero-sub">📍 GPS: ${active.check_in_latitude?.toFixed(5)}, ${active.check_in_longitude?.toFixed(5)}</div>
      </div>

      <div class="map-container" id="active-site-map"></div>

      <button class="pill-btn" id="add-report-btn">+ Add Work Report</button>

      <div class="card-block" style="margin-top:1rem">
        <label for="checkout-remarks">Check-Out Remarks (optional)</label>
        <textarea id="checkout-remarks" rows="3" placeholder="Any notes about today's work…"></textarea>
        <label class="checkbox-label"><input type="checkbox" id="work-completed" checked /> Work completed</label>
        <div class="error" id="sites-error"></div>
        <button class="pill-btn dark" id="checkout-btn">Check Out</button>
      </div>
    `;
    const siteMap = renderMap("active-site-map", [
      {
        lat: active.check_in_latitude,
        lng: active.check_in_longitude,
        popup: `<strong>${escapeHtml(active.site_name)}</strong><br/>Checked in here`,
      },
    ]);
    getPosition()
      .then((pos) => {
        if (_sitesMap !== siteMap) return; // user navigated away before this resolved
        const here = [pos.coords.latitude, pos.coords.longitude];
        L.marker(here, { icon: meIcon }).addTo(siteMap).bindPopup("You are here");
        siteMap.fitBounds(siteMap.getBounds().extend(here).pad(0.2), { maxZoom: 16 });
      })
      .catch(() => {}); // best-effort only, map still works without it

    content.querySelector("#add-report-btn").addEventListener("click", () => nav.pushView("reportform", { session: active }));
    content.querySelector("#checkout-btn").addEventListener("click", async (e) => {
      const btn = e.target;
      const errorEl = content.querySelector("#sites-error");
      errorEl.textContent = "";
      btn.disabled = true;
      btn.textContent = "Getting location…";
      try {
        const pos = await getPosition();
        btn.textContent = "Checking out…";
        await post("/attendance/check-out", {
          session_id: active.id,
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
          work_completed: content.querySelector("#work-completed").checked,
          remarks: content.querySelector("#checkout-remarks").value,
        });
        renderSites(content, user, nav);
      } catch (err) {
        errorEl.textContent = err.message;
        btn.disabled = false;
        btn.textContent = "Check Out";
      }
    });
  } else {
    const sites = await get("/sites");
    content.innerHTML = `
      <h1 class="page-title">Sites</h1>
      <p class="page-subtitle">Tap a site to check in</p>
      <div class="map-container" id="sites-map"></div>
      <div class="error" id="sites-error"></div>
      <div id="site-list">
        ${sites
          .map(
            (s) => `
          <button class="list-card" data-id="${s.id}" style="width:100%;text-align:left;border:1px solid var(--border);cursor:pointer">
            <div class="list-card-body">
              <div class="list-card-title">${escapeHtml(s.site_name)}</div>
              <div class="list-card-sub">${escapeHtml(s.client_name)}</div>
              <div class="list-card-meta">📍 ${escapeHtml(s.address)}</div>
            </div>
            <div class="list-card-right">${statusBadge(s.status)}</div>
          </button>
        `
          )
          .join("") || '<div class="empty-state"><div class="title">No sites</div>No active sites assigned yet.</div>'}
      </div>
    `;
    const sitesMap = renderMap(
      "sites-map",
      sites
        .filter((s) => typeof s.latitude === "number" && typeof s.longitude === "number")
        .map((s) => ({
          lat: s.latitude,
          lng: s.longitude,
          popup: `<strong>${escapeHtml(s.site_name)}</strong><br/>${escapeHtml(s.address)}`,
        }))
    );
    getPosition()
      .then((pos) => {
        if (_sitesMap !== sitesMap) return; // user navigated away before this resolved
        const here = [pos.coords.latitude, pos.coords.longitude];
        L.marker(here, { icon: meIcon }).addTo(sitesMap).bindPopup("You are here");
        sitesMap.fitBounds(sitesMap.getBounds().extend(here).pad(0.2), { maxZoom: 15 });
      })
      .catch(() => {}); // best-effort only, map still works without it

    content.querySelectorAll("[data-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const errorEl = content.querySelector("#sites-error");
        errorEl.textContent = "";
        btn.disabled = true;
        const original = btn.innerHTML;
        btn.innerHTML = "<div class='list-card-body'>Getting location…</div>";
        try {
          const pos = await getPosition();
          btn.innerHTML = "<div class='list-card-body'>Checking in…</div>";
          await post("/attendance/check-in", {
            site_id: btn.dataset.id,
            latitude: pos.coords.latitude,
            longitude: pos.coords.longitude,
            accuracy: pos.coords.accuracy,
            client_sync_id: uuid(),
          });
          renderSites(content, user, nav);
        } catch (err) {
          errorEl.textContent = err.message;
          btn.disabled = false;
          btn.innerHTML = original;
        }
      });
    });
  }
}

async function renderReportForm(content, user, nav, logout, params) {
  const session = params.session;
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <h1 class="page-title">Submit Report</h1>
    </div>
    <p class="page-subtitle">${escapeHtml(session.site_name)} — ${formatDateTime(session.check_in_time)}</p>

    <form id="report-form">
      <label for="report-doors">Doors installed</label>
      <input type="number" id="report-doors" min="0" required />

      <label class="checkbox-label"><input type="checkbox" id="report-completed" checked /> Work completed</label>

      <label for="report-notes">Notes</label>
      <textarea id="report-notes" rows="3" placeholder="Optional notes"></textarea>

      <label for="report-before">Before photos</label>
      <input type="file" id="report-before" accept="image/*" capture="environment" multiple required />
      <div class="file-hint" id="before-hint">No photos selected</div>

      <label for="report-after">After photos</label>
      <input type="file" id="report-after" accept="image/*" capture="environment" multiple required />
      <div class="file-hint" id="after-hint">No photos selected</div>

      <div class="error" id="report-error"></div>
      <button type="submit" class="pill-btn">Submit Report</button>
    </form>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.goTab("sites"));

  const beforeInput = content.querySelector("#report-before");
  const afterInput = content.querySelector("#report-after");
  beforeInput.addEventListener("change", () => {
    content.querySelector("#before-hint").textContent = `${beforeInput.files.length} photo(s) selected`;
  });
  afterInput.addEventListener("change", () => {
    content.querySelector("#after-hint").textContent = `${afterInput.files.length} photo(s) selected`;
  });

  content.querySelector("#report-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = content.querySelector("#report-error");
    errorEl.textContent = "";
    const btn = e.target.querySelector("button[type=submit]");
    if (!beforeInput.files.length || !afterInput.files.length) {
      errorEl.textContent = "At least 1 before and 1 after photo required";
      return;
    }
    btn.disabled = true;
    btn.textContent = "Uploading…";
    try {
      // Sequential (not Promise.all) so only one photo is ever being decoded at a
      // time — running both batches concurrently is what let peak memory spike.
      const beforePhotos = await filesToDataUris(beforeInput.files);
      const afterPhotos = await filesToDataUris(afterInput.files);
      await post("/work-reports", {
        session_id: session.id,
        door_count: Number(content.querySelector("#report-doors").value),
        work_completed: content.querySelector("#report-completed").checked,
        notes: content.querySelector("#report-notes").value,
        before_photos: beforePhotos,
        after_photos: afterPhotos,
        client_sync_id: uuid(),
      });
      nav.goTab("reports");
    } catch (err) {
      errorEl.textContent = err.message;
      btn.disabled = false;
      btn.textContent = "Submit Report";
    }
  });
}

async function renderReports(content, user) {
  const reports = await get("/work-reports");
  content.innerHTML = `
    <h1 class="page-title">Work Reports</h1>
    <p class="page-subtitle">${reports.length} total</p>
    <div id="report-list">
      ${
        reports.length === 0
          ? '<div class="empty-state"><div class="title">No reports</div>Submit one after checking out of a site.</div>'
          : reports
              .map(
                (r, i) => `
        <div class="list-card" style="flex-direction:column;align-items:stretch">
          <div style="display:flex;justify-content:space-between;align-items:flex-start">
            <div class="list-card-body">
              <div class="list-card-title">${escapeHtml(r.site_name)}</div>
              <div class="list-card-sub">${escapeHtml(r.work_date)}</div>
            </div>
            ${statusBadge(r.approval_status)}
          </div>
          <div style="display:flex;gap:1.25rem;margin-top:0.6rem">
            <div><div class="stat-tile-label">Doors</div><div class="list-card-title">${r.door_count}</div></div>
            <div><div class="stat-tile-label">Rate</div><div class="list-card-title">${money(r.rate_per_door)}</div></div>
            <div><div class="stat-tile-label">Total</div><div class="list-card-amount">${money(r.total_amount)}</div></div>
          </div>
          ${r.notes ? `<div class="list-card-meta">Note: ${escapeHtml(r.notes)}</div>` : ""}
          ${r.approval_remarks ? `<div class="list-card-meta">Remarks: ${escapeHtml(r.approval_remarks)}</div>` : ""}
          <button class="btn-link" data-idx="${i}" style="align-self:flex-start;margin-top:0.4rem">View Photos</button>
          <div class="photo-strip" id="photos-${i}" style="display:none">
            ${r.before_photos.map((p) => `<img src="${p}" alt="before" />`).join("")}
            ${r.after_photos.map((p) => `<img src="${p}" alt="after" />`).join("")}
          </div>
        </div>
      `
              )
              .join("")
      }
    </div>
  `;
  content.querySelectorAll("[data-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const strip = content.querySelector(`#photos-${btn.dataset.idx}`);
      strip.style.display = strip.style.display === "none" ? "flex" : "none";
    });
  });
}

function shareOnWhatsApp(p) {
  const text = `Payment received: ${money(p.amount)} via ${p.payment_method} on ${p.payment_date}${
    p.transaction_reference ? " (Ref: " + p.transaction_reference + ")" : ""
  } — JONAH ENTERPRISES`;
  window.open(`https://wa.me/?text=${encodeURIComponent(text)}`, "_blank");
}

async function renderPayments(content, user) {
  const [ledger, payments] = await Promise.all([
    get(`/workers/${user.worker_id}/ledger`),
    get("/payments"),
  ]);
  content.innerHTML = `
    <h1 class="page-title">Payments</h1>
    <p class="page-subtitle">Your earnings and history</p>

    <div class="hero-block">
      <div class="hero-label">Total Approved Earnings</div>
      <div class="hero-value">${money(ledger.summary.approved_earnings)}</div>
    </div>
    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-tile-label">Payment Received</div><div class="stat-tile-value">${money(ledger.summary.paid_amount)}</div></div>
      <div class="stat-tile"><div class="stat-tile-label">Pending Payment</div><div class="stat-tile-value">${money(ledger.summary.pending_amount)}</div></div>
    </div>
    <button class="pill-btn outline" id="slip-btn">Download This Month's Slip</button>
    <div class="error" id="slip-error"></div>

    <div class="section-label">Payment History</div>
    <div id="payment-list">
      ${
        payments.length === 0
          ? '<div class="empty-state"><div class="title">No payments yet</div></div>'
          : payments
              .map(
                (p, i) => `
        <div class="list-card" style="flex-direction:column;align-items:stretch">
          <div style="display:flex;justify-content:space-between">
            <div class="list-card-body">
              <div class="list-card-title">${escapeHtml(p.payment_method)}</div>
              <div class="list-card-sub">${escapeHtml(p.payment_date)}</div>
            </div>
            <div class="list-card-amount text-success">${money(p.amount)}</div>
          </div>
          <button class="pill-btn outline pill-btn-sm" style="width:100%;margin-top:0.6rem" data-idx="${i}">Share on WhatsApp</button>
        </div>
      `
              )
              .join("")
      }
    </div>
  `;
  content.querySelector("#slip-btn").addEventListener("click", async (e) => {
    const btn = e.target;
    const errorEl = content.querySelector("#slip-error");
    errorEl.textContent = "";
    btn.disabled = true;
    try {
      const { token } = await post("/worker/salary-slip-token", {});
      window.open(`/api/salary-slip/${user.worker_id}?token=${encodeURIComponent(token)}`, "_blank");
    } catch (err) {
      errorEl.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });
  content.querySelectorAll("[data-idx]").forEach((btn) => {
    btn.addEventListener("click", () => shareOnWhatsApp(payments[Number(btn.dataset.idx)]));
  });
}

async function renderProfile(content, user, nav, logout) {
  const notifications = await get("/notifications");
  content.innerHTML = `
    <h1 class="page-title">Profile</h1>

    <div class="list-card">
      <div class="avatar-circle">${escapeHtml((user.name || user.username)[0].toUpperCase())}</div>
      <div class="list-card-body">
        <div class="list-card-title">${escapeHtml(user.name || user.username)}</div>
        <div class="list-card-sub">@${escapeHtml(user.username)} · ${escapeHtml(user.role)}</div>
      </div>
    </div>

    <button class="list-card" id="leave-link" style="width:100%;text-align:left;border:1px solid var(--border);cursor:pointer">
      <div class="list-card-body">
        <div class="list-card-title">Leave Requests</div>
        <div class="list-card-sub">View history and request time off</div>
      </div>
      <div>›</div>
    </button>

    <div class="section-label">Notifications</div>
    <div id="notif-list">
      ${
        notifications.length === 0
          ? '<div class="empty-state"><div class="title">No notifications</div></div>'
          : notifications
              .map(
                (n) => `
        <div class="list-card" style="flex-direction:column;align-items:stretch">
          <div class="list-card-title">${escapeHtml(n.title)}</div>
          <div class="list-card-sub">${escapeHtml(n.message)}</div>
          <div class="list-card-meta">${formatDateTime(n.created_at)}</div>
        </div>
      `
              )
              .join("")
      }
    </div>

    <button class="pill-btn outline" id="logout-btn" style="margin-top:1rem">Logout</button>
  `;
  content.querySelector("#leave-link").addEventListener("click", () => nav.pushView("leave"));
  content.querySelector("#logout-btn").addEventListener("click", logout);
}

async function renderLeave(content, user, nav) {
  const leaves = await get("/leaves");
  content.innerHTML = `
    <div class="back-row">
      <button class="icon-btn" id="back-btn">←</button>
      <div style="flex:1">
        <h1 class="page-title">Leaves</h1>
        <p class="page-subtitle">Your leave history</p>
      </div>
      <button class="fab" id="add-leave-btn">+</button>
    </div>
    <div id="leave-form-wrap" style="display:none" class="card-block">
      <form id="leave-form">
        <label for="leave-start">Start date</label>
        <input type="date" id="leave-start" required />
        <label for="leave-end">End date</label>
        <input type="date" id="leave-end" required />
        <label for="leave-type">Type</label>
        <select id="leave-type">
          <option>Personal</option><option>Sick</option><option>Emergency</option><option>Other</option>
        </select>
        <label for="leave-reason">Reason</label>
        <textarea id="leave-reason" rows="2" required></textarea>
        <div class="error" id="leave-error"></div>
        <button type="submit" class="pill-btn">Submit Request</button>
      </form>
    </div>
    <div id="leave-list">
      ${
        leaves.length === 0
          ? '<div class="empty-state"><div class="title">No leaves</div>Tap + to request leave.</div>'
          : leaves
              .map(
                (l) => `
        <div class="list-card">
          <div class="list-card-body">
            <div class="list-card-title">${escapeHtml(l.start_date)} → ${escapeHtml(l.end_date)}</div>
            <div class="list-card-sub">${escapeHtml(l.leave_type)} — ${escapeHtml(l.reason)}</div>
          </div>
          ${statusBadge(l.status)}
        </div>
      `
              )
              .join("")
      }
    </div>
  `;
  content.querySelector("#back-btn").addEventListener("click", () => nav.goTab("profile"));
  const formWrap = content.querySelector("#leave-form-wrap");
  content.querySelector("#add-leave-btn").addEventListener("click", () => {
    formWrap.style.display = formWrap.style.display === "none" ? "block" : "none";
  });
  content.querySelector("#leave-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = content.querySelector("#leave-error");
    errorEl.textContent = "";
    try {
      await post("/leaves", {
        start_date: content.querySelector("#leave-start").value,
        end_date: content.querySelector("#leave-end").value,
        leave_type: content.querySelector("#leave-type").value,
        reason: content.querySelector("#leave-reason").value,
      });
      renderLeave(content, user, nav);
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });
}
