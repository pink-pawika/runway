const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const state = {
  tab: "add",
  month: new Date().toISOString().slice(0, 7),
  categories: [],
  profile: "student",
  editor: null,
  addReady: false,
  charts: { bars: null, trend: null },
};

const STATE_COLOR = {
  calm: "#1c6b43",
  almost: "#e0b422",
  warning: "#d4651a",
  over: "#b4332a",
  no_budget: "#b7aea4",
};

const statusLine = document.querySelector("#status");

function h(tag, attrs, children) {
  const el = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([key, value]) => {
    if (key === "className") el.className = value;
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value !== false && value != null) {
      el.setAttribute(key, String(value));
      if (key === "value" && "value" in el) el.value = String(value);
    }
  });
  (children || []).forEach((child) => {
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  });
  return el;
}

function setText(el, value) {
  el.textContent = value == null ? "" : String(value);
}

function cleanMessage(message) {
  const text = String(message || "").trim();
  if (!text) return "";
  if (/traceback|site-packages|file "|^\s*at\s+\S+\s+\(|\.py", line /i.test(text)) {
    return "Something went wrong. Try again.";
  }
  return text;
}

function setStatus(message) {
  setText(statusLine, cleanMessage(message || ""));
}

function showAlert(message) {
  document.querySelectorAll("[data-alert]").forEach((banner) => {
    if (!message) {
      banner.hidden = true;
      setText(banner, "");
      return;
    }
    banner.hidden = false;
    setText(banner, message);
  });
}

function money(cents) {
  const sign = cents < 0 ? "−" : "";
  const abs = Math.abs(Number(cents) || 0);
  const dollars = Math.floor(abs / 100).toLocaleString("en-US");
  const rest = String(abs % 100).padStart(2, "0");
  return `${sign}$${dollars}.${rest}`;
}

function formatCost(value) {
  const amount = Number(value) || 0;
  let digits = 6;
  if (amount !== 0) {
    while (digits < 8 && Math.abs(amount) < 0.5 * 10 ** -digits) digits += 1;
  }
  return `$${amount.toFixed(digits)}`;
}

function uploadSummary(saved, review, usage) {
  const tokens = ((usage.input_tokens || 0) + (usage.output_tokens || 0)).toLocaleString("en-US");
  const items = saved === 1 ? "1 item" : `${saved} items`;
  const needs = review === 1 ? "1 needs review" : `${review} need review`;
  return `Found ${items}, ${needs}, used ${tokens} tokens (${formatCost(usage.cost_usd)})`;
}

function errorText(payload) {
  if (!payload) return "Something went wrong. Try again.";
  if (typeof payload.detail === "string") return cleanMessage(payload.detail) || "Something went wrong. Try again.";
  if (Array.isArray(payload.detail)) {
    const text = payload.detail.map((item) => item.msg || "Check that field.").join(" ");
    return cleanMessage(text) || "Check the form and try again.";
  }
  return "Something went wrong. Try again.";
}

function dollarsToCents(raw, allowNegative, allowZero) {
  const cleaned = String(raw || "").trim().replace(/\$/g, "").replace(/,/g, "");
  if (!cleaned) return { error: "Enter an amount." };
  const match = cleaned.match(/^(-)?(\d+)(?:\.(\d{1,2}))?$/);
  if (!match) return { error: "Use dollars and cents, like 12.50." };
  if (match[1] && !allowNegative) return { error: "Type a positive amount." };
  const cents = Number(match[2]) * 100 + Number((match[3] || "").padEnd(2, "0"));
  const signed = match[1] ? -cents : cents;
  if (signed === 0 && !allowZero) return { error: "Amount cannot be zero." };
  if (Math.abs(signed) >= 100000000) return { error: "Amount must be under $1,000,000." };
  return { cents: signed };
}

function centsToDollars(cents) {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

function shiftMonth(month, delta) {
  const [year, mon] = month.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, mon - 1 + delta, 1));
  return `${shifted.getUTCFullYear()}-${String(shifted.getUTCMonth() + 1).padStart(2, "0")}`;
}

function monthTitle(month) {
  const [year, mon] = month.split("-").map(Number);
  return `${MONTHS[mon - 1]} ${year}`;
}

function trendNotes(trend) {
  const points = (trend || []).map((point) => {
    const [year, mon] = String(point.month).split("-").map(Number);
    return {
      name: MONTHS[mon - 1] || point.month,
      year,
      cents: Number(point.spent_cents) || 0,
    };
  });
  if (!points.length) {
    return ["This chart has no months yet.", "There is no line to read."];
  }
  const years = new Set(points.map((point) => point.year));
  const label = (point) => (years.size > 1 ? `${point.name} ${point.year}` : point.name);
  const first = points[0];
  const last = points[points.length - 1];
  const peak = points.reduce((best, point) => (point.cents > best.cents ? point : best));
  const low = points.reduce((best, point) => (point.cents < best.cents ? point : best));
  if (points.every((point) => point.cents === 0)) {
    return [
      `Every month from ${label(first)} to ${label(last)} is $0.00.`,
      "The line stays flat because nothing was spent.",
    ];
  }
  let opening;
  if (last.cents > first.cents) {
    opening = `Spending rises from ${money(first.cents)} in ${label(first)} to ${money(last.cents)} in ${label(last)}.`;
  } else if (last.cents < first.cents) {
    opening = `Spending falls from ${money(first.cents)} in ${label(first)} to ${money(last.cents)} in ${label(last)}.`;
  } else {
    opening = `Spending starts and ends at ${money(first.cents)}, in ${label(first)} and ${label(last)}.`;
  }
  const lowAt = points.findIndex((point) => point === low);
  const peakAt = points.findIndex((point) => point === peak);
  let shape;
  if (peak.cents === low.cents) {
    shape = `The line stays level at ${money(peak.cents)} across these ${points.length} months.`;
  } else if (lowAt > 0 && lowAt < points.length - 1 && peakAt > lowAt) {
    shape = `It falls to ${money(low.cents)} in ${label(low)}, then rises to ${money(peak.cents)} in ${label(peak)}.`;
  } else if (peakAt > 0 && peakAt < points.length - 1 && lowAt > peakAt) {
    shape = `It rises to ${money(peak.cents)} in ${label(peak)}, then falls to ${money(low.cents)} in ${label(low)}.`;
  } else if (peakAt === points.length - 1) {
    shape = `${label(last)} is the highest month, and ${label(low)} is the lowest at ${money(low.cents)}.`;
  } else if (lowAt === points.length - 1) {
    shape = `${label(last)} is the lowest month, and ${label(peak)} is the highest at ${money(peak.cents)}.`;
  } else {
    shape = `The high is ${label(peak)} at ${money(peak.cents)}. The low is ${label(low)} at ${money(low.cents)}.`;
  }
  return [opening, shape];
}

function prettyDate(iso) {
  if (!iso) return "No date";
  const [, month, day] = iso.split("-");
  return `${SHORT[Number(month) - 1]} ${Number(day)}`;
}

async function api(url, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.body) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(url, { ...options, headers });
  } catch (error) {
    throw new Error("The app could not be reached. Check that it is running.");
  }
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = errorText(payload);
    if (response.status === 429) showAlert(message);
    throw new Error(message);
  }
  return payload;
}

