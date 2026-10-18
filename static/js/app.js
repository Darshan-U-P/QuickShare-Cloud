(() => {
  "use strict";

  const HISTORY_KEY = "quickshare.myShares.v1";
  const toast = document.getElementById("toast");
  let toastTimer = null;

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2800);
  }

  function validShareId(value) {
    return typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
  }

  function readHistory() {
    try {
      const value = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      if (!Array.isArray(value)) return [];
      return value.filter((item) => item && validShareId(item.shareId));
    } catch (_error) {
      return [];
    }
  }

  function saveHistory(records) {
    try {
      const unique = [];
      const seen = new Set();
      for (const record of records) {
        if (!record || !validShareId(record.shareId) || seen.has(record.shareId)) continue;
        seen.add(record.shareId);
        unique.push(record);
      }
      localStorage.setItem(HISTORY_KEY, JSON.stringify(unique.slice(0, 100)));
      return true;
    } catch (_error) {
      showToast("Browser storage is unavailable. Save the share link separately.");
      return false;
    }
  }

  function findSavedShare(shareId) {
    return readHistory().find((item) => item.shareId === shareId) || null;
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
    const labels = {
      active: "Active",
      expired: "Expired",
      limit: "Limit reached",
      revoked: "Revoked",
      invalid: "Unavailable",
      missing: "Not found"
    };
    return labels[value] || "Unavailable";
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
      const copied = await copyText(target.value || target.textContent || "");
      if (copied) {
        button.textContent = "✓ Copied";
        window.setTimeout(() => { button.innerHTML = original; }, 1800);
      }
    });
  });

  document.querySelectorAll(".flash-dismiss").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash")?.remove());
  });

  // The upload handler only exposes this one-time owner token to the browser
  // that completed the upload. Save it locally so the owner can manage the share.
  const createdShareScript = document.getElementById("quickshare-created-share");
  if (createdShareScript) {
    try {
      const created = JSON.parse(createdShareScript.textContent || "{}");
      if (validShareId(created.share_id) && typeof created.owner_token === "string" && created.owner_token.length > 20) {
        const history = readHistory().filter((item) => item.shareId !== created.share_id);
        history.unshift({
          shareId: created.share_id,
          ownerToken: created.owner_token,
          filename: created.filename || "Shared file",
          uploadTime: created.upload_time || new Date().toISOString()
        });
        saveHistory(history);
        showToast("Share saved to My Shares in this browser.");
      }
    } catch (_error) {
      showToast("This share was created, but browser history could not be saved.");
    }
  }

  // Upload form: file picker, drag and drop, validation, and disabled submit state.
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

  // Owner-only revoke button on an individual share page.
  const revokeCurrentButton = document.getElementById("revoke-current-share");
  if (revokeCurrentButton) {
    const shareId = revokeCurrentButton.dataset.shareId;
    const record = findSavedShare(shareId);
    const ownerHint = document.getElementById("owner-hint");
    if (record?.ownerToken) {
      revokeCurrentButton.hidden = false;
      if (ownerHint) ownerHint.textContent = "You're managing this share from this browser.";
      revokeCurrentButton.addEventListener("click", async () => {
        if (!window.confirm("Revoke this share? QuickShare will stop serving downloads from this link.")) return;
        await revokeShare(shareId, record.ownerToken, revokeCurrentButton);
      });
    } else if (ownerHint) {
      ownerHint.hidden = true;
    }
  }

  async function revokeShare(shareId, ownerToken, button) {
    if (button) {
      button.disabled = true;
      button.textContent = "Revoking…";
    }
    try {
      const response = await fetch("/api/shares/" + encodeURIComponent(shareId) + "/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner_token: ownerToken })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Could not revoke this share.");
      showToast("Share revoked. QuickShare downloads are now disabled.");
      window.setTimeout(() => window.location.reload(), 700);
    } catch (error) {
      showToast(error.message || "Revoke failed. Try again.");
      if (button) {
        button.disabled = false;
        button.textContent = "Revoke this link";
      }
    }
  }

  // My Shares: browser-local history, live status, owner-authorized activity and revoke.
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

    async function refreshRecord(record) {
      const fallback = {
        share_id: record.shareId,
        original_filename: record.filename || "Shared file",
        upload_time: record.uploadTime || "",
        status: "missing",
        file_size: 0,
        download_count: 0,
        max_downloads: 0,
        expiry_time: ""
      };
      try {
        const response = await fetch("/api/shares/" + encodeURIComponent(record.shareId), { cache: "no-store" });
        if (!response.ok) return { ...record, ...fallback, status: response.status === 404 ? "missing" : "invalid", analytics: null };
        const data = await response.json();
        let analytics = null;
        if (record.ownerToken) {
          try {
            const activityResponse = await fetch("/api/shares/" + encodeURIComponent(record.shareId) + "/analytics", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ owner_token: record.ownerToken })
            });
            if (activityResponse.ok) analytics = await activityResponse.json();
          } catch (_error) {
            analytics = null;
          }
        }
        return { ...record, ...data, analytics };
      } catch (_error) {
        return { ...record, ...fallback, status: "invalid", analytics: null, loadError: true };
      }
    }

    function makeButton(label, className, handler) {
      const button = element("button", "btn " + className, label);
      button.type = "button";
      button.addEventListener("click", handler);
      return button;
    }

    function buildActivity(record) {
      const details = element("details", "share-activity");
      const events = record.analytics?.downloads;
      const summary = element("summary", "", "Download & access history" + (events ? " (" + events.length + ")" : ""));
      details.appendChild(summary);
      const list = element("div", "activity-list");
      if (!record.ownerToken) {
        list.appendChild(element("p", "activity-empty", "Owner activity is unavailable for this share. It may have been created before My Shares was added, or its owner key is not saved in this browser."));
      } else if (!record.analytics) {
        list.appendChild(element("p", "activity-empty", "Could not load activity. Refresh and try again."));
      } else if (!events || events.length === 0) {
        list.appendChild(element("p", "activity-empty", "No download attempts have been recorded yet."));
      } else {
        [...events].reverse().forEach((event) => {
          const row = element("div", "activity-row");
          const eventName = element("strong", "", String(event.status || "unknown").replaceAll("_", " "));
          const eventTime = element("span", "", formatDate(event.time));
          row.append(eventName, eventTime);
          list.appendChild(row);
        });
      }
      details.appendChild(list);
      return details;
    }

    function buildCard(record) {
      const card = element("article", "share-list-card");
      const main = element("div", "share-list-main");
      const fileIcon = element("span", "share-list-icon", "↗");
      fileIcon.setAttribute("aria-hidden", "true");
      const copy = element("div", "share-list-copy");
      const titleRow = element("div", "share-list-title-row");
      titleRow.appendChild(element("strong", "share-list-title", record.original_filename || record.filename || "Shared file"));
      titleRow.appendChild(element("span", "share-state " + statusClass(record.status), prettyStatus(record.status)));
      copy.appendChild(titleRow);
      copy.appendChild(element("p", "share-list-subtitle", formatSize(record.file_size) + " · Uploaded " + formatDate(record.upload_time || record.uploadTime)));
      copy.appendChild(element("small", "share-list-id", "ID " + record.shareId.slice(0, 8) + "…"));
      main.append(fileIcon, copy);
      card.appendChild(main);

      const metricWrap = element("div", "share-list-metrics");
      const values = [
        ["DOWNLOADS", (Number(record.download_count) || 0) + " / " + (Number(record.max_downloads) || 0)],
        ["EXPIRES", record.expiry_time ? formatDate(record.expiry_time) : "Unknown"],
        ["REMAINING", Math.max(0, (Number(record.max_downloads) || 0) - (Number(record.download_count) || 0)) + " downloads"]
      ];
      values.forEach(([label, value]) => {
        const box = element("div", "share-list-metric");
        box.append(element("span", "", label), element("strong", "", value));
        metricWrap.appendChild(box);
      });
      card.appendChild(metricWrap);

      const actions = element("div", "share-list-actions");
      actions.appendChild(makeButton("Copy link", "btn-secondary", () => copyText(currentLink(record.shareId))));
      if (record.status === "active") {
        const qr = element("a", "btn btn-secondary", "Save QR");
        qr.href = "/qr/" + encodeURIComponent(record.shareId) + ".png";
        qr.download = "quickshare-" + record.shareId + ".png";
        actions.appendChild(qr);
      }
      const open = element("a", "btn btn-secondary", "View share");
      open.href = currentLink(record.shareId);
      actions.appendChild(open);
      if (record.ownerToken && record.status === "active") {
        actions.appendChild(makeButton("Revoke", "btn-danger", async (event) => {
          const button = event.currentTarget;
          if (!window.confirm("Revoke this share? QuickShare will stop serving downloads from this link.")) return;
          await revokeShare(record.shareId, record.ownerToken, button);
          await refreshDashboard();
        }));
      }
      actions.appendChild(makeButton("Remove from list", "btn-muted", () => {
        if (!window.confirm("Remove this share from this browser's list? This does not revoke the link.")) return;
        saveHistory(readHistory().filter((item) => item.shareId !== record.shareId));
        showToast("Removed from this browser's list.");
        refreshDashboard();
      }));
      card.appendChild(actions);
      card.appendChild(buildActivity(record));
      return card;
    }

    function renderRecords() {
      sharesList.replaceChildren();
      const query = (searchInput?.value || "").trim().toLowerCase();
      const filtered = allRecords.filter((record) => {
        const name = String(record.original_filename || record.filename || "").toLowerCase();
        const id = String(record.shareId || "");
        const filterMatch = filter === "all" || record.status === filter;
        return filterMatch && (!query || name.includes(query) || id.includes(query));
      });
      filtered.forEach((record) => sharesList.appendChild(buildCard(record)));
      if (empty) empty.hidden = filtered.length !== 0;
      if (emptyTitle) emptyTitle.textContent = allRecords.length === 0 ? "No shares saved yet" : "No matches found";
      if (emptyDescription) emptyDescription.textContent = allRecords.length === 0
        ? "Upload a file to create a link. It will appear here in this browser."
        : "Try another search or filter. Your saved shares are still in this browser.";
      if (loading) loading.hidden = true;
    }

    async function refreshDashboard() {
      if (isLoading) return;
      isLoading = true;
      if (loading) loading.hidden = false;
      if (empty) empty.hidden = true;
      if (refreshButton) refreshButton.disabled = true;
      try {
        const saved = readHistory();
        allRecords = await Promise.all(saved.slice(0, 100).map(refreshRecord));
        if (statTotal) statTotal.textContent = String(allRecords.length);
        if (statDownloads) statDownloads.textContent = String(allRecords.reduce((sum, record) => sum + (Number(record.download_count) || 0), 0));
        if (statActive) statActive.textContent = String(allRecords.filter((record) => record.status === "active").length);
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
})();
