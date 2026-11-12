(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const el = {
    readyBadge: $("#ready-badge"),
    tokenToggle: $("#token-toggle"),
    tokenPanel: $("#token-panel"),
    tokenInput: $("#token-input"),
    tokenSave: $("#token-save"),
    dropzone: $("#dropzone"),
    fileInput: $("#file-input"),
    filePreview: $("#file-preview"),
    fileName: $("#file-name"),
    fileSize: $("#file-size"),
    clearFile: $("#clear-file"),
    limitsHint: $("#limits-hint"),
    timestamps: $("#timestamps"),
    modelSelect: $("#model-select"),
    submit: $("#submit"),
    uploadProgress: $("#upload-progress"),
    uploadBar: $("#upload-bar"),
    uploadPercent: $("#upload-percent"),
    jobSection: $("#job-section"),
    jobStatusBadge: $("#job-status-badge"),
    jobTitle: $("#job-title"),
    jobStage: $("#job-stage"),
    jobProgressLabel: $("#job-progress-label"),
    jobEta: $("#job-eta"),
    jobProgressbar: $("#job-progressbar"),
    jobBar: $("#job-bar"),
    cancelJob: $("#cancel-job"),
    jobResult: $("#job-result"),
    downloadLink: $("#download-link"),
    newJob: $("#new-job"),
    jobPreview: $("#job-preview"),
    jobError: $("#job-error"),
    jobErrorText: $("#job-error-text"),
    retry: $("#retry"),
    historyList: $("#history-list"),
    historyEmpty: $("#history-empty"),
    refreshHistory: $("#refresh-history"),
    version: $("#version"),
    toast: $("#toast"),
  };

  const ACTIVE = new Set(["queued", "extracting", "transcribing", "rendering"]);
  const TERMINAL = new Set(["done", "failed", "canceled"]);
  const STATUS_LABELS = {
    queued: "В очереди",
    extracting: "Извлечение аудио",
    transcribing: "Распознавание",
    rendering: "Формирование DOCX",
    done: "Готово",
    failed: "Ошибка",
    canceled: "Отменено",
  };
  const STATUS_CLASS = {
    queued: "badge-neutral",
    extracting: "badge-active",
    transcribing: "badge-active",
    rendering: "badge-active",
    done: "badge-done",
    failed: "badge-error",
    canceled: "badge-canceled",
  };
  const TERMINAL_LABELS = {
    done: "Обработка завершена",
    failed: "Не удалось обработать файл",
    canceled: "Задача отменена",
  };

  const state = {
    file: null,
    job: null,
    limits: null,
    token: localStorage.getItem("gigaam_token") || "",
    eventSource: null,
    reconnectTimer: null,
    pollTimer: null,
    uploadXhr: null,
    uploading: false,
    toastTimer: null,
    readyTimer: null,
  };

  class ApiError extends Error {
    constructor(status, payload) {
      const error = payload && payload.error ? payload.error : {};
      super(error.message || "Ошибка запроса");
      this.status = status;
      this.code = error.code || "request_failed";
      this.payload = payload;
    }
  }

  function showToast(message) {
    el.toast.textContent = message;
    el.toast.hidden = false;
    requestAnimationFrame(() => el.toast.classList.add("is-visible"));
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => {
      el.toast.classList.remove("is-visible");
      setTimeout(() => { el.toast.hidden = true; }, 220);
    }, 4200);
  }

  function authHeaders(extra = {}) {
    const headers = new Headers(extra);
    if (state.token) headers.set("X-Auth-Token", state.token);
    return headers;
  }

  async function api(path, options = {}) {
    const headers = authHeaders(options.headers || {});
    if (!(options.body instanceof FormData)) {
      headers.set("Accept", "application/json");
    }
    const response = await fetch(path, { ...options, headers });
    let payload = null;
    const text = await response.text();
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = { error: { code: "bad_response", message: text } }; }
    }
    if (!response.ok) {
      if (response.status === 401) showTokenPanel(true);
      throw new ApiError(response.status, payload);
    }
    return payload;
  }

  function withToken(url) {
    if (!url || !state.token) return url;
    const separator = url.includes("?") ? "&" : "?";
    return `${url}${separator}token=${encodeURIComponent(state.token)}`;
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return "—";
    const units = ["Б", "КБ", "МБ", "ГБ"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return `${value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) return `${h} ч ${String(m).padStart(2, "0")} мин`;
    if (m > 0) return `${m} мин ${String(s).padStart(2, "0")} с`;
    return `${s} с`;
  }

  function formatEta(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "";
    if (seconds < 60) return "Осталось меньше минуты";
    return `Осталось ~${formatDuration(seconds)}`;
  }

  function formatDate(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function setBadge(node, status) {
    node.className = `badge ${STATUS_CLASS[status] || "badge-neutral"}`;
    node.textContent = STATUS_LABELS[status] || status;
  }

  function setReadyBadge(kind, text) {
    el.readyBadge.className = `badge ${kind}`;
    el.readyBadge.textContent = text;
  }

  function showTokenPanel(show = true) {
    el.tokenPanel.hidden = !show;
    el.tokenToggle.setAttribute("aria-expanded", String(show));
    if (show) {
      el.tokenInput.value = state.token;
      setTimeout(() => el.tokenInput.focus(), 30);
    }
  }

  async function loadLimits() {
    try {
      const limits = await api("/api/limits");
      state.limits = limits;
      el.version.textContent = `v${limits.app_version} · ${limits.image_tag}`;
      el.limitsHint.textContent = `До ${limits.max_upload_mb} МБ · до ${limits.max_duration_min} минут · ${limits.allowed_extensions.slice(0, 8).join(", ").toUpperCase()}…`;
      el.fileInput.accept = limits.allowed_extensions.join(",");
      el.timestamps.checked = Boolean(limits.default_timestamps);
      const available = new Set(limits.models);
      for (const option of Array.from(el.modelSelect.options)) {
        option.hidden = !available.has(option.value);
      }
      const defaultOption = Array.from(el.modelSelect.options).find((option) => option.value === limits.default_model);
      if (defaultOption) el.modelSelect.value = limits.default_model;
      if (limits.auth_required) {
        el.tokenToggle.hidden = false;
        if (!state.token) showTokenPanel(true);
      }
    } catch (error) {
      setReadyBadge("badge-error", "API недоступен");
      showToast(error.message || "Не удалось получить лимиты сервиса");
    }
  }

  async function checkReady() {
    clearTimeout(state.readyTimer);
    try {
      const response = await fetch("/api/ready", { headers: authHeaders({ Accept: "application/json" }) });
      const data = await response.json().catch(() => ({}));
      if (response.ok && data.ready) {
        setReadyBadge("badge-ready", data.device === "cuda" ? "GPU готова" : "CPU готова");
        return;
      }
      const message = data.message || "Модель загружается…";
      setReadyBadge("badge-loading", "Модель загружается…");
      el.readyBadge.title = message;
      state.readyTimer = setTimeout(checkReady, 5000);
    } catch {
      setReadyBadge("badge-error", "Сервер недоступен");
      state.readyTimer = setTimeout(checkReady, 8000);
    }
  }

  function setFile(file) {
    if (!file) {
      state.file = null;
      el.filePreview.hidden = true;
      el.fileInput.value = "";
      el.submit.disabled = true;
      return;
    }
    const limits = state.limits;
    const name = file.name || "audio";
    const extension = name.includes(".") ? `.${name.split(".").pop().toLowerCase()}` : "";
    if (limits && !limits.allowed_extensions.includes(extension)) {
      showToast(`Формат ${extension || "без расширения"} не поддерживается`);
      return;
    }
    if (limits && file.size > limits.max_upload_mb * 1024 * 1024) {
      showToast(`Файл больше ${limits.max_upload_mb} МБ`);
      return;
    }
    state.file = file;
    el.fileName.textContent = name;
    el.fileSize.textContent = `${formatBytes(file.size)}${extension ? ` · ${extension.slice(1).toUpperCase()}` : ""}`;
    el.filePreview.hidden = false;
    el.submit.disabled = state.uploading;
  }

  function resetJobView({ clearFile = false } = {}) {
    closeEvents();
    state.job = null;
    el.jobSection.hidden = true;
    el.jobResult.hidden = true;
    el.jobError.hidden = true;
    el.jobBar.style.width = "0%";
    el.jobBar.classList.remove("is-indeterminate");
    el.jobProgressbar.setAttribute("aria-valuenow", "0");
    el.jobProgressLabel.textContent = "0%";
    el.jobEta.textContent = "";
    localStorage.removeItem("gigaam_job_id");
    if (clearFile) setFile(null);
    el.submit.disabled = !state.file || state.uploading;
  }

  function closeEvents() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  function renderJob(job) {
    state.job = job;
    if (job.id) localStorage.setItem("gigaam_job_id", job.id);
    el.jobSection.hidden = false;
    setBadge(el.jobStatusBadge, job.status);
    el.jobTitle.textContent = job.filename || "Задача";
    const active = ACTIVE.has(job.status);
    el.cancelJob.hidden = !active;

    let stage = job.stage_message || STATUS_LABELS[job.status] || "";
    if (job.status === "queued" && job.queue_position) {
      stage = `Позиция в очереди: ${job.queue_position}`;
    }
    if (TERMINAL.has(job.status)) stage = TERMINAL_LABELS[job.status] || stage;
    el.jobStage.textContent = stage;

    const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
    const indeterminate = job.status === "extracting" && progress < 0.5;
    el.jobBar.classList.toggle("is-indeterminate", indeterminate);
    el.jobBar.style.width = `${progress}%`;
    el.jobProgressbar.setAttribute("aria-valuenow", String(Math.round(progress)));
    el.jobProgressLabel.textContent = job.status === "done" ? "100%" : `${Math.round(progress)}%`;
    if (job.status === "transcribing" && job.duration_s) {
      if (stage.startsWith("Поиск речи")) {
        el.jobProgressLabel.textContent = `${Math.round(progress)}%`;
      } else {
        el.jobProgressLabel.textContent = `${Math.round(progress)}% · ${formatDuration(job.processed_seconds)} / ${formatDuration(job.duration_s)}`;
      }
      el.jobEta.textContent = formatEta(job.eta_seconds);
    } else if (job.status === "queued" && job.duration_s) {
      el.jobEta.textContent = `Длительность: ${formatDuration(job.duration_s)}`;
    } else if (job.status === "rendering") {
      el.jobEta.textContent = "Собираем документ…";
    } else {
      el.jobEta.textContent = "";
    }

    const showResult = job.status === "done";
    el.jobResult.hidden = !showResult;
    if (showResult) {
      el.downloadLink.href = withToken(job.download_url) || "#";
      el.jobPreview.textContent = job.preview || "Речь не обнаружена.";
    }

    const showError = job.status === "failed" || job.status === "canceled";
    el.jobError.hidden = !showError;
    if (showError) {
      el.jobErrorText.textContent = job.error?.message || TERMINAL_LABELS[job.status] || "Задача завершилась с ошибкой";
      el.retry.textContent = state.file ? "Повторить" : "Выбрать файл";
    }
  }

  function handleJobUpdate(job) {
    renderJob(job);
    if (TERMINAL.has(job.status)) {
      closeEvents();
      refreshHistory();
    }
  }

  function handleDeletedJob(error) {
    closeEvents();
    if (state.job && state.job.id) {
      resetJobView();
    }
    refreshHistory();
    showToast(error?.message || "Задача удалена");
  }

  function startPolling(jobId) {
    clearTimeout(state.pollTimer);
    const poll = async () => {
      if (!state.job || state.job.id !== jobId || !ACTIVE.has(state.job.status)) return;
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        handleJobUpdate(job);
      } catch (error) {
        if (error.status === 404) {
          resetJobView();
          showToast("Задача удалена");
          return;
        }
      }
      if (state.job && ACTIVE.has(state.job.status)) {
        state.pollTimer = setTimeout(poll, 5000);
      }
    };
    state.pollTimer = setTimeout(poll, 5000);
  }

  function scheduleReconnect() {
    if (state.reconnectTimer) return;
    if (!state.job || !ACTIVE.has(state.job.status)) return;
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      if (!state.job) return;
      if (ACTIVE.has(state.job.status)) startEvents(state.job.id);
      else startPolling(state.job.id);
    }, 3000);
  }

  function startEvents(jobId) {
    closeEvents();
    const url = withToken(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    const source = new EventSource(url);
    state.eventSource = source;
    const consume = (event) => {
      try {
        const job = JSON.parse(event.data);
        handleJobUpdate(job);
      } catch {
        scheduleReconnect();
      }
    };
    source.addEventListener("status", consume);
    source.addEventListener("progress", consume);
    source.addEventListener("done", consume);
    source.addEventListener("error", (event) => {
      if (event.data) {
        try {
          const payload = JSON.parse(event.data);
          if (payload.error && !payload.status) {
            handleDeletedJob(payload.error);
            return;
          }
        } catch {
          // Ниже обработаем как обычное обновление задачи.
        }
        consume(event);
        return;
      }
      if (source.readyState === EventSource.CLOSED) scheduleReconnect();
    });
    source.onerror = () => {
      if (state.job && TERMINAL.has(state.job.status)) {
        source.close();
        return;
      }
      source.close();
      state.eventSource = null;
      scheduleReconnect();
    };
    if (state.job) startPolling(jobId);
  }

  async function loadJob(jobId) {
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      renderJob(job);
      if (ACTIVE.has(job.status)) startEvents(job.id);
      else closeEvents();
    } catch (error) {
      localStorage.removeItem("gigaam_job_id");
      resetJobView();
      if (error.status !== 404) showToast(error.message);
    }
  }

  function openJob(jobId) {
    localStorage.setItem("gigaam_job_id", jobId);
    loadJob(jobId);
    el.jobSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function uploadWithProgress(formData) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      state.uploadXhr = xhr;
      xhr.open("POST", "/api/jobs");
      xhr.setRequestHeader("Accept", "application/json");
      if (state.token) xhr.setRequestHeader("X-Auth-Token", state.token);
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        const percent = Math.max(1, Math.round((event.loaded / event.total) * 100));
        el.uploadBar.style.width = `${percent}%`;
        el.uploadPercent.textContent = `${percent}%`;
      };
      xhr.onload = () => {
        state.uploadXhr = null;
        let payload = null;
        try { payload = JSON.parse(xhr.responseText || "{}"); } catch { payload = {}; }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload);
        } else {
          if (xhr.status === 401) showTokenPanel(true);
          reject(new ApiError(xhr.status, payload));
        }
      };
      xhr.onerror = () => {
        state.uploadXhr = null;
        reject(new ApiError(0, { error: { code: "network_error", message: "Сеть недоступна. Проверьте соединение с сервером." } }));
      };
      xhr.onabort = () => {
        state.uploadXhr = null;
        reject(new ApiError(0, { error: { code: "aborted", message: "Загрузка отменена." } }));
      };
      xhr.send(formData);
    });
  }

  async function startUpload() {
    if (!state.file || state.uploading) {
      if (!state.file) {
        el.dropzone.focus();
        showToast("Сначала выберите файл");
      }
      return;
    }
    if (state.limits?.auth_required && !state.token) {
      showTokenPanel(true);
      showToast("Введите токен доступа");
      return;
    }
    state.uploading = true;
    closeEvents();
    state.job = null;
    localStorage.removeItem("gigaam_job_id");
    el.submit.disabled = true;
    el.dropzone.setAttribute("aria-busy", "true");
    el.uploadProgress.hidden = false;
    el.uploadBar.style.width = "0%";
    el.uploadPercent.textContent = "0%";
    el.jobSection.hidden = true;
    const formData = new FormData();
    formData.append("file", state.file, state.file.name);
    formData.append("timestamps", el.timestamps.checked ? "true" : "false");
    formData.append("model", el.modelSelect.value);
    try {
      const payload = await uploadWithProgress(formData);
      el.uploadProgress.hidden = true;
      state.uploading = false;
      state.file = null;
      el.filePreview.hidden = true;
      el.fileInput.value = "";
      el.submit.disabled = true;
      el.dropzone.removeAttribute("aria-busy");
      localStorage.setItem("gigaam_job_id", payload.job_id);
      await loadJob(payload.job_id);
      await refreshHistory();
      showToast("Файл загружен. Задача поставлена в очередь.");
    } catch (error) {
      state.uploading = false;
      el.uploadProgress.hidden = true;
      el.dropzone.removeAttribute("aria-busy");
      el.submit.disabled = !state.file;
      renderUploadFailure(error);
    }
  }

  function renderUploadFailure(error) {
    el.jobSection.hidden = false;
    setBadge(el.jobStatusBadge, "failed");
    el.jobTitle.textContent = state.file ? state.file.name : "Загрузка файла";
    el.jobStage.textContent = "Не удалось поставить задачу в очередь";
    el.jobProgressLabel.textContent = "0%";
    el.jobEta.textContent = "";
    el.jobBar.classList.remove("is-indeterminate");
    el.jobBar.style.width = "0%";
    el.jobResult.hidden = true;
    el.cancelJob.hidden = true;
    el.jobError.hidden = false;
    el.jobErrorText.textContent = error.message || "Не удалось загрузить файл";
    el.retry.textContent = state.file ? "Повторить" : "Выбрать файл";
    showToast(error.message || "Не удалось загрузить файл");
  }

  async function refreshHistory() {
    try {
      const data = await api("/api/jobs?limit=20");
      renderHistory(data.items || []);
    } catch (error) {
      if (error.status !== 401) showToast(error.message);
    }
  }

  function renderHistory(items) {
    el.historyList.textContent = "";
    el.historyEmpty.hidden = items.length > 0;
    for (const job of items) {
      const item = document.createElement("li");
      item.className = "history-item";
      if (state.job && state.job.id === job.id) item.classList.add("is-current");

      const main = document.createElement("button");
      main.type = "button";
      main.className = "history-main";
      main.setAttribute("aria-label", `Открыть задачу ${job.filename}`);
      main.addEventListener("click", () => openJob(job.id));

      const name = document.createElement("span");
      name.className = "history-name";
      name.textContent = job.filename;
      const meta = document.createElement("span");
      meta.className = "history-meta";
      const status = document.createElement("span");
      status.className = `badge ${STATUS_CLASS[job.status] || "badge-neutral"}`;
      status.textContent = STATUS_LABELS[job.status] || job.status;
      const duration = document.createElement("span");
      duration.textContent = job.duration_s ? formatDuration(job.duration_s) : "";
      const date = document.createElement("span");
      date.textContent = formatDate(job.created_at);
      meta.append(status, duration, date);
      main.append(name, meta);

      const actions = document.createElement("div");
      actions.className = "history-actions";
      if (job.status === "done" && job.download_url) {
        const download = document.createElement("a");
        download.className = "ghost small";
        download.href = withToken(job.download_url);
        download.textContent = "DOCX";
        download.setAttribute("aria-label", `Скачать DOCX для ${job.filename}`);
        actions.append(download);
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "ghost small danger";
      remove.textContent = "Удалить";
      remove.setAttribute("aria-label", `Удалить задачу ${job.filename}`);
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteJob(job.id);
      });
      actions.append(remove);

      item.append(main, actions);
      el.historyList.append(item);
    }
  }

  async function deleteJob(jobId) {
    if (!window.confirm("Удалить задачу и связанные файлы?")) return;
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
      if (state.job && state.job.id === jobId) {
        const active = ACTIVE.has(state.job.status);
        if (active) {
          el.jobStage.textContent = "Отмена задачи…";
          el.cancelJob.hidden = true;
          showToast("Задача отменяется…");
        } else {
          resetJobView();
        }
      }
      await refreshHistory();
      if (state.job && state.job.id === jobId && ACTIVE.has(state.job.status)) {
        setTimeout(() => loadJob(jobId).catch(() => {}), 1500);
      }
    } catch (error) {
      showToast(error.message);
    }
  }

  function bindEvents() {
    el.dropzone.addEventListener("click", () => el.fileInput.click());
    el.dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        el.fileInput.click();
      }
    });
    el.fileInput.addEventListener("change", () => {
      if (el.fileInput.files && el.fileInput.files[0]) setFile(el.fileInput.files[0]);
    });
    for (const eventName of ["dragenter", "dragover"]) {
      el.dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        el.dropzone.classList.add("is-drag");
      });
    }
    for (const eventName of ["dragleave", "drop"]) {
      el.dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        el.dropzone.classList.remove("is-drag");
      });
    }
    el.dropzone.addEventListener("drop", (event) => {
      const files = event.dataTransfer?.files;
      if (files && files[0]) setFile(files[0]);
    });
    el.clearFile.addEventListener("click", () => setFile(null));
    el.submit.addEventListener("click", startUpload);
    el.cancelJob.addEventListener("click", () => {
      if (state.job) deleteJob(state.job.id);
    });
    el.newJob.addEventListener("click", () => {
      resetJobView();
      setFile(null);
      el.dropzone.focus();
      clearTimeout(state.pollTimer);
    });
    el.refreshHistory.addEventListener("click", refreshHistory);
    el.retry.addEventListener("click", () => {
      if (state.file) {
        startUpload();
      } else {
        el.dropzone.scrollIntoView({ behavior: "smooth", block: "center" });
        el.dropzone.focus();
        showToast("Выберите файл ещё раз");
      }
    });
    el.tokenToggle.addEventListener("click", () => {
      showTokenPanel(el.tokenPanel.hidden);
    });
    el.tokenSave.addEventListener("click", async () => {
      state.token = el.tokenInput.value.trim();
      if (state.token) localStorage.setItem("gigaam_token", state.token);
      else localStorage.removeItem("gigaam_token");
      showTokenPanel(false);
      showToast(state.token ? "Токен сохранён" : "Токен удалён");
      await loadLimits();
      await checkReady();
      await refreshHistory();
      if (state.job && state.job.id) await loadJob(state.job.id);
    });
    window.addEventListener("beforeunload", closeEvents);
  }

  async function init() {
    bindEvents();
    if (state.token) el.tokenInput.value = state.token;
    await loadLimits();
    checkReady();
    await refreshHistory();
    const savedJob = localStorage.getItem("gigaam_job_id");
    if (savedJob) {
      await loadJob(savedJob);
    }
  }

  init();
})();
