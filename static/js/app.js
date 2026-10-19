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
    if (value < 1024 ** 3) return (value / (1024 * 1024)).toFixed(2) + " MB";
    if (value < 1024 ** 4) return (value / (1024 ** 3)).toFixed(2) + " GB";
    return (value / (1024 ** 4)).toFixed(2) + " TB";
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

  // Upload interactions. Drive uploads are initiated by Vercel, then file bytes
  // travel directly from the browser to Google's resumable-upload endpoint.
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
  const fileHint = document.querySelector(".file-hint");
  const storageProviderSelect = document.getElementById("storage_provider");
  const progressWrap = document.getElementById("upload-progress");
  const progressLabel = document.getElementById("upload-progress-label");
  const progressPercent = document.getElementById("upload-progress-percent");
  const progressBar = document.getElementById("upload-progress-bar");
  const progressDetail = document.getElementById("upload-progress-detail");

  const dataBytes = (name, fallback) => Number(fileInput?.dataset?.[name] || fallback);
  function currentMaxBytes() {
    if (!fileInput) return 0;
    if (storageProviderSelect?.value === "google_drive" && fileInput.dataset.googleDriveReady === "true") {
      if (fileInput.dataset.largeTransferReady !== "true") {
        return dataBytes("githubLimitBytes", 4 * 1024 * 1024);
      }
      return dataBytes(
        fileInput.dataset.largeUploadAllowed === "true" ? "approvedLimitBytes" : "standardLimitBytes",
        fileInput.dataset.largeUploadAllowed === "true" ? 50 * 1024 ** 3 : 5 * 1024 ** 3
      );
    }
    return dataBytes("githubLimitBytes", 4 * 1024 * 1024);
  }

  function updateUploadLimitHint() {
    if (!fileHint || !fileInput) return;
    if (storageProviderSelect?.value === "google_drive" && fileInput.dataset.googleDriveReady === "true") {
      if (fileInput.dataset.largeTransferReady !== "true") {
        fileHint.textContent = "Maximum 4 MB until the large-transfer service is configured";
        return;
      }
      const approved = fileInput.dataset.largeUploadAllowed === "true";
      fileHint.textContent = "Up to " + (approved ? "50 GB" : "5 GB") +
        (approved ? " per file" : " per file · 50 GB requires approval") +
        " · uploads resume automatically";
    } else {
      fileHint.textContent = "Maximum 4 MB via GitHub storage · PDF, images, documents and archives";
    }
  }

  function updateUploadProgress(done, total, label, detail) {
    const percent = total > 0 ? Math.min(100, Math.floor(done / total * 100)) : 0;
    if (progressWrap) progressWrap.hidden = false;
    if (progressLabel) progressLabel.textContent = label;
    if (progressPercent) progressPercent.textContent = percent + "%";
    if (progressBar) progressBar.style.width = percent + "%";
    if (progressDetail) progressDetail.textContent = detail || (formatSize(done) + " of " + formatSize(total));
  }

  function resetUploadProgress() {
    if (progressWrap) progressWrap.hidden = true;
    if (progressBar) progressBar.style.width = "0%";
    if (progressPercent) progressPercent.textContent = "0%";
  }

  function displaySelectedFile(file) {
    if (!file || !fileInput) return;
    const maxBytes = currentMaxBytes();
    if (file.size > maxBytes) {
      fileInput.value = "";
      if (selectedWrap) selectedWrap.hidden = true;
      showToast("This file exceeds the current storage option's limit.");
      return;
    }
    if (filePrompt) filePrompt.textContent = "File ready to share";
    if (fileDetail) fileDetail.textContent = "You can change it before uploading";
    if (selectedName) selectedName.textContent = file.name;
    if (selectedSize) selectedSize.textContent = formatSize(file.size);
    if (selectedWrap) selectedWrap.hidden = false;
  }

  function setUploadBusy(busy, label) {
    if (!submitButton) return;
    submitButton.disabled = busy;
    const labelNode = submitButton.querySelector("span");
    const arrow = submitButton.querySelector(".btn-arrow");
    if (labelNode) labelNode.textContent = label || (busy ? "Uploading…" : "Upload and create share link");
    if (arrow) arrow.textContent = busy ? "…" : "↗";
    if (removeFile) removeFile.disabled = busy;
    if (fileInput) fileInput.disabled = busy;
    if (storageProviderSelect) storageProviderSelect.disabled = busy;
  }

  async function responseJson(response) {
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.error || ("Request failed (HTTP " + response.status + ")."));
    return value;
  }

  function offsetFromRange(response) {
    const value = response.headers.get("Range") || response.headers.get("range") || "";
    const match = value.match(/bytes[= ]\d+-(\d+)/i);
    return match ? Number(match[1]) + 1 : null;
  }

  async function uploadLargeDriveFile(file) {
    const metadata = {
      filename: file.name,
      file_size: file.size,
      mime_type: file.type || "application/octet-stream",
      expiry: document.getElementById("expiry")?.value || "24h",
      max_downloads: Number(document.getElementById("max_downloads")?.value || 3),
      storage_provider: "google_drive"
    };
    updateUploadProgress(0, file.size, "Preparing secure upload…", "Opening a resumable session with Google Drive.");
    const initResponse = await fetch("/api/uploads/initiate", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(metadata)
    });
    const init = await responseJson(initResponse);
    const uploadTicket = init.upload_ticket;
    const chunkSize = Math.min(4 * 1024 * 1024, Math.max(256 * 1024, Number(init.chunk_size) || 4 * 1024 * 1024));
    if (!uploadTicket) throw new Error("QuickShare did not return a secure upload ticket.");

    let offset = 0;
    let completedFile = null;
    while (offset < file.size && !completedFile) {
      const chunkEnd = Math.min(offset + chunkSize, file.size);
      const chunk = file.slice(offset, chunkEnd);
      let advanced = false;
      let lastError = null;

      for (let attempt = 0; attempt < 5 && !advanced; attempt++) {
        let response;
        try {
          // Browser sends only a <=4 MiB chunk to the same-origin Vercel endpoint.
          // The server forwards those bytes to the Drive resumable session.
          response = await fetch("/api/uploads/chunk", {
            method: "POST",
            credentials: "same-origin",
            headers: {
              "Content-Type": "application/octet-stream",
              "X-CSRF-Token": csrfToken,
              "X-Upload-Ticket": uploadTicket,
              "Content-Range": "bytes " + offset + "-" + (chunkEnd - 1) + "/" + file.size
            },
            body: chunk
          });
        } catch (error) {
          lastError = new Error("Network error while sending upload chunk to QuickShare. Retrying…");
          if (attempt < 4) await pause(Math.min(8000, 600 * (2 ** attempt)));
          continue;
        }

        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
          const error = new Error(result.error || ("Chunk request failed (HTTP " + response.status + ")."));
          if (response.status < 500 && response.status !== 408 && response.status !== 429) throw error;
          lastError = error;
          if (attempt < 4) await pause(Math.min(8000, 600 * (2 ** attempt)));
          continue;
        }

        if (result.complete === true && result.drive_file_id) {
          completedFile = { id: result.drive_file_id };
          offset = file.size;
          advanced = true;
          updateUploadProgress(file.size, file.size, "Finishing upload…", "All file bytes reached Google Drive. Verifying the completed file.");
          break;
        }

        const nextOffset = Number(result.next_offset);
        if (!Number.isFinite(nextOffset) || nextOffset < 0 || nextOffset > file.size) {
          throw new Error("QuickShare received an invalid upload offset from Google Drive.");
        }
        if (nextOffset > offset) {
          offset = nextOffset;
          advanced = true;
          updateUploadProgress(
            offset,
            file.size,
            "Uploading to Google Drive…",
            formatSize(offset) + " of " + formatSize(file.size) + " uploaded"
          );
          break;
        }

        lastError = new Error("Google Drive has not acknowledged new bytes yet. Retrying this chunk…");
        if (attempt < 4) await pause(Math.min(8000, 600 * (2 ** attempt)));
      }

      if (!advanced && !completedFile) {
        throw lastError || new Error("This chunk could not be uploaded. Check your connection and retry.");
      }
    }

    if (!completedFile?.id) throw new Error("Google Drive did not confirm the completed file.");
    updateUploadProgress(file.size, file.size, "Registering your share link…", "Checking the stored file and saving the share metadata.");
    const finalizeResponse = await fetch("/api/uploads/finalize", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ upload_ticket: uploadTicket, drive_file_id: completedFile.id })
    });
    const finalized = await responseJson(finalizeResponse);
    updateUploadProgress(file.size, file.size, "Share created", "Opening your new share page…");
    window.location.assign(finalized.share_url);
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
      resetUploadProgress();
      if (filePrompt) filePrompt.textContent = "Choose a file to upload";
      if (fileDetail) fileDetail.innerHTML = 'Drag and drop it here, or <span class="browse-link">browse files</span>';
    });
  }

  if (uploadForm && submitButton) {
    uploadForm.addEventListener("submit", async (event) => {
      if (!fileInput?.files?.length) {
        event.preventDefault();
        showToast("Choose a file first.");
        return;
      }
      const file = fileInput.files[0];
      const maxBytes = currentMaxBytes();
      if (file.size > maxBytes) {
        event.preventDefault();
        showToast("The selected file exceeds the current upload limit.");
        return;
      }

      if (storageProviderSelect?.value === "google_drive" && fileInput.dataset.googleDriveReady === "true") {
        event.preventDefault();
        setUploadBusy(true, "Preparing upload…");
        try {
          await uploadLargeDriveFile(file);
        } catch (error) {
          showToast(error.message || "Upload failed. Please retry.");
          updateUploadProgress(0, file.size, "Upload paused", error.message || "The upload did not complete. Your file is still on your device.");
          setUploadBusy(false);
        }
        return;
      }

      if (file.size > dataBytes("githubLimitBytes", 4 * 1024 * 1024)) {
        event.preventDefault();
        showToast("GitHub storage supports only small demo uploads. Select Google Drive for larger files.");
        return;
      }
      setUploadBusy(true, "Uploading to storage…");
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
      actions.appendChild(makeButton("Copy share link", "btn-secondary", () => copyText(currentLink(record.share_id))));
      if (record.status === "active") {
        const directLink = window.location.origin + "/download/" + encodeURIComponent(record.share_id);
        actions.appendChild(makeButton("Copy download link", "btn-secondary", () => copyText(directLink)));
        const download = element("a", "btn btn-primary", "Download file");
        download.href = "/download/" + encodeURIComponent(record.share_id);
        actions.appendChild(download);
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
    storageProviderSelect.addEventListener("change", () => {
      updateStorageProviderHelp();
      updateUploadLimitHint();
      if (fileInput?.files?.[0]) displaySelectedFile(fileInput.files[0]);
    });
    updateStorageProviderHelp();
    updateUploadLimitHint();
  }

  // Large private files are downloaded as bounded ranges. Chromium's
  // File System Access API writes each range directly to disk, avoiding a giant Blob.
  const largeDownloadButton = document.getElementById("start-large-download");
  if (largeDownloadButton) {
    const shareId = largeDownloadButton.dataset.shareId || "";
    const totalBytes = Number(largeDownloadButton.dataset.fileSize || 0);
    const fileName = largeDownloadButton.dataset.fileName || "quickshare-download.bin";
    const chunkSize = Math.min(4 * 1024 * 1024, Number(largeDownloadButton.dataset.chunkSize || 4 * 1024 * 1024));
    const progressWrap = document.getElementById("large-download-progress");
    const statusLabel = document.getElementById("large-download-status");
    const percentLabel = document.getElementById("large-download-percent");
    const progressBar = document.getElementById("large-download-bar");
    const detail = document.getElementById("large-download-detail");
    let currentTicket = "";
    let ticketExpiresAt = 0;
    let refreshPromise = null;

    function setDownloadProgress(done, message) {
      const percent = totalBytes > 0 ? Math.min(100, Math.floor(done / totalBytes * 100)) : 0;
      if (progressWrap) progressWrap.hidden = false;
      if (statusLabel) statusLabel.textContent = message || "Downloading file…";
      if (percentLabel) percentLabel.textContent = percent + "%";
      if (progressBar) progressBar.style.width = percent + "%";
      if (detail) detail.textContent = formatSize(done) + " of " + formatSize(totalBytes) + " downloaded";
    }

    async function postDownloadJson(url, payload) {
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify(payload || {})
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || ("Download request failed (HTTP " + response.status + ")."));
      return data;
    }

    async function refreshTicket(force) {
      if (!force && ticketExpiresAt > Math.floor(Date.now() / 1000) + 90) return;
      if (refreshPromise) return refreshPromise;
      refreshPromise = (async () => {
        const data = await postDownloadJson("/api/download/refresh", { ticket: currentTicket });
        currentTicket = data.ticket;
        ticketExpiresAt = Number(data.ticket_expires_at || 0);
      })();
      try {
        await refreshPromise;
      } finally {
        refreshPromise = null;
      }
    }

    async function fetchRange(start, end) {
      let lastError = null;
      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          await refreshTicket(false);
          const response = await fetch("/api/download/chunk", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
            body: JSON.stringify({ ticket: currentTicket, start, end })
          });
          if (response.ok && response.status === 206) {
            const bytes = await response.arrayBuffer();
            if (bytes.byteLength !== end - start + 1) throw new Error("Received an incomplete file range.");
            return bytes;
          }
          const data = await response.json().catch(() => ({}));
          const error = new Error(data.error || ("File range request failed (HTTP " + response.status + ")."));
          if (response.status === 410 && /ticket needs refreshing/i.test(error.message)) {
            await refreshTicket(true);
            continue;
          }
          if (![408, 429, 500, 502, 503, 504].includes(response.status)) throw error;
          lastError = error;
        } catch (error) {
          lastError = error;
          if (/expired or was revoked|share has expired|no longer exists|invalid or the share has expired/i.test(error.message || "")) {
            throw error;
          }
        }
        if (attempt < 4) await pause(Math.min(10000, 700 * (2 ** attempt)));
      }
      throw lastError || new Error("This file range could not be downloaded after several retries.");
    }

    async function loadTicket() {
      const data = await postDownloadJson("/api/download/initiate/" + encodeURIComponent(shareId), {});
      currentTicket = data.ticket;
      ticketExpiresAt = Number(data.ticket_expires_at || 0);
    }

    async function downloadToFile(writable) {
      await loadTicket();
      const concurrency = 4;
      let done = 0;
      setDownloadProgress(0, "Starting secure download…");
      for (let groupStart = 0; groupStart < totalBytes; groupStart += chunkSize * concurrency) {
        await refreshTicket(false);
        const ranges = [];
        for (let slot = 0; slot < concurrency; slot++) {
          const start = groupStart + slot * chunkSize;
          if (start >= totalBytes) break;
          ranges.push({ start, end: Math.min(totalBytes - 1, start + chunkSize - 1) });
        }
        const parts = await Promise.all(ranges.map(range => fetchRange(range.start, range.end)));
        for (let i = 0; i < ranges.length; i++) {
          await writable.write({ type: "write", position: ranges[i].start, data: parts[i] });
          done += parts[i].byteLength;
        }
        setDownloadProgress(done, "Downloading from private Drive…");
      }
      await writable.close();
      setDownloadProgress(totalBytes, "Download complete");
      if (detail) detail.textContent = "Saved " + fileName + " to the location you selected.";
    }

    async function downloadWithBlob() {
      if (totalBytes > 512 * 1024 * 1024) {
        throw new Error("This browser cannot safely save a multi-gigabyte file directly to disk. Open this link in desktop Microsoft Edge or Google Chrome.");
      }
      await loadTicket();
      const chunks = [];
      let done = 0;
      const concurrency = 4;
      setDownloadProgress(0, "Preparing browser download…");
      for (let groupStart = 0; groupStart < totalBytes; groupStart += chunkSize * concurrency) {
        await refreshTicket(false);
        const ranges = [];
        for (let slot = 0; slot < concurrency; slot++) {
          const start = groupStart + slot * chunkSize;
          if (start >= totalBytes) break;
          ranges.push({ start, end: Math.min(totalBytes - 1, start + chunkSize - 1) });
        }
        const parts = await Promise.all(ranges.map(range => fetchRange(range.start, range.end)));
        parts.forEach(part => { chunks.push(part); done += part.byteLength; });
        setDownloadProgress(done, "Downloading file…");
      }
      const blob = new Blob(chunks, { type: "application/octet-stream" });
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = fileName;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
      setDownloadProgress(totalBytes, "Download ready");
    }

    largeDownloadButton.addEventListener("click", async () => {
      largeDownloadButton.disabled = true;
      const originalLabel = largeDownloadButton.innerHTML;
      largeDownloadButton.innerHTML = "<span aria-hidden=\"true\">…</span> Preparing download…";
      let writable = null;
      try {
        if (totalBytes <= 0 || !shareId) throw new Error("This share has invalid download details.");
        if (typeof window.showSaveFilePicker === "function") {
          // Call the picker before awaiting any network operations; browsers require a user gesture.
          const fileHandle = await window.showSaveFilePicker({ suggestedName: fileName });
          writable = await fileHandle.createWritable();
          await downloadToFile(writable);
          writable = null;
        } else {
          await downloadWithBlob();
        }
        showToast("File download completed.");
        largeDownloadButton.innerHTML = "✓ Download complete";
      } catch (error) {
        if (writable && typeof writable.abort === "function") {
          try { await writable.abort(); } catch (_ignored) {}
        }
        showToast(error.message || "Download failed. Please retry.");
        if (statusLabel) statusLabel.textContent = "Download stopped";
        if (detail) detail.textContent = error.message || "The file was not fully downloaded.";
        largeDownloadButton.innerHTML = originalLabel;
      } finally {
        largeDownloadButton.disabled = false;
      }
    });
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