function panel(name) {
  return document.querySelector(`#panel-${name}`);
}

function categorySelect(selected) {
  const names = state.categories.slice();
  if (selected && !names.includes(selected)) names.unshift(selected);
  const select = h("select", { name: "category" });
  names.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    setText(option, name);
    if (name === selected) option.selected = true;
    select.append(option);
  });
  if (selected) select.value = selected;
  return select;
}

function knownDate(item) {
  if (item.date) return String(item.date).slice(0, 10);
  const created = String(item.created_at || "").slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(created) ? created : "";
}

function editorHeading(title) {
  const heading = h("h2");
  setText(heading, title);
  const cancel = h("button", {
    type: "button",
    className: "secondary editor-cancel",
    onClick: (event) => {
      event.preventDefault();
      closeEditor();
    },
  }, ["Cancel"]);
  return h("div", { className: "editor-head" }, [heading, cancel]);
}

function revealEditor(card) {
  card.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    closeEditor();
  });
  requestAnimationFrame(() => card.scrollIntoView({ block: "center" }));
}

function dateLine(item) {
  const wrap = h("span", { className: "row-date" });
  const day = document.createElement("span");
  setText(day, prettyDate(item.date || String(item.created_at || "").slice(0, 10)));
  wrap.append(day);
  return wrap;
}

function moneyNode(cents) {
  const node = h("span", { className: cents < 0 ? "money money-in" : "money" });
  setText(node, money(cents));
  return node;
}

function monthBar(onShift) {
  const bar = h("div", { className: "month-bar" });
  const label = h("p", { className: "month-label" });
  setText(label, monthTitle(state.month));
  bar.append(
    h("button", { type: "button", className: "ghost", onClick: () => onShift(-1) }, ["Back"]),
    label,
    h("button", { type: "button", className: "ghost", onClick: () => onShift(1) }, ["Next"]),
  );
  return bar;
}

function penIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M4 16.5V20h3.5L18.8 8.7l-3.5-3.5L4 16.5zM16.4 3.8l3.5 3.5");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.8");
  path.setAttribute("stroke-linejoin", "round");
  path.setAttribute("stroke-linecap", "round");
  svg.append(path);
  return svg;
}

function editButton(onClick) {
  return h("button", {
    type: "button",
    className: "edit-activity",
    "aria-label": "Edit",
    onClick,
  }, [penIcon()]);
}

function activityRow(item) {
  const card = h("article", { className: "row" });
  const merchant = h("span", { className: "row-merchant" });
  setText(merchant, item.merchant || "Missing name");
  const meta = h("span", { className: "row-meta" });
  const category = h("span", { className: "pill" });
  setText(category, item.category);
  meta.append(category);
  if (item.status === "review") {
    const flag = h("span", { className: "pill pill-review" });
    setText(flag, "Review");
    meta.append(document.createTextNode(" "));
    meta.append(flag);
  }
  card.append(dateLine(item), merchant, meta, moneyNode(item.amount_cents), editButton(() => openEditor(item, "add")));
  return card;
}

function openEditor(item, source) {
  state.editor = { id: item.id, source };
  if (source === "review") renderReview();
  else renderActivity();
}

function closeEditor() {
  const source = state.editor && state.editor.source;
  state.editor = null;
  if (source === "review") renderReview();
  else renderActivity();
}

function editorCard(item, source) {
  const card = h("form", { className: "card editor" });
  const shownDate = knownDate(item);
  const date = h("input", { type: "date", name: "date", value: shownDate });
  const merchant = h("input", { name: "merchant", maxlength: "80", value: item.merchant || "", autocomplete: "off" });
  const amount = h("input", { name: "amount", inputmode: "decimal", value: centsToDollars(item.amount_cents) });
  const category = categorySelect(item.category);
  let armedDelete = false;
  const remove = h("button", { type: "button", className: "danger" }, ["Delete"]);
  remove.addEventListener("click", async () => {
    if (!armedDelete) {
      armedDelete = true;
      setText(remove, "Delete this transaction?");
      return;
    }
    try {
      await api(`/api/transactions/${item.id}`, { method: "DELETE" });
      state.editor = null;
      setStatus("Transaction deleted.");
      await loadReviewCount();
      if (source === "review") await renderReview();
      else await renderActivity();
    } catch (error) {
      setStatus(error.message);
    }
  });
  card.append(
    editorHeading("Edit activity"),
    h("div", { className: "editor-grid" }, [
      h("label", {}, ["Date", date]),
      h("label", {}, ["Amount", amount]),
      h("label", { className: "editor-span" }, ["Name", merchant]),
      h("label", { className: "editor-span" }, ["Category", category]),
    ]),
    h("div", { className: "button-row" }, [
      h("button", { type: "submit", className: "primary" }, ["Save"]),
      h("button", { type: "button", className: "secondary", onClick: (event) => { event.preventDefault(); closeEditor(); } }, ["Cancel"]),
      remove,
    ]),
  );
  revealEditor(card);
  card.addEventListener("submit", async (event) => {
    event.preventDefault();
    const parsed = dollarsToCents(amount.value, true);
    if (parsed.error) {
      setStatus(parsed.error);
      return;
    }
    const body = {};
    const nextMerchant = merchant.value.trim().replace(/\s+/g, " ");
    if (nextMerchant !== item.merchant) body.merchant = nextMerchant;
    if (parsed.cents !== item.amount_cents) body.amount_cents = parsed.cents;
    const nextDate = date.value || null;
    const originalDate = item.date || null;
    if (nextDate !== originalDate && !(originalDate == null && nextDate === shownDate)) body.date = nextDate;
    if (category.value !== item.category) body.category = category.value;
    if (!Object.keys(body).length) {
      setStatus("No changes to save.");
      return;
    }
    try {
      await api(`/api/transactions/${item.id}`, { method: "PATCH", body: JSON.stringify(body) });
      state.editor = null;
      setStatus(body.category ? "Saved. Future uploads will reuse this category." : "Saved.");
      await loadReviewCount();
      if (source === "review") await renderReview();
      else await renderActivity();
    } catch (error) {
      setStatus(error.message);
    }
  });
  return card;
}

