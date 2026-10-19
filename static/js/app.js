(() => {
  "use strict";

  const toast = document.getElementById("toast");
  let toastTimer = null;
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

  // Older releases stored owner tokens in localStorage. Account ownership now
  // comes from Google sign-in, so remove the obsolete client-side credentials.
  try { localStorage.removeItem("quickshare.myShares.v1"); } catch (_error) {}

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2800);
  }

  function formatSize(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
    return (value / (1024 * 1024)).toFixed(2) + " MB";
  }

  function formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Unknown";
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  function prettyStatus(value) {
    return ({
      active: "Active",
      expired: "Expired",
      limit: "Limit reached",
      revoked: "Revoked",
      invalid: "Unavailable",
      missing: "Not found"
    })[value] || "Unavailable";
  }

  async function copyText(value) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        const helper = document.createElement("textarea");
        helper.value = value;
        helper.setAttribute("readonly", "");
        helper.style.position = "fixed";
        helper.style.opacity = "0";
        document.body.appendChild(helper);
        helper.select();
        const copied = document.execCommand("copy");
        helper.remove();
        if (!copied) throw new Error("Copy command failed");
      }
      showToast("Share link copied to clipboard.");
      return true;
    } catch (_error) {
      showToast("Copy failed. Select and copy the link manually.");
      return false;
    }
  }

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      const original = button.innerHTML;
      if (await copyText(target.value || target.textContent || "")) {
        button.textContent = "✓ Copied";
        window.setTimeout(() => { button.innerHTML = original; }, 1800);
      }
    });
  });

  document.querySelectorAll(".flash-dismiss").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash")?.remove());
  });

  // Upload form interactions: drag/drop, file size validation, and feedback.
  const fileInput = document.getElementById("file");
  const dropzone = document.getElementById("dropzone");
  const selectedWrap = document.getElementById("selected-file");
  const selectedName = document.getElementById("selected-name");
  const selectedSize = document.getElementById("selected-size");
  const removeFile = document.getElementById("remove-file");
  const uploadForm = document.getElementById("upload-form");
  const submitButton = document.getElementById("upload-submit");
  const filePrompt = document.getElementById("file-prompt");
  const fileDetail = document.getElementById("file-detail");
  const maxBytes = fileInput ? Number((document.querySelector(".file-hint")?.textContent || "").match(/[0-9]+/)?.[0] || 3) * 1024 * 1024 : 0;

  function displaySelectedFile(file) {
    if (!file || !fileInput) return;
    if (file.size > maxBytes) {
      fileInput.value = "";
      showToast("This file is too large. Choose a file within the displayed limit.");
      return;
    }
    if (filePrompt) filePrompt.textContent = "File ready to share";
    if (fileDetail) fileDetail.textContent = "You can change it before uploading";
    if (selectedName) selectedName.textContent = file.name;
    if (selectedSize) selectedSize.textContent = formatSize(file.size);
    if (selectedWrap) selectedWrap.hidden = false;
  }

  if (fileInput && dropzone) {
    fileInput.addEventListener("change", () => displaySelectedFile(fileInput.files?.[0]));
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fileInput.click();
      }
    });
    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("is-dragging");
      });
    });
    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("is-dragging");
      });
    });
    dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer?.files?.[0];
      if (!file) return;
      const transfer = new DataTransfer();
      transfer.items.add(file);
      fileInput.files = transfer.files;
      displaySelectedFile(file);
    });
  }

  if (removeFile && fileInput) {
    removeFile.addEventListener("click", () => {
      fileInput.value = "";
      if (selectedWrap) selectedWrap.hidden = true;
      if (filePrompt) filePrompt.textContent = "Drop your file here";
      if (fileDetail) fileDetail.innerHTML = 'or <span class="browse-link">browse files</span> from your device';
    });
  }

  if (uploadForm && submitButton) {
    uploadForm.addEventListener("submit", (event) => {
      if (!fileInput?.files?.length) {
        event.preventDefault();
        showToast("Choose a file first.");
        return;
      }
      if (fileInput.files[0].size > maxBytes) {
        event.preventDefault();
        showToast("The file exceeds the upload limit.");
        return;
      }
      submitButton.disabled = true;
      const label = submitButton.querySelector("span");
      const arrow = submitButton.querySelector(".btn-arrow");
      if (label) label.textContent = "Uploading to storage…";
      if (arrow) arrow.textContent = "…";
    });
  }

  async function revokeShare(shareId, button) {
    if (button) {
      button.disabled = true;
      button.textContent = "Revoking…";
    }
    try {
      const response = await fetch("/api/shares/" + encodeURIComponent(shareId) + "/revoke", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({})
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Could not revoke this share.");
      showToast("Share revoked. QuickShare downloads are now disabled.");
      window.setTimeout(() => window.location.reload(), 650);
      return true;
    } catch (error) {
      showToast(error.message || "Revoke failed. Try again.");
      if (button) {
        button.disabled = false;
        button.textContent = "Revoke";
      }
      return false;
    }
  }

  const revokeCurrentButton = document.getElementById("revoke-current-share");
  if (revokeCurrentButton) {
    revokeCurrentButton.addEventListener("click", async () => {
      if (!window.confirm("Revoke this share? QuickShare will stop serving downloads from this link.")) return;
      await revokeShare(revokeCurrentButton.dataset.shareId, revokeCurrentButton);
    });
  }

  // Account-scoped My Shares dashboard: the server decides ownership from the
  // verified Google account in the signed session, never from browser-supplied IDs.
  const sharesList = document.getElementById("shares-list");
  if (sharesList) {
    const loading = document.getElementById("shares-loading");
    const empty = document.getElementById("shares-empty");
    const emptyTitle = document.getElementById("empty-title");
    const emptyDescription = document.getElementById("empty-description");
    const searchInput = document.getElementById("share-search");
    const refreshButton = document.getElementById("refresh-shares");
    const statTotal = document.getElementById("stat-total");
    const statDownloads = document.getElementById("stat-downloads");
    const statActive = document.getElementById("stat-active");
    let allRecords = [];
    let filter = "all";
    let isLoading = false;
    let loadFailed = false;

    function element(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = String(text);
      return node;
    }

    function currentLink(shareId) {
      return window.location.origin + "/s/" + encodeURIComponent(shareId);
    }

    function statusClass(status) {
      return "state-" + (["active", "expired", "limit", "revoked", "invalid"].includes(status) ? status : "invalid");
    }

    function makeButton(label, className, handler) {
      const button = element("button", "btn " + className, label);
      button.type = "button";
      button.addEventListener("click", handler);
      return button;
    }

    function buildActivity(record) {
      const details = element("details", "share-activity");
      const events = Array.isArray(record.downloads) ? record.downloads : [];
      details.appendChild(element("summary", "", "Download & access history (" + events.length + ")"));
      const list = element("div", "activity-list");
      if (events.length === 0) {
        list.appendChild(element("p", "activity-empty", "No download or access attempts have been recorded yet."));
      } else {
        [...events].reverse().forEach((event) => {
          const row = element("div", "activity-row");
          row.append(
            element("strong", "", String(event.status || "unknown").replaceAll("_", " ")),
            element("span", "", formatDate(event.time))
          );
          list.appendChild(row);
        });
      }
      details.appendChild(list);
      return details;
    }

    function buildCard(record) {
      const card = element("article", "share-list-card");
      const main = element("div", "share-list-main");
      const icon = element("span", "share-list-icon", "↗");
      icon.setAttribute("aria-hidden", "true");
      const copy = element("div", "share-list-copy");
      const titleRow = element("div", "share-list-title-row");
      titleRow.append(
        element("strong", "share-list-title", record.original_filename || "Shared file"),
        element("span", "share-state " + statusClass(record.status), prettyStatus(record.status))
      );
      copy.append(
        titleRow,
        element("p", "share-list-subtitle", formatSize(record.file_size) + " · " + (record.storage_provider === "google_drive" ? "Google Drive · private" : "GitHub · public") + " · Uploaded " + formatDate(record.upload_time)),
        element("small", "share-list-id", "ID " + record.share_id.slice(0, 8) + "…")
      );
      main.append(icon, copy);
      card.appendChild(main);

      const metricWrap = element("div", "share-list-metrics");
      [
        ["DOWNLOADS", (Number(record.download_count) || 0) + " / " + (Number(record.max_downloads) || 0)],
        ["EXPIRES", formatDate(record.expiry_time)],
        ["REMAINING", Math.max(0, (Number(record.max_downloads) || 0) - (Number(record.download_count) || 0)) + " downloads"]
      ].forEach(([label, value]) => {
        const box = element("div", "share-list-metric");
        box.append(element("span", "", label), element("strong", "", value));
        metricWrap.appendChild(box);
      });
      card.appendChild(metricWrap);

      const actions = element("div", "share-list-actions");
      actions.appendChild(makeButton("Copy link", "btn-secondary", () => copyText(currentLink(record.share_id))));
      if (record.status === "active") {
        const qr = element("a", "btn btn-secondary", "Save QR");
        qr.href = "/qr/" + encodeURIComponent(record.share_id) + ".png";
        qr.download = "quickshare-" + record.share_id + ".png";
        actions.appendChild(qr);
      }
      const open = element("a", "btn btn-secondary", "View share");
      open.href = currentLink(record.share_id);
      actions.appendChild(open);
      if (record.status === "active") {
        actions.appendChild(makeButton("Revoke", "btn-danger", async (event) => {
          if (!window.confirm("Revoke this share? QuickShare will stop serving downloads from this link.")) return;
          const ok = await revokeShare(record.share_id, event.currentTarget);
          if (ok) await refreshDashboard();
        }));
      }
      card.appendChild(actions);
      card.appendChild(buildActivity(record));
      return card;
    }

    function renderRecords() {
      sharesList.replaceChildren();
      const query = (searchInput?.value || "").trim().toLowerCase();
      const filtered = allRecords.filter((record) => {
        const name = String(record.original_filename || "").toLowerCase();
        const filterMatch = filter === "all" || record.status === filter;
        return filterMatch && (!query || name.includes(query) || String(record.share_id).includes(query));
      });
      filtered.forEach((record) => sharesList.appendChild(buildCard(record)));
      if (empty) empty.hidden = filtered.length !== 0;
      if (emptyTitle) emptyTitle.textContent = loadFailed ? "Couldn't load your shares" : allRecords.length === 0 ? "No shares yet" : "No matches found";
      if (emptyDescription) emptyDescription.textContent = loadFailed
        ? "The account share list could not be loaded. Try refreshing in a moment."
        : allRecords.length === 0
          ? "Upload a file to create a share. It will be available here whenever you sign in to this account."
          : "Try another search or filter. Your other shares are still available.";
      if (loading) loading.hidden = true;
    }

    async function refreshDashboard() {
      if (isLoading) return;
      isLoading = true;
      loadFailed = false;
      if (loading) loading.hidden = false;
      if (empty) empty.hidden = true;
      if (refreshButton) refreshButton.disabled = true;
      try {
        const response = await fetch("/api/my-shares", { credentials: "same-origin", cache: "no-store" });
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) {
          window.location.href = "/login?next=%2Fmy-shares";
          return;
        }
        if (!response.ok) throw new Error(data.error || "Could not load shares");
        allRecords = Array.isArray(data.shares) ? data.shares : [];
        if (statTotal) statTotal.textContent = String(allRecords.length);
        if (statDownloads) statDownloads.textContent = String(allRecords.reduce((sum, record) => sum + (Number(record.download_count) || 0), 0));
        if (statActive) statActive.textContent = String(allRecords.filter((record) => record.status === "active").length);
        renderRecords();
      } catch (_error) {
        allRecords = [];
        loadFailed = true;
        if (statTotal) statTotal.textContent = "—";
        if (statDownloads) statDownloads.textContent = "—";
        if (statActive) statActive.textContent = "—";
        renderRecords();
      } finally {
        isLoading = false;
        if (refreshButton) refreshButton.disabled = false;
        if (loading) loading.hidden = true;
      }
    }

    document.querySelectorAll("[data-filter]").forEach((button) => {
      button.addEventListener("click", () => {
        filter = button.dataset.filter || "all";
        document.querySelectorAll("[data-filter]").forEach((candidate) => candidate.classList.toggle("is-active", candidate === button));
        renderRecords();
      });
    });
    if (searchInput) searchInput.addEventListener("input", renderRecords);
    if (refreshButton) refreshButton.addEventListener("click", refreshDashboard);
    refreshDashboard();
  }

  document.querySelectorAll("time[data-format-date]").forEach((time) => {
    const raw = time.getAttribute("datetime");
    if (!raw) return;
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return;
    time.textContent = formatDate(raw);
    time.title = date.toLocaleString();
  });

  // Storage provider chooser explains the visibility trade-off before upload.
  const storageProviderSelect = document.getElementById("storage_provider");
  const storageProviderHelp = document.getElementById("storage-provider-help");
  function updateStorageProviderHelp() {
    if (!storageProviderSelect || !storageProviderHelp) return;
    if (storageProviderSelect.value === "google_drive") {
      storageProviderHelp.textContent = "File contents are stored in the administrator's connected Drive for all users. Share metadata remains in the public GitHub repository.";
      storageProviderHelp.classList.add("storage-help-private");
    } else {
      storageProviderHelp.textContent = "GitHub file contents are public and can be accessed outside QuickShare, even after a link expires.";
      storageProviderHelp.classList.remove("storage-help-private");
    }
  }
  if (storageProviderSelect) {
    storageProviderSelect.addEventListener("change", updateStorageProviderHelp);
    updateStorageProviderHelp();
  }

  const driveTokenButton = document.getElementById("copy-drive-refresh-token");
  if (driveTokenButton) {
    driveTokenButton.addEventListener("click", async () => {
      const tokenInput = document.getElementById("drive-refresh-token");
      if (!tokenInput) return;
      const token = tokenInput.value;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(token);
        } else {
          tokenInput.focus();
          tokenInput.select();
          if (!document.execCommand("copy")) throw new Error("Copy failed");
        }
        showToast("Refresh token copied. Add it to Vercel Environment Variables now.");
      } catch (_error) {
        tokenInput.focus();
        tokenInput.select();
        showToast("Automatic copy failed. Copy the selected token manually.");
      }
    });
  }

})();
