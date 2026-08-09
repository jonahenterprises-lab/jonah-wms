const API_BASE = "/api";

function getToken() {
  return localStorage.getItem("jonah_token");
}
function setToken(t) {
  localStorage.setItem("jonah_token", t);
}
function clearToken() {
  localStorage.removeItem("jonah_token");
  localStorage.removeItem("jonah_user");
}
function getUser() {
  const u = localStorage.getItem("jonah_user");
  return u ? JSON.parse(u) : null;
}
function setUser(u) {
  localStorage.setItem("jonah_user", JSON.stringify(u));
}

const DEFAULT_TIMEOUT_MS = 30000;

async function api(path, options = {}) {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...fetchOptions } = options;
  const headers = fetchOptions.headers ? { ...fetchOptions.headers } : {};
  if (fetchOptions.body && !(fetchOptions.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const token = getToken();
  if (token) headers["Authorization"] = "Bearer " + token;

  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    throw new Error("You're offline. Check your connection and try again.");
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(API_BASE + path, { ...fetchOptions, headers, signal: controller.signal });
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error("That took too long to respond. Check your connection and try again.");
    }
    throw new Error("Can't reach the server. Check your connection and try again.");
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401 && token) {
    // Only an *expired/invalid session* forces a reload — a 401 with no token
    // attached (e.g. a wrong-password login attempt) is a normal auth failure
    // and should fall through to show its real server message below.
    clearToken();
    location.reload();
    throw new Error("Not authenticated");
  }

  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      data = text;
    }
  }

  if (!res.ok) {
    const detail = data && typeof data === "object" ? data.detail : null;
    const msg = detail
      ? (typeof detail === "string" ? detail : JSON.stringify(detail))
      : (res.statusText || "Request failed");
    throw new Error(msg);
  }
  return data;
}

const get = (path, opts) => api(path, opts);
const post = (path, body, opts) => api(path, { method: "POST", body: JSON.stringify(body || {}), ...opts });
const put = (path, body, opts) => api(path, { method: "PUT", body: JSON.stringify(body || {}), ...opts });
const del = (path, opts) => api(path, { method: "DELETE", ...opts });

export { api, get, post, put, del, getToken, setToken, clearToken, getUser, setUser };