async function renderAdd() {
  const root = panel("add");
  root.replaceChildren();
  const fileLibrary = h("input", {
    className: "file-input",
    type: "file",
    accept: "application/pdf,image/jpeg,image/png,image/webp,image/heic,image/heif,.pdf,.jpg,.jpeg,.png,.webp,.heic,.heif",
    multiple: "multiple",
  });
  const fileCamera = h("input", {
    className: "file-input",
    type: "file",
    accept: "image/*",
    capture: "environment",
  });
  fileLibrary.addEventListener("change", () => sendFile(fileLibrary));
  fileCamera.addEventListener("change", () => sendFile(fileCamera));
  const summary = h("div", { className: "card summary", hidden: "hidden" });
  const manual = h("form", { className: "card form manual" });
  const date = h("input", { type: "date", name: "date" });
  const merchant = h("input", { name: "merchant", maxlength: "80", required: "true", autocomplete: "off" });
  const amount = h("input", { name: "amount", inputmode: "decimal", required: "true", placeholder: "12.50" });
  const category = categorySelect(state.categories.includes("Other") ? "Other" : state.categories[0]);
  const save = h("button", { type: "submit", className: "primary" }, ["Save"]);
  const title = h("h2", { className: "manual-title" });
  setText(title, "Or type it");
  manual.append(
    title,
    h("div", { className: "manual-grid" }, [
      h("label", {}, ["Date, optional", date]),
      h("label", {}, ["Amount", amount]),
      h("label", { className: "manual-span" }, ["Merchant", merchant]),
      h("label", {}, ["Category", category]),
      save,
    ]),
  );
  manual.addEventListener("submit", async (event) => {
    event.preventDefault();
    const parsed = dollarsToCents(amount.value, false);
    if (parsed.error) {
      setStatus(parsed.error);
      return;
    }
    save.disabled = true;
    setStatus("Saving…");
    try {
      const body = {
        merchant: merchant.value,
        amount_cents: parsed.cents,
        category: category.value,
      };
      if (date.value) body.date = date.value;
      const saved = await api("/api/transactions", { method: "POST", body: JSON.stringify(body) });
      merchant.value = "";
      amount.value = "";
      date.value = "";
      setStatus(`Saved ${saved.merchant} for ${money(saved.amount_cents)}.`);
      await loadReviewCount();
      await renderActivity();
    } catch (error) {
      setStatus(error.message);
    } finally {
      save.disabled = false;
    }
  });
  const budgetInput = h("input", {
    inputmode: "decimal",
    placeholder: "No budget",
    "aria-label": "Monthly budget",
  });
  const budgetSave = h("button", { type: "button", className: "secondary" }, ["Save"]);
  budgetSave.addEventListener("click", () => saveBudget(budgetInput));
  const strip = h("div", { className: "add-strip" });
  const activity = h("div", { className: "activity", id: "activity" });
  strip.append(
    h("section", { className: "card hero" }, [
      h("label", { className: "budget-line" }, ["Monthly budget", budgetInput, budgetSave]),
      h("div", { className: "actions" }, [
        h("label", { className: "big" }, ["Choose files", fileLibrary]),
        h("label", { className: "big secondary" }, ["Take photo", fileCamera]),
      ]),
    ]),
    summary,
    manual,
  );
  root.append(strip, activity);
  loadBudget(budgetInput);
  renderActivity();

  function uploadStep(stateName, label) {
    const mark = stateName === "active"
      ? h("span", { className: "spinner", "aria-hidden": "true" })
      : h("span", { className: "step-mark", "aria-hidden": "true" }, stateName === "done" ? ["✓"] : []);
    const text = h("span");
    setText(text, label);
    return h("li", { className: `step ${stateName}` }, [mark, text]);
  }

  function savedSentence(payload) {
    const rows = payload.transactions || [];
    const skipped = Number(payload.received_skipped) || 0;
    const aside = skipped ? ` Left out ${skipped} received.` : "";
    if (rows.length > 1) return `Saved ${rows.length} lines.${aside}`;
    const row = rows[0];
    if (row) return `Saved ${row.merchant || "the receipt"} for ${money(row.amount_cents)}.${aside}`;
    return `${uploadSummary(payload.saved, payload.review_count, payload.usage)}${aside}`;
  }

  function renderBatch(rows, reviewCount) {
    const busy = rows.some((row) => row.phase === "uploading" || row.phase === "processing");
    summary.className = "card summary";
    summary.hidden = false;
    summary.setAttribute("aria-busy", busy ? "true" : "false");
    const finished = rows.filter((row) => row.phase === "done" || row.phase === "failed").length;
    const heading = h("h2");
    setText(heading, rows.length === 1 ? rows[0].name : `${finished} of ${rows.length}`);
    const list = h("ol", { className: "batch" });
    rows.forEach((row) => {
      const item = h("li", { className: `batch-row ${row.phase}` });
      const name = h("p", { className: "file-name" });
      setText(name, row.name);
      item.append(name);
      if (row.phase === "waiting") {
        const waiting = h("p", { className: "quiet" });
        setText(waiting, "Waiting");
        item.append(waiting);
      } else if (row.phase === "uploading") {
        item.append(uploadStep("active", "Uploading…"));
      } else if (row.phase === "processing") {
        item.append(uploadStep("done", "Download completed"));
        item.append(uploadStep("active", "Processing the data"));
      } else if (row.phase === "done") {
        item.append(uploadStep("done", "Download completed"));
        item.append(uploadStep("done", "Processing the data"));
        const line = h("p");
        setText(line, row.message);
        item.append(line);
      } else {
        const line = h("p");
        setText(line, `Not uploaded. ${row.message}`);
        item.append(line);
      }
      list.append(item);
    });
    summary.replaceChildren(heading, list);
    if (!busy && reviewCount > 0) {
      summary.append(h("button", {
        type: "button",
        className: "secondary",
        onClick: () => showTab("review"),
      }, ["Open the review queue"]));
    }
  }

  function showUploadFailed(name, message) {
    summary.className = "card summary upload-failed";
    summary.hidden = false;
    summary.setAttribute("aria-busy", "false");
    const heading = h("h2");
    setText(heading, "Not uploaded");
    const fileLine = h("p", { className: "file-name" });
    setText(fileLine, name);
    const line = h("p");
    setText(line, message);
    summary.replaceChildren(heading, fileLine, line);
    setStatus(message);
  }

  function postUpload(file, onDownloaded) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const body = new FormData();
      body.append("file", file);
      let downloaded = false;
      const markDownloaded = () => {
        if (downloaded) return;
        downloaded = true;
        onDownloaded();
      };
      xhr.open("POST", "/api/upload");
      xhr.responseType = "json";
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && event.loaded >= event.total) markDownloaded();
      });
      xhr.upload.addEventListener("load", markDownloaded);
      xhr.addEventListener("error", () => reject(new Error("The file did not upload. Try again.")));
      xhr.addEventListener("load", () => {
        markDownloaded();
        const payload = xhr.response && typeof xhr.response === "object" ? xhr.response : {};
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new Error(errorText(payload)));
          return;
        }
        resolve(payload);
      });
      xhr.send(body);
    });
  }

  async function sendFile(input) {
    const files = Array.from(input.files || []);
    input.value = "";
    if (!files.length) return;
    if (files.length > 10) {
      showUploadFailed("Too many files", "Choose up to 10 photos at a time.");
      return;
    }
    const rows = files.map((file) => ({
      name: file.name || "photo",
      phase: "waiting",
      message: "",
    }));
    fileLibrary.disabled = true;
    fileCamera.disabled = true;
    let reviewCount = 0;
    renderBatch(rows, 0);
    try {
      for (let index = 0; index < files.length; index += 1) {
        const row = rows[index];
        row.phase = "uploading";
        renderBatch(rows, 0);
        setStatus(files.length === 1 ? `Uploading ${row.name}` : `Uploading ${index + 1} of ${files.length}`);
        try {
          const payload = await postUpload(files[index], () => {
            row.phase = "processing";
            renderBatch(rows, 0);
            setStatus(files.length === 1
              ? "Download completed. Processing the data…"
              : `Processing ${index + 1} of ${files.length}`);
          });
          row.phase = "done";
          row.message = savedSentence(payload);
          reviewCount += payload.review_count || 0;
        } catch (error) {
          row.phase = "failed";
          row.message = error.message;
        }
        renderBatch(rows, 0);
      }
      renderBatch(rows, reviewCount);
      await loadReviewCount();
      await renderActivity();
      await loadUsage();
      const failed = rows.filter((row) => row.phase === "failed").length;
      const saved = rows.length - failed;
      if (rows.length === 1) {
        setStatus(failed ? rows[0].message : `Uploaded ${rows[0].name}. ${rows[0].message}`);
      } else if (failed) {
        setStatus(`Finished ${saved} of ${rows.length}. ${failed} did not upload.`);
      } else {
        setStatus(`Uploaded ${rows.length} photos.`);
      }
    } finally {
      fileLibrary.disabled = false;
      fileCamera.disabled = false;
    }
  }
}

