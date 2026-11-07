/* CloudCrunch frontend
 * 1. POST /upload-url        -> presigned POST for the input S3 bucket
 * 2. POST file directly to S3 (the browser never holds AWS credentials)
 * 3. S3 event triggers the processor Lambda automatically
 * 4. Poll GET /status/{jobId} until the result JSON appears in the output bucket
 */
(() => {
  const API = ((window.APP_CONFIG && window.APP_CONFIG.API_URL) || "").replace(/\/+$/, "");
  const MAX_BYTES = 10 * 1024 * 1024;
  const POLL_MS = 1000;
  const POLL_TIMEOUT_MS = 60000;

  const $ = (id) => document.getElementById(id);
  const state = { file: null, originalUrl: null, maxSize: "1024", format: "webp", busy: false };

  // ---------- helpers ----------
  const fmtBytes = (b) => {
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${(b / 1024 / 1024).toFixed(2)} MB`;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function log(msg, cls = "") {
    const line = document.createElement("div");
    const t = new Date().toLocaleTimeString([], { hour12: false });
    line.innerHTML = `<span class="t">${t}</span> `;
    const span = document.createElement("span");
    span.className = cls;
    span.textContent = msg;
    line.appendChild(span);
    $("log").appendChild(line);
    $("log").scrollTop = $("log").scrollHeight;
  }

  function step(name, status) {
    const li = document.querySelector(`.pipeline li[data-step="${name}"]`);
    li.classList.remove("active", "done", "fail");
    if (status) li.classList.add(status);
  }
  const resetPipeline = () => ["api", "s3in", "lambda", "s3out"].forEach((s) => step(s, null));

  async function api(path, options = {}) {
    const res = await fetch(API + path, options);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `API error ${res.status}`);
    return body;
  }

  // ---------- API health ----------
  async function checkApi() {
    const el = $("apiStatus");
    if (!API) {
      $("configBanner").classList.remove("hidden");
      el.textContent = "● API not configured";
      el.className = "api-status bad";
      return;
    }
    try {
      await api("/health");
      el.textContent = "● API connected";
      el.className = "api-status ok";
    } catch {
      el.textContent = "● API unreachable";
      el.className = "api-status bad";
    }
  }

  // ---------- file selection ----------
  function selectFile(file) {
    if (!file) return;
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      log(`Rejected ${file.name}: only JPG, PNG or WebP`, "err");
      return;
    }
    if (file.size > MAX_BYTES) {
      log(`Rejected ${file.name}: larger than 10 MB`, "err");
      return;
    }
    state.file = file;
    if (state.originalUrl) URL.revokeObjectURL(state.originalUrl);
    state.originalUrl = URL.createObjectURL(file);

    const img = $("dropPreview");
    img.onload = () => {
      $("fileMeta").textContent = `${file.name} · ${img.naturalWidth}×${img.naturalHeight}px · ${fmtBytes(file.size)}`;
      $("fileMeta").classList.remove("hidden");
    };
    img.src = state.originalUrl;
    img.classList.remove("hidden");
    $("dropPrompt").classList.add("hidden");
    $("processBtn").disabled = !API;
  }

  $("fileInput").addEventListener("change", (e) => selectFile(e.target.files[0]));
  const dz = $("dropzone");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => selectFile(e.dataTransfer.files[0]));

  // ---------- options ----------
  document.querySelectorAll(".segmented").forEach((group) => {
    group.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      group.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
      state[group.dataset.name] = btn.dataset.value;
      if (group.dataset.name === "format") {
        $("qualityField").style.opacity = btn.dataset.value === "png" ? 0.4 : 1;  // PNG is lossless
      }
    });
  });
  $("quality").addEventListener("input", (e) => ($("qualityOut").textContent = e.target.value));

  // ---------- main flow ----------
  $("processBtn").addEventListener("click", async () => {
    if (!state.file || state.busy) return;
    state.busy = true;
    $("processBtn").disabled = true;
    resetPipeline();
    $("log").innerHTML = "";
    const file = state.file;

    try {
      // 1) presigned upload
      step("api", "active");
      log(`Requesting upload URL (size=${state.maxSize}, format=${state.format}, q=${$("quality").value})`);
      const { jobId, key, upload } = await api("/upload-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: file.name,
          contentType: file.type,
          maxSize: state.maxSize,
          format: state.format,
          quality: $("quality").value,
        }),
      });
      step("api", "done");
      log(`Job ${jobId.slice(0, 8)} created`, "ok");

      // 2) upload straight to S3
      step("s3in", "active");
      log(`Uploading ${fmtBytes(file.size)} to s3://…/${key}`);
      const form = new FormData();
      Object.entries(upload.fields).forEach(([k, v]) => form.append(k, v));
      form.append("file", file); // must be last
      const up = await fetch(upload.url, { method: "POST", body: form });
      if (!up.ok) throw new Error(`S3 upload failed (${up.status})`);
      step("s3in", "done");
      log("Upload complete → S3 ObjectCreated event fired", "ok");

      // 3) wait for Lambda
      step("lambda", "active");
      log("Waiting for Lambda to process…");
      const t0 = Date.now();
      let result;
      while (Date.now() - t0 < POLL_TIMEOUT_MS) {
        result = await api(`/status/${jobId}`);
        if (result.status !== "pending") break;
        await sleep(POLL_MS);
      }
      if (!result || result.status === "pending") throw new Error("Timed out waiting for Lambda (check CloudWatch logs)");
      if (result.status === "error") {
        step("lambda", "fail");
        throw new Error(`Lambda error: ${result.error}`);
      }
      step("lambda", "done");
      log(`Lambda finished in ${result.processing_ms} ms`, "ok");

      // 4) result in output bucket
      step("s3out", "done");
      log(`Saved s3://…/${result.output_key}`, "ok");
      showResult(file, result);
    } catch (err) {
      console.error(err);
      log(err.message, "err");
      document.querySelectorAll(".pipeline li.active").forEach((li) => {
        li.classList.remove("active");
        li.classList.add("fail");
      });
    } finally {
      state.busy = false;
      $("processBtn").disabled = false;
    }
  });

  // ---------- result ----------
  function showResult(file, r) {
    $("resultEmpty").classList.add("hidden");
    $("result").classList.remove("hidden");
    $("beforeImg").src = state.originalUrl;
    $("afterImg").src = r.previewUrl;
    $("downloadBtn").href = r.downloadUrl;
    setCompare(50);
    $("compareRange").value = 50;

    const stats = [
      ["Original", fmtBytes(r.original_bytes)],
      ["Processed", fmtBytes(r.processed_bytes)],
      ["Size saved", `${r.reduction_percent}%`, true],
      ["Dimensions", `${r.processed_width}×${r.processed_height}`],
    ];
    $("stats").innerHTML = "";
    stats.forEach(([label, value, hl]) => {
      const d = document.createElement("div");
      d.className = "stat" + (hl ? " hl" : "");
      d.innerHTML = `<small></small><b></b>`;
      d.querySelector("small").textContent = label;
      d.querySelector("b").textContent = value;
      $("stats").appendChild(d);
    });

    const body = $("historyBody");
    body.querySelector(".none")?.remove();
    const tr = document.createElement("tr");
    [
      file.name,
      `${r.output_format} · ${r.processed_width}×${r.processed_height}`,
      fmtBytes(r.original_bytes),
      fmtBytes(r.processed_bytes),
      `${r.reduction_percent}%`,
      `${r.processing_ms} ms`,
    ].forEach((v, i) => {
      const td = document.createElement("td");
      td.textContent = v;
      if (i === 4) td.className = "saved";
      tr.appendChild(td);
    });
    body.prepend(tr);
  }

  function setCompare(pct) {
    $("beforeWrap").style.clipPath = `inset(0 ${100 - pct}% 0 0)`;
    $("handle").style.left = `${pct}%`;
  }
  $("compareRange").addEventListener("input", (e) => setCompare(e.target.value));

  checkApi();
})();
