function niFormatDateTimes() {
  const locale = navigator.language || "en-US";
  let timeZone;
  try {
    timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch (_) {
    timeZone = undefined;
  }
  const options = {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  };
  if (timeZone) {
    options.timeZone = timeZone;
  }
  document.querySelectorAll("[data-ni-datetime]").forEach((el) => {
    const iso = el.getAttribute("data-ni-datetime");
    if (!iso) return;
    const d = new Date(iso);
    if (isNaN(d.getTime())) return;
    try {
      const fmt = new Intl.DateTimeFormat(locale, options);
      el.textContent = fmt.format(d);
    } catch (_) {
      el.textContent = d.toISOString();
    }
  });
}

/** Append current URL query to HTMX fragment links so shareable URLs work. */
function niApplyUrlParamsToTabs() {
  const q = window.location.search;
  if (!q) return;
  ["/fragment/live", "/fragment/stats", "/fragment/ports", "/fragment/pipeline"].forEach((path) => {
    document.querySelectorAll(`[hx-get="${path}"]`).forEach((el) => {
      const base = el.getAttribute("hx-get");
      if (base && base.indexOf("?") === -1) {
        el.setAttribute("hx-get", base + q);
      }
    });
  });
  if (q.indexOf("anonymize=1") !== -1) {
    const form = document.getElementById("live-filters");
    if (form && !form.querySelector('input[name="anonymize"][type="hidden"]')) {
      const hid = document.createElement("input");
      hid.type = "hidden";
      hid.name = "anonymize";
      hid.value = "1";
      form.appendChild(hid);
    }
  }
}

function niPollHealth() {
  const el = document.getElementById("health-badge");
  if (!el) return;
  fetch("/api/health")
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!data) {
        el.textContent = "?";
        el.className = "ni-health-badge status-unknown";
        return;
      }
      const s = data.status || "unknown";
      el.textContent = s === "green" ? "●" : s === "yellow" ? "◆" : "■";
      el.className = "ni-health-badge status-" + s;
      el.title = "Drop rate: " + (data.drop_rate_pct ?? "?") + "%";
    })
    .catch(() => {
      el.textContent = "?";
      el.className = "ni-health-badge status-unknown";
    });
}

document.addEventListener("DOMContentLoaded", () => {
  niFormatDateTimes();
  niApplyUrlParamsToTabs();
  niPollHealth();
  setInterval(niPollHealth, 5000);
});

document.body.addEventListener("htmx:afterSwap", () => {
  niFormatDateTimes();
  niApplyUrlParamsToTabs();
});

