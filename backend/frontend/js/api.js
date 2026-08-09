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

async function api(path, options = {}) {
  const headers = options.headers ? { ...options.headers } : {};
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const token = getToken();
  if (token) headers["Authorization"] = "Bearer " + token;

  const res = await fetch(API_BASE + path, { ...options, headers });

  if (res.status === 401) {
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

const get = (path) => api(path);
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
const put = (path, body) => api(path, { method: "PUT", body: JSON.stringify(body || {}) });
const del = (path) => api(path, { method: "DELETE" });

export { api, get, post, put, del, getToken, setToken, clearToken, getUser, setUser };
