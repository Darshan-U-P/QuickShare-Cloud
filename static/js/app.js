(() => {
  const toast = document.getElementById("toast");
  let toastTimer;

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2600);
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
        button.innerHTML = "✓ Copied";
        window.setTimeout(() => { button.innerHTML = original; }, 1800);
      }
    });
  });

  document.querySelectorAll(".flash-dismiss").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash")?.remove());
  });

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

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  }

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
    if (dropzone) dropzone.classList.add("has-file");
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
      if (dropzone) dropzone.classList.remove("has-file");
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
      submitButton.querySelector("span")?.replaceChildren(document.createTextNode("Uploading to storage…"));
      submitButton.querySelector(".btn-arrow")?.replaceChildren(document.createTextNode("…"));
    });
  }

  document.querySelectorAll("time[data-format-date]").forEach((time) => {
    const raw = time.getAttribute("datetime");
    if (!raw) return;
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return;
    time.textContent = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short"
    }).format(date);
    time.title = date.toLocaleString();
  });
})();
