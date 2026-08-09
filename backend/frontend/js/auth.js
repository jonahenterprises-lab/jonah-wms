import { post, setToken, setUser } from "./api.js";

export function renderLogin(root, onSuccess) {
  root.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "login-wrap";
  wrap.innerHTML = `
    <form class="login-card" id="login-form">
      <div class="login-brand">JONAH ENTERPRISES</div>
      <p class="login-subtitle">Work Management System</p>
      <label for="login-username">Username</label>
      <input type="text" id="login-username" autocomplete="username" required />
      <label for="login-password">Password</label>
      <input type="password" id="login-password" autocomplete="current-password" required />
      <div class="error" id="login-error"></div>
      <button type="submit" class="btn btn-primary btn-block">Sign in</button>
    </form>
  `;
  root.appendChild(wrap);

  const form = wrap.querySelector("#login-form");
  const errorEl = wrap.querySelector("#login-error");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.textContent = "";
    const username = wrap.querySelector("#login-username").value.trim();
    const password = wrap.querySelector("#login-password").value;
    const btn = form.querySelector("button");
    btn.disabled = true;
    btn.textContent = "Signing in…";
    try {
      const data = await post("/auth/login", { username, password });
      setToken(data.access_token);
      setUser(data.user);
      onSuccess(data.user);
    } catch (err) {
      errorEl.textContent = err.message || "Login failed";
    } finally {
      btn.disabled = false;
      btn.textContent = "Sign in";
    }
  });
}
