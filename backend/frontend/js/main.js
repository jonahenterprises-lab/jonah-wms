import { getToken, api, clearToken, setUser } from "./api.js";
import { renderLogin } from "./auth.js";
import { renderAdmin } from "./admin.js";
import { renderWorker } from "./worker.js";
import { escapeHtml } from "./utils.js";

const root = document.getElementById("app");

async function boot() {
  const token = getToken();
  if (!token) {
    renderLogin(root, renderShell);
    return;
  }
  try {
    const me = await api("/auth/me");
    setUser(me);
    renderShell(me);
  } catch (e) {
    // A 401 already cleared the token and reloaded inside api() — anything else
    // (offline, timeout, server unreachable) is transient, so keep the session
    // and offer a retry instead of silently logging the worker out.
    if (e.message === "Not authenticated") return;
    renderConnectionError(e.message);
  }
}

function renderConnectionError(message) {
  root.innerHTML = `
    <div class="shell">
      <div class="app-frame">
        <div class="content">
          <div class="empty-state">
            <div class="title">Can't connect</div>
            ${escapeHtml(message)}
          </div>
          <button class="pill-btn" id="retry-btn" style="margin-top:1rem">Retry</button>
        </div>
      </div>
    </div>
  `;
  root.querySelector("#retry-btn").addEventListener("click", boot);
}

function logout() {
  clearToken();
  location.reload();
}

function renderShell(user) {
  root.innerHTML = "";
  const shell = document.createElement("div");
  shell.className = "shell" + (user.role === "Admin" ? " role-admin" : "");
  const frame = document.createElement("div");
  frame.className = "app-frame";
  shell.appendChild(frame);
  root.appendChild(shell);

  if (user.role === "Admin") {
    renderAdmin(frame, user, logout);
  } else if (user.role === "Worker") {
    renderWorker(frame, user, logout);
  } else {
    frame.innerHTML = `
      <div class="content">
        <div class="empty-state">
          <div class="title">Not available yet</div>
          The client portal isn't available in this app.
        </div>
        <button class="pill-btn outline" id="logout-btn">Logout</button>
      </div>
    `;
    frame.querySelector("#logout-btn").addEventListener("click", logout);
  }
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {}); // offline shell is a nice-to-have, never block boot on it
  });
}

boot();
