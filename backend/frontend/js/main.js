import { getToken, api, clearToken, setUser } from "./api.js";
import { renderLogin } from "./auth.js";
import { renderAdmin } from "./admin.js";
import { renderWorker } from "./worker.js";

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
    clearToken();
    renderLogin(root, renderShell);
  }
}

function logout() {
  clearToken();
  location.reload();
}

function renderShell(user) {
  root.innerHTML = "";
  const shell = document.createElement("div");
  shell.className = "shell";
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

boot();
