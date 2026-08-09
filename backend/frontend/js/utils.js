// Fallback path: decodes the ORIGINAL file at full resolution before scaling down.
// Fine for normal photos, but for a 48MP+ shot the full decode alone can exceed a
// phone browser's memory budget before we ever get to shrink it.
function fileToDataUriViaImage(file, maxDim, quality) {
  return new Promise((resolve, reject) => {
    const objectUrl = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(objectUrl);
      let { width, height } = img;
      if (width > maxDim || height > maxDim) {
        const ratio = Math.min(maxDim / width, maxDim / height);
        width = Math.round(width * ratio);
        height = Math.round(height * ratio);
      }
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      canvas.getContext("2d").drawImage(img, 0, 0, width, height);
      resolve(canvas.toDataURL("image/jpeg", quality));
    };
    img.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      reject(new Error("Could not read photo — it may be corrupted or an unsupported format"));
    };
    img.src = objectUrl;
  });
}

// Phone cameras produce huge originals (often 8-15MB, 4000px+, and 48MP+ on newer
// phones). Decoding that many bytes to base64 and holding several in memory at once
// is what triggers WebKit's "unable to complete previous operation due to low memory"
// error on iOS. createImageBitmap's resize option lets supporting browsers decode
// directly at a reduced scale instead of fully materializing the original bitmap
// first — a 48MP photo never exists in memory at full size at all. We probe the
// aspect ratio with a throwaway tiny decode first since resizeWidth/resizeHeight
// need concrete target dimensions to avoid distorting the image.
async function fileToDataUri(file, maxDim = 1600, quality = 0.82) {
  if (typeof createImageBitmap === "function") {
    try {
      const probe = await createImageBitmap(file, { resizeWidth: 64, resizeQuality: "low" });
      const aspect = probe.width / probe.height;
      probe.close();
      const targetW = aspect >= 1 ? maxDim : Math.round(maxDim * aspect);
      const targetH = aspect >= 1 ? Math.round(maxDim / aspect) : maxDim;
      const bitmap = await createImageBitmap(file, {
        resizeWidth: targetW,
        resizeHeight: targetH,
        resizeQuality: "medium",
      });
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.getContext("2d").drawImage(bitmap, 0, 0);
      bitmap.close();
      return canvas.toDataURL("image/jpeg", quality);
    } catch (e) {
      // Unsupported or failed (e.g. HEIC quirks) — fall through to the Image-based path.
    }
  }
  return fileToDataUriViaImage(file, maxDim, quality);
}

// Sequential, not Promise.all — decoding several full-size photos at once is exactly
// what spikes peak memory on phones. One at a time keeps only one decode in flight.
async function filesToDataUris(fileList) {
  const results = [];
  for (const file of Array.from(fileList)) {
    results.push(await fileToDataUri(file));
  }
  return results;
}

function getPosition() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("Geolocation is not supported by this browser"));
      return;
    }
    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: true,
      timeout: 15000,
    });
  });
}

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function money(n, currency = "₹") {
  const v = Number(n || 0);
  return currency + v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function formatDateTime(iso) {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
  } catch (e) {
    return iso;
  }
}

function showMessage(container, text, type = "info") {
  let el = container.querySelector(":scope > .msg-banner");
  if (!el) {
    el = document.createElement("div");
    el.className = "msg-banner";
    container.prepend(el);
  }
  el.textContent = text;
  el.className = `msg-banner msg-${type}`;
  el.style.display = "block";
  if (type !== "error") {
    clearTimeout(el._timer);
    el._timer = setTimeout(() => {
      el.style.display = "none";
    }, 4000);
  }
}

function statusBadge(status) {
  const cls = {
    Approved: "badge-success",
    Pending: "badge-warning",
    Rejected: "badge-danger",
    Correction: "badge-info",
    Active: "badge-success",
    Disabled: "badge-danger",
  }[status] || "badge-neutral";
  return `<span class="badge ${cls}">${escapeHtml(status)}</span>`;
}

export {
  fileToDataUri,
  filesToDataUris,
  getPosition,
  uuid,
  money,
  escapeHtml,
  formatDateTime,
  showMessage,
  statusBadge,
};