async function loadBudget(input) {
  try {
    state.settings = await api("/api/settings");
    if (state.settings.monthly_limit_cents != null) {
      input.value = centsToDollars(state.settings.monthly_limit_cents);
    }
  } catch (error) {
    setStatus(error.message);
  }
}

async function saveBudget(input) {
  let cents = null;
  if (input.value.trim()) {
    const parsed = dollarsToCents(input.value, false, true);
    if (parsed.error) {
      setStatus(parsed.error);
      return;
    }
    cents = parsed.cents;
  }
  try {
    const current = state.settings || await api("/api/settings");
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        profile: current.profile,
        academic_year_months: current.academic_year_months,
        custom_categories: current.custom_categories,
        monthly_limit_cents: cents,
      }),
    });
    setStatus(cents == null ? "Monthly budget cleared." : `Monthly budget is ${money(cents)}.`);
  } catch (error) {
    setStatus(error.message);
  }
}

async function renderActivity() {
  const root = document.querySelector("#activity");
  if (!root) return;
  root.replaceChildren();
  root.append(monthBar(async (delta) => {
    state.month = shiftMonth(state.month, delta);
    state.editor = null;
    await renderActivity();
  }));
  let data;
  try {
    data = await api(`/api/transactions?month=${state.month}`);
  } catch (error) {
    setStatus(error.message);
    return;
  }
  const recurringTotal = (data.recurring || []).reduce((sum, row) => sum + Number(row.amount_cents || 0), 0);
  const total = h("p", { className: "total-line" });
  const label = document.createElement("span");
  setText(label, "Spent");
  const totalAmount = moneyNode(data.confirmed_total_cents + recurringTotal);
  totalAmount.classList.add("total-number");
  total.append(label, totalAmount);
  root.append(total);
  if (data.held_count) {
    const note = h("p", { className: "quiet" });
    setText(note, "Review items are listed here but are not in this spent total.");
    root.append(note);
  }
  if (state.editor && state.editor.source === "add") {
    const fresh = data.transactions.find((row) => row.id === state.editor.id);
    if (!fresh) state.editor = null;
  }
  if (state.editor && state.editor.source === "recurring") {
    const fresh = (data.recurring || []).find((row) => row.id === state.editor.id);
    if (!fresh) state.editor = null;
  }
  (data.recurring || []).forEach((row) => {
    if (state.editor && state.editor.source === "recurring" && state.editor.id === row.id) {
      root.append(recurringEditor(row));
    } else {
      root.append(recurringCard(row));
    }
  });
  if (!data.transactions.length && !(data.recurring || []).length) {
    const empty = h("p", { className: "empty card" });
    setText(empty, `Nothing in ${monthTitle(state.month)} yet.`);
    root.append(empty);
    return;
  }
  data.transactions.forEach((item) => {
    if (state.editor && state.editor.source === "add" && state.editor.id === item.id) {
      root.append(editorCard(item, "add"));
    } else {
      root.append(activityRow(item));
    }
  });
}

function recurringCard(row) {
  const card = h("article", { className: "row" });
  const merchant = h("span", { className: "row-merchant" });
  setText(merchant, row.merchant);
  const meta = h("span", { className: "row-meta" });
  const category = h("span", { className: "pill" });
  setText(category, row.category);
  const flag = h("span", { className: "pill" });
  setText(flag, "Recurring");
  meta.append(category, document.createTextNode(" "), flag);
  card.append(
    h("span", { className: "row-date" }, ["Every month"]),
    merchant,
    meta,
    moneyNode(row.amount_cents),
    editButton(() => openEditor(row, "recurring")),
  );
  return card;
}

function recurringEditor(row) {
  const card = h("form", { className: "card editor" });
  const merchant = h("input", { name: "merchant", maxlength: "80", value: row.merchant || "", autocomplete: "off" });
  const amount = h("input", { name: "amount", inputmode: "decimal", value: centsToDollars(row.amount_cents) });
  const category = categorySelect(row.category);
  card.append(
    editorHeading("Edit recurring"),
    h("div", { className: "editor-grid" }, [
      h("label", {}, ["Name", merchant]),
      h("label", {}, ["Amount", amount]),
      h("label", { className: "editor-span" }, ["Category", category]),
    ]),
    h("div", { className: "button-row" }, [
      h("button", { type: "submit", className: "primary" }, ["Save"]),
      h("button", { type: "button", className: "secondary", onClick: (event) => { event.preventDefault(); closeEditor(); } }, ["Cancel"]),
    ]),
  );
  revealEditor(card);
  card.addEventListener("submit", async (event) => {
    event.preventDefault();
    const parsed = dollarsToCents(amount.value, false, true);
    if (parsed.error) {
      setStatus(parsed.error);
      return;
    }
    const nextMerchant = merchant.value.trim().replace(/\s+/g, " ");
    const body = {};
    if (nextMerchant !== row.merchant) body.merchant = nextMerchant;
    if (parsed.cents !== row.amount_cents) body.amount_cents = parsed.cents;
    if (category.value !== row.category) body.category = category.value;
    if (!Object.keys(body).length) {
      setStatus("No changes to save.");
      return;
    }
    try {
      await api(`/api/recurring/${row.id}`, { method: "PATCH", body: JSON.stringify(body) });
      state.editor = null;
      setStatus("Saved.");
      await renderActivity();
    } catch (error) {
      setStatus(error.message);
    }
  });
  return card;
}

function reasonList(reason) {
  const wrap = h("div", { className: "review-reasons" });
  String(reason || "Needs a look.").split("; ").forEach((part) => {
    const line = h("p", { className: "reason" });
    setText(line, part);
    wrap.append(line);
  });
  return wrap;
}

async function approveOne(id) {
  await api(`/api/review/${id}/approve`, { method: "POST" });
  state.editor = null;
  setStatus("Approved. The next upload of this merchant can skip the model.");
  await loadReviewCount();
  await renderReview();
}

function sourceThumb(batchId) {
  const thumb = h("img", {
    className: "source-thumb",
    alt: "Original photo or statement",
    src: `/api/uploads/${batchId}/file`,
  });
  thumb.addEventListener("click", () => thumb.classList.toggle("source-open"));
  return thumb;
}

function approveAllButton(batch) {
  let armed = false;
  const all = h("button", { type: "button", className: "primary batch-approve" }, ["Approve all"]);
  all.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      setText(all, `Approve all ${batch.items.length}?`);
      return;
    }
    all.disabled = true;
    try {
      for (const item of batch.items) {
        await api(`/api/review/${item.id}/approve`, { method: "POST" });
      }
      state.editor = null;
      setStatus("Batch approved.");
      await loadReviewCount();
      await renderReview();
    } catch (error) {
      setStatus(error.message);
      all.disabled = false;
    }
  });
  return all;
}

async function renderReview() {
  const root = panel("review");
  root.replaceChildren();
  let data;
  try {
    data = await loadReviewCount();
  } catch (error) {
    setStatus(error.message);
    return;
  }
  if (!data.batches.length) {
    const empty = h("p", { className: "empty card" });
    setText(empty, "Nothing is waiting. Confirmed spending is already in the activity list.");
    root.append(empty);
    return;
  }
  data.batches.forEach((batch) => {
    const card = h("section", { className: batch.items.length > 1 ? "card review-batch" : "review-batch" });
    if (batch.items.length > 1) {
      const head = h("div", { className: "batch-head" });
      if (batch.has_file && batch.batch_id) head.append(sourceThumb(batch.batch_id));
      const heading = h("h2");
      setText(heading, `${batch.items.length} items`);
      head.append(heading);
      if (batch.can_approve_all) head.append(approveAllButton(batch));
      card.append(head);
    }
    batch.items.forEach((item) => {
      if (state.editor && state.editor.source === "review" && state.editor.id === item.id) {
        card.append(editorCard(item, "review"));
        return;
      }
      const block = h("article", { className: "review-item" });
      if (batch.items.length === 1 && batch.has_file && batch.batch_id) {
        block.append(sourceThumb(batch.batch_id));
      }
      const main = h("div", { className: "review-main" });
      const merchant = h("p", { className: "row-merchant" });
      setText(merchant, item.merchant || "Missing merchant");
      const meta = h("p", { className: "quiet" });
      setText(meta, item.category);
      main.append(
        dateLine(item),
        merchant,
        meta,
        moneyNode(item.amount_cents),
        editButton(() => openEditor(item, "review")),
      );
      block.append(main, reasonList(item.review_reason));
      block.append(h("div", { className: "button-row" }, [
        h("button", { type: "button", className: "primary", onClick: () => approveOne(item.id) }, ["Approve"]),
        h("button", {
          type: "button",
          className: "danger",
          onClick: async (event) => {
            const button = event.currentTarget;
            if (button.dataset.armed !== "yes") {
              button.dataset.armed = "yes";
              setText(button, "Delete this transaction?");
              return;
            }
            try {
              await api(`/api/transactions/${item.id}`, { method: "DELETE" });
              setStatus("Deleted.");
              await loadReviewCount();
              await renderReview();
            } catch (error) {
              setStatus(error.message);
            }
          },
        }, ["Delete"]),
      ]));
      card.append(block);
    });
    root.append(card);
  });
}

function destroyCharts() {
  Object.values(state.charts).forEach((chart) => {
    if (chart) chart.destroy();
  });
  state.charts = { bars: null, trend: null };
}

function percentWidth(row) {
  if (row.percent == null) return "0%";
  return `${Math.min(Math.max(row.percent, 0), 100)}%`;
}

function budgetCard(row, heading) {
  const card = h("article", { className: `card budget-card state-${row.state}` });
  const top = h("div", { className: "total-line" });
  const name = h("h2", { className: "card-title" });
  setText(name, heading);
  const titleSide = h("div", { className: "title-side" });
  if (row.projection && row.projection.at_risk) {
    const badge = h("span", { className: "risk-badge" });
    setText(badge, "At risk");
    titleSide.append(badge);
  }
  titleSide.append(stateIcon(row.state));
  top.append(name, titleSide);
  const figures = h("p", { className: "total-line" });
  const spent = h("span", { className: "total-number" });
  setText(spent, money(row.spent_cents));
  const rest = h("span", { className: "quiet" });
  setText(rest, row.limit_cents == null ? "No limit" : `of ${money(row.limit_cents)}`);
  figures.append(spent, rest);
  const meter = h("div", { className: "meter" });
  const fill = h("div", { className: "meter-fill" });
  fill.style.width = "0%";
  meter.append(fill);
  const label = h("p", { className: "state-label" });
  const shown = row.percent == null ? "" : `${row.percent}% · `;
  setText(label, `${shown}${row.label}`);
  card.append(top, figures, meter, label);
  if (row.projection) card.append(explainMath(row.projection));
  requestAnimationFrame(() => {
    fill.style.width = percentWidth(row);
  });
  return card;
}

function explainMath(projection) {
  const details = h("details", { className: "math" });
  const summary = h("summary");
  setText(summary, "Explain the math");
  details.append(summary);
  (projection.explain || []).forEach((line) => {
    const row = h("p", { className: "quiet" });
    setText(row, `${line.label}: ${line.value}`);
    details.append(row);
  });
  return details;
}

function alertList(alerts) {
  if (!alerts || !alerts.length) return null;
  const card = h("section", { className: "card alerts" });
  const title = h("h2", { className: "card-title" });
  setText(title, "At risk");
  card.append(title);
  alerts.forEach((item) => {
    const line = h("p", { className: "alert-line" });
    setText(line, item.message);
    card.append(line, explainMath(item));
  });
  return card;
}

function stateIcon(stateName) {
  const icon = h("span", { className: "state-icon", "aria-hidden": "true" });
  if (stateName === "calm") setText(icon, "✓");
  else if (stateName === "warning" || stateName === "over") setText(icon, "!");
  return icon;
}

function drawCharts(data) {
  if (typeof Chart !== "function") return;
  const spent = data.categories.filter((row) => row.spent_cents !== 0);
  const barCanvas = document.querySelector("#chart-bars");
  const trendCanvas = document.querySelector("#chart-trend");
  if (barCanvas && !spent.length) {
    const note = h("p", { className: "quiet" });
    setText(note, "No spending in this month yet.");
    barCanvas.replaceWith(note);
  } else if (barCanvas && spent.length) {
    state.charts.bars = new Chart(barCanvas, {
      type: "bar",
      data: {
        labels: spent.map((row) => row.category),
        datasets: [{
          label: "Spent",
          data: spent.map((row) => row.spent_cents / 100),
          backgroundColor: "#8b7cff",
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#b7c0d4" }, grid: { color: "rgba(186, 198, 255, 0.12)" } },
          y: {
            ticks: { color: "#b7c0d4", callback: (value) => `$${value}` },
            grid: { color: "rgba(186, 198, 255, 0.12)" },
          },
        },
      },
    });
  }
  if (trendCanvas) {
    state.charts.trend = new Chart(trendCanvas, {
      type: "line",
      data: {
        labels: data.trend.map((point) => monthTitle(point.month)),
        datasets: [{
          label: "Total spending",
          data: data.trend.map((point) => point.spent_cents / 100),
          borderColor: "#e7c27a",
          backgroundColor: "rgba(231, 194, 122, 0.16)",
          fill: true,
          tension: 0.25,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#b7c0d4" }, grid: { color: "rgba(186, 198, 255, 0.12)" } },
          y: {
            ticks: { color: "#b7c0d4", callback: (value) => `$${value}` },
            grid: { color: "rgba(186, 198, 255, 0.12)" },
          },
        },
      },
    });
  }
}

async function renderDashboard() {
  destroyCharts();
  const root = panel("dashboard");
  root.replaceChildren();
  let data;
  try {
    data = await api(`/api/summary?month=${state.month}`);
  } catch (error) {
    setStatus(error.message);
    return;
  }
  const months = data.months.length ? data.months : [data.month];
  const index = Math.max(0, months.indexOf(data.month));
  const bar = h("div", { className: "month-bar compact" });
  const select = h("select", { "aria-label": "Month" });
  months.forEach((month) => {
    const option = document.createElement("option");
    option.value = month;
    setText(option, monthTitle(month));
    if (month === data.month) option.selected = true;
    select.append(option);
  });
  select.addEventListener("change", () => {
    state.month = select.value;
    renderDashboard();
  });
  bar.append(
    h("button", {
      type: "button",
      className: "ghost month-nav",
      disabled: index <= 0 ? "true" : false,
      onClick: () => {
        state.month = months[index - 1];
        renderDashboard();
      },
    }, ["Back"]),
    select,
    h("button", {
      type: "button",
      className: "ghost month-nav",
      disabled: index >= months.length - 1 ? "true" : false,
      onClick: () => {
        state.month = months[index + 1];
        renderDashboard();
      },
    }, ["Next"]),
  );
  root.append(bar);
  const blurb = h("p", { className: "blurb card" });
  setText(blurb, data.blurb || "");
  root.append(blurb);
  const alerts = alertList(data.alerts);
  if (alerts) root.append(alerts);
  root.append(budgetCard({
    state: data.state,
    spent_cents: data.spent_cents,
    limit_cents: data.budget_cents,
    percent: data.percent,
    label: data.label,
  }, "This month"));
  const chartCard = h("section", { className: "card" });
  const chartTitle = h("h2", { className: "card-title" });
  setText(chartTitle, "Spending by category");
  const barBox = h("div", { className: "chart-box" });
  barBox.append(h("canvas", { id: "chart-bars" }));
  const trendTitle = h("h2", { className: "card-title" });
  setText(trendTitle, "Six-month trend");
  const notes = trendNotes(data.trend);
  const trendOne = h("p", { className: "quiet chart-note" });
  setText(trendOne, notes[0]);
  const trendTwo = h("p", { className: "quiet chart-note" });
  setText(trendTwo, notes[1]);
  const trendBox = h("div", { className: "chart-box" });
  trendBox.append(h("canvas", { id: "chart-trend" }));
  chartCard.append(chartTitle, barBox, trendTitle, trendOne, trendTwo, trendBox);
  root.append(chartCard);
  drawCharts(data);
}

function jobStep(phase, label) {
  const mark = phase === "active"
    ? h("span", { className: "spinner", "aria-hidden": "true" })
    : h("span", { className: "step-mark", "aria-hidden": "true" }, phase === "done" ? ["✓"] : []);
  const text = h("span");
  setText(text, label);
  return h("li", { className: `step ${phase}` }, [mark, text]);
}

function jobCard(progress) {
  if (!progress) return null;
  const failed = progress.phase === "failed";
  const card = h("section", {
    className: failed ? "card summary upload-failed" : "card summary",
    "aria-busy": progress.phase === "processing" ? "true" : "false",
  });
  const list = h("ol", { className: "steps" });
  if (progress.phase === "processing") list.append(jobStep("active", "Processing"));
  else if (progress.phase === "done") list.append(jobStep("done", "Done"));
  else list.append(jobStep("active", "Not completed"));
  const detail = h("p");
  setText(detail, progress.detail || "");
  card.append(list, detail);
  return card;
}

function emptyI20() {
  return {
    banner: "",
    months: 9,
    tuitionMonths: ["", ""],
    percents: null,
    plan: null,
    rentEdited: false,
    rentCents: null,
    busy: false,
    progress: null,
    values: {
      tuition_and_fees_cents: 0,
      living_expenses_cents: 0,
      dependent_expenses_cents: 0,
      other_expenses_cents: 0,
      funding_sources: [],
      confidence: 0,
    },
  };
}

function renderI20() {
  const root = panel("i20");
  root.replaceChildren();
  if (!state.i20) state.i20 = emptyI20();
  const draft = state.i20;
  const advice = h("p", { className: "advice" });
  setText(advice, "Only living expenses are used for this plan. This is not financial or immigration advice.");
  root.append(advice);
  const progress = jobCard(draft.progress);
  if (progress) root.append(progress);
  if (draft.plan) {
    const plan = planCard(draft);
    plan.id = "i20-plan";
    root.append(plan);
  }

  const upload = h("section", { className: "card" });
  const uploadTitle = h("h2", { className: "card-title" });
  setText(uploadTitle, "Financial section");
  const uploadCopy = h("p", { className: "quiet" });
  setText(uploadCopy, "PDF or photo. Only the yearly living expenses are used. The file is not kept.");
  const file = h("input", {
    className: "file-input",
    type: "file",
    accept: "application/pdf,image/jpeg,image/png,image/webp,image/heic,image/heif,.pdf,.jpg,.jpeg,.png,.webp,.heic,.heif",
  });
  file.disabled = !!draft.busy;
  file.addEventListener("change", () => uploadI20(file));
  upload.append(
    uploadTitle,
    uploadCopy,
    h("label", { className: "big" }, ["Choose I-20", file]),
  );
  root.append(upload);

  if (draft.banner) {
    const banner = h("p", { className: "banner" });
    setText(banner, draft.banner);
    root.append(banner);
  }

  const form = h("form", { className: "card form" });
  const confirmTitle = h("h2", { className: "card-title" });
  setText(confirmTitle, "Living expenses");
  const confirmCopy = h("p", { className: "quiet" });
  setText(confirmCopy, "This yearly amount becomes the monthly plan.");
  const livingInput = h("input", {
    className: "living-amount",
    inputmode: "decimal",
    "aria-label": "Living expenses, yearly",
    value: centsToDollars(draft.values.living_expenses_cents || 0),
  });
  form.append(confirmTitle, confirmCopy, h("label", {}, ["Living expenses, yearly", livingInput]));
  const formError = h("p", { className: "banner" });
  formError.hidden = true;
  form.append(formError);
  form.append(h("button", {
    type: "submit",
    className: "primary",
    disabled: draft.busy ? "true" : false,
  }, ["Confirm living expenses"]));
  function rejectForm(message) {
    formError.hidden = false;
    setText(formError, message);
    setStatus(message);
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    formError.hidden = true;
    setText(formError, "");
    const parsed = dollarsToCents(livingInput.value, false, true);
    if (parsed.error) {
      rejectForm(`Living expenses: ${parsed.error}`);
      return;
    }
    draft.values = Object.assign({}, draft.values, {
      living_expenses_cents: parsed.cents,
    });
    draft.plan = null;
    await refreshPlan();
  });
  root.append(form);
  const shown = root.querySelector("#i20-plan");
  if (shown) shown.scrollIntoView({ block: "start" });

  async function refreshPlan() {
    draft.busy = true;
    draft.progress = { phase: "processing", detail: "Building the monthly plan" };
    setStatus("Processing");
    renderI20();
    try {
      draft.plan = await api("/api/i20/plan", {
        method: "POST",
        body: JSON.stringify({
          living_expenses_cents: draft.values.living_expenses_cents,
          tuition_and_fees_cents: draft.values.tuition_and_fees_cents,
          months: draft.months,
          tuition_months: draft.tuitionMonths.filter(Boolean),
          percents: draft.percents,
        }),
      });
      draft.busy = false;
      draft.progress = { phase: "done", detail: "Your monthly plan is ready. Enter rent to see what is left to spend." };
      setStatus("Done");
      renderI20();
    } catch (error) {
      draft.busy = false;
      draft.progress = { phase: "failed", detail: error.message };
      setStatus(error.message);
      renderI20();
    }
  }

  async function uploadI20(input) {
    const chosen = input.files && input.files[0];
    input.value = "";
    if (!chosen || draft.busy) return;
    draft.busy = true;
    draft.progress = { phase: "processing", detail: "Reading the financial section" };
    setStatus("Processing");
    renderI20();
    try {
      const body = new FormData();
      body.append("file", chosen);
      const response = await fetch("/api/i20/extract", { method: "POST", body });
      const payload = await response.json().catch(() => ({}));
      draft.busy = false;
      if (!response.ok) {
        draft.banner = errorText(payload);
        draft.progress = { phase: "failed", detail: draft.banner };
        setStatus(draft.banner);
        renderI20();
        return;
      }
      draft.banner = payload.banner || "";
      draft.values = payload.values;
      draft.plan = null;
      draft.percents = null;
      draft.progress = {
        phase: "done",
        detail: draft.banner || "Check the living expenses, then confirm.",
      };
      setStatus("Done");
      renderI20();
    } catch (error) {
      draft.busy = false;
      draft.banner = error.message;
      draft.progress = { phase: "failed", detail: error.message };
      setStatus(error.message);
      renderI20();
    }
  }
}

function planCard(draft) {
  const plan = draft.plan;
  const yearlyLiving = money(draft.values.living_expenses_cents);
  const monthlyLiving = money(plan.monthly_living_cents);
  const card = h("section", { className: "card" });
  const title = h("h2", { className: "card-title" });
  setText(title, "Here is the monthly version");
  const intro = h("p");
  setText(intro, "This comes from living expenses only. Nothing is saved yet.");
  const living = h("p", { className: "plan-figure" });
  setText(living, `${yearlyLiving} a year for living costs is ${monthlyLiving} a month over ${plan.months} months.`);
  const periodLabel = h("p");
  setText(periodLabel, "1. How long should this monthly budget cover?");
  const period = h("div", { className: "button-row" });
  [
    [9, "9 months, a school year"],
    [12, "12 months, a full year"],
  ].forEach(([count, label]) => {
    period.append(h("button", {
      type: "button",
      className: draft.months === count ? "primary" : "secondary",
      onClick: async () => {
        draft.months = count;
        await reloadPlan(draft);
      },
    }, [label]));
  });
  const rentLine = plan.lines.find((line) => line.category === "Rent & utilities" && line.cadence === "monthly");
  const suggestedRent = rentLine ? rentLine.amount_cents : 0;
  const rentValue = draft.rentEdited ? draft.rentCents : suggestedRent;
  const rentInput = h("input", {
    inputmode: "decimal",
    "aria-label": "Monthly rent",
    value: centsToDollars(rentValue || 0),
  });
  const rentResult = h("p", { className: "plan-figure" });
  function paintRent() {
    const parsed = dollarsToCents(rentInput.value, false, true);
    if (parsed.error) {
      setText(rentResult, parsed.error);
      return null;
    }
    const left = Math.max(0, plan.monthly_living_cents - parsed.cents);
    setText(rentResult, `${monthlyLiving} a month − ${money(parsed.cents)} rent = ${money(left)} left to spend.`);
    return parsed.cents;
  }
  rentInput.addEventListener("input", () => {
    draft.rentEdited = true;
    const cents = paintRent();
    if (cents != null) draft.rentCents = cents;
  });
  paintRent();
  card.append(
    title,
    intro,
    living,
    periodLabel,
    period,
    h("p", {}, ["2. What is your rent each month? It will repeat every month."]),
    h("label", {}, ["Monthly rent", rentInput]),
    rentResult,
    h("p", { className: "quiet" }, ["The amount left is what you can use for other spending. Rent is kept separate."]),
    h("button", {
      type: "button",
      className: "primary",
      disabled: draft.busy ? "true" : false,
      onClick: () => saveRent(draft, rentInput.value),
    }, ["Save rent as recurring"]),
  );
  return card;
}

async function saveRent(draft, raw) {
  const parsed = dollarsToCents(raw, false, true);
  if (parsed.error) {
    setStatus(parsed.error);
    return;
  }
  if (draft.busy) return;
  draft.rentEdited = true;
  draft.rentCents = parsed.cents;
  draft.busy = true;
  draft.progress = { phase: "processing", detail: "Saving rent as a monthly charge" };
  setStatus("Processing");
  renderI20();
  try {
    const saved = await api("/api/i20/rent", {
      method: "POST",
      body: JSON.stringify({
        monthly_living_cents: draft.plan.monthly_living_cents,
        rent_cents: parsed.cents,
      }),
    });
    state.settings = Object.assign({}, state.settings || {}, {
      monthly_limit_cents: saved.spending_cents,
    });
    draft.busy = false;
    draft.progress = {
      phase: "done",
      detail: `Rent of ${money(saved.rent_cents)} repeats every month. ${money(saved.spending_cents)} is left to spend.`,
    };
    setStatus("Done");
    renderI20();
  } catch (error) {
    draft.busy = false;
    draft.progress = { phase: "failed", detail: error.message };
    setStatus(error.message);
    renderI20();
  }
}

async function reloadPlan(draft) {
  draft.busy = true;
  draft.progress = { phase: "processing", detail: "Updating the monthly plan" };
  setStatus("Processing");
  renderI20();
  try {
    draft.plan = await api("/api/i20/plan", {
      method: "POST",
      body: JSON.stringify({
        living_expenses_cents: draft.values.living_expenses_cents,
        tuition_and_fees_cents: draft.values.tuition_and_fees_cents,
        months: draft.months,
        tuition_months: draft.tuitionMonths.filter(Boolean),
        percents: draft.percents,
      }),
    });
    draft.busy = false;
    draft.progress = { phase: "done", detail: "The monthly plan is updated. Enter rent to see what is left to spend." };
    setStatus("Done");
    renderI20();
  } catch (error) {
    draft.busy = false;
    draft.progress = { phase: "failed", detail: error.message };
    setStatus(error.message);
    renderI20();
  }
}

async function suggestSplit(draft, note) {
  if (draft.busy) return;
  draft.busy = true;
  draft.progress = { phase: "processing", detail: "Adjusting the split" };
  setStatus("Processing");
  renderI20();
  try {
    const result = await api("/api/i20/split", {
      method: "POST",
      body: JSON.stringify({ note }),
    });
    draft.percents = result.used_default ? null : result.percents;
    if (result.banner) draft.banner = result.banner;
    await reloadPlan(draft);
  } catch (error) {
    draft.busy = false;
    draft.progress = { phase: "failed", detail: error.message };
    setStatus(error.message);
    renderI20();
  }
}

async function applyPlan(draft, editors) {
  const monthly = [];
  const lumps = [];
  for (const row of editors) {
    const parsed = dollarsToCents(row.input.value, false, true);
    const label = row.line.cadence === "lump" ? "Tuition" : row.line.category;
    if (parsed.error) {
      setStatus(`${label}: ${parsed.error}`);
      return;
    }
    row.line.amount_cents = parsed.cents;
    if (row.line.cadence === "lump") {
      lumps.push({ month: row.line.month, amount_cents: parsed.cents });
    } else {
      monthly.push({ category: row.line.category, amount_cents: parsed.cents });
    }
  }
  if (draft.values.tuition_and_fees_cents > 0 && !lumps.length) {
    setStatus("Choose the month for the tuition lump.");
    return;
  }
  if (draft.busy) return;
  draft.busy = true;
  draft.progress = { phase: "processing", detail: "Saving the monthly budget" };
  setStatus("Processing");
  renderI20();
  try {
    await api("/api/i20/apply", {
      method: "POST",
      body: JSON.stringify({ monthly, lumps }),
    });
    const current = state.settings || await api("/api/settings");
    const monthlyTotal = monthly.reduce((sum, row) => sum + row.amount_cents, 0);
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        profile: current.profile,
        academic_year_months: current.academic_year_months,
        custom_categories: current.custom_categories,
        monthly_limit_cents: monthlyTotal,
      }),
    });
    draft.busy = false;
    draft.progress = { phase: "done", detail: `Your monthly budget is now ${money(monthlyTotal)}.` };
    setStatus("Done");
    renderI20();
  } catch (error) {
    draft.busy = false;
    draft.progress = { phase: "failed", detail: error.message };
    setStatus(error.message);
    renderI20();
  }
}

async function loadReady() {
  try {
    const data = await api("/api/ready");
    const lines = [];
    if (!data.model_ready) {
      lines.push(data.model_message || "Add an API key in .env in this folder or the folder above it, then restart the app.");
    }
    if (data.cap_reached) {
      lines.push(`Today's AI limit of ${data.max_runs_per_day} runs is used up. You can still review and edit saved activity. New model reads can continue tomorrow.`);
    }
    showAlert(lines.join(" "));
  } catch (error) {
    showAlert("");
  }
}

async function loadUsage() {
  const box = document.querySelector("#usage");
  try {
    const data = await api("/api/usage");
    const last = document.querySelector("#usage-last");
    const today = document.querySelector("#usage-today");
    if (data.last) setText(last, usageLine("Last upload", data.last));
    else setText(last, "Last upload: none yet.");
    const left = Math.max(0, Number(data.max_runs_per_day) - Number(data.runs_today));
    const runs = left === 1 ? "1 run left" : `${left} runs left`;
    setText(today, `${usageLine("Today", data.totals)} · ${runs}`);
    box.hidden = false;
  } catch (error) {
    box.hidden = true;
  }
}

function usageLine(label, row) {
  const input = Number(row.input_tokens || 0).toLocaleString("en-US");
  const cached = Number(row.cached_tokens || 0).toLocaleString("en-US");
  const output = Number(row.output_tokens || 0).toLocaleString("en-US");
  return `${label}: ${input} input · ${cached} cached · ${output} output · ${formatCost(row.cost_usd)}`;
}

async function loadReviewCount() {
  const data = await api("/api/review");
  const badge = document.querySelector("#review-badge");
  if (data.count > 0) {
    badge.hidden = false;
    setText(badge, String(data.count));
  } else {
    badge.hidden = true;
    setText(badge, "");
  }
  return data;
}

async function showTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((button) => {
    const selected = button.dataset.tab === name;
    button.setAttribute("aria-selected", selected ? "true" : "false");
    if (selected) button.scrollIntoView({ inline: "center", block: "nearest" });
  });
  document.querySelectorAll(".panel").forEach((section) => {
    section.hidden = section.id !== `panel-${name}`;
  });
  if (name === "add") {
    if (!state.addReady) {
      await renderAdd();
      state.addReady = true;
    } else {
      await renderActivity();
    }
  } else if (name === "review") await renderReview();
  else if (name === "dashboard") await renderDashboard();
  else if (name === "i20") renderI20();
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => showTab(button.dataset.tab));
});

let creatingAccount = true;

function showGate(message) {
  document.body.classList.add("logged-out");
  const gate = document.querySelector("#gate");
  gate.hidden = false;
  const error = document.querySelector("#gate-error");
  if (message) {
    error.hidden = false;
    setText(error, message);
  }
}

function setGateMode(creating) {
  creatingAccount = creating;
  document.querySelector("#gate-confirm-label").hidden = !creating;
  document.querySelector("#gate-password").setAttribute("autocomplete", creating ? "new-password" : "current-password");
  setText(document.querySelector("#gate-submit"), creating ? "Create account" : "Log in");
  setText(document.querySelector("#gate-switch"), creating ? "I already have an account" : "Create an account");
  setText(
    document.querySelector("#gate-copy"),
    creating
      ? "Choose an email and a password. This account keeps your own spending."
      : "Use the email and password for your account.",
  );
  const error = document.querySelector("#gate-error");
  error.hidden = true;
  setText(error, "");
}

function bindGate() {
  loadReady();
  document.querySelector("#gate-switch").addEventListener("click", () => setGateMode(!creatingAccount));
  document.querySelector("#gate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const email = document.querySelector("#gate-email").value.trim();
    const password = document.querySelector("#gate-password").value;
    const confirm = document.querySelector("#gate-confirm").value;
    const error = document.querySelector("#gate-error");
    if (password.length < 8) {
      error.hidden = false;
      setText(error, "Use at least 8 characters for the password.");
      return;
    }
    if (creatingAccount && password !== confirm) {
      error.hidden = false;
      setText(error, "Passwords do not match.");
      return;
    }
    const response = await fetch(creatingAccount ? "/api/auth/register" : "/api/auth/login", {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      error.hidden = false;
      setText(error, errorText(payload));
      return;
    }
    window.location.reload();
  });
  document.querySelector("#sign-out").addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.reload();
  });
}

async function boot() {
  bindGate();
  try {
    const me = await api("/api/auth/me");
    setText(document.querySelector("#account-email"), me.email);
    document.querySelector("#account").hidden = false;
    document.body.classList.remove("logged-out");
    document.querySelector("#gate").hidden = true;
  } catch (error) {
    showGate();
    return;
  }
  try {
    const categories = await api("/api/categories");
    state.categories = categories.categories;
    state.profile = categories.profile;
    await loadReviewCount();
    await loadUsage();
    await loadReady();
    await showTab("add");
  } catch (error) {
    setStatus(error.message);
  }
}

boot();
