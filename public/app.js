/* ── Orbit Resume Matcher — Frontend Logic ─────────────────────────── */

const configuredApiUrl = window.APP_CONFIG && typeof window.APP_CONFIG.API_URL === "string"
  ? window.APP_CONFIG.API_URL.trim()
  : null;
const API_URL = configuredApiUrl === null
  ? "http://localhost:8000"
  : configuredApiUrl.replace(/\/$/, "");

/* ── State ─────────────────────────────────────────────────────────── */
const state = {
  charts: { radar: null, bar: null, doughnut: null },
  llmMode: "auto",
};

/* ── DOM Refs ──────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
const refs = {
  form:        $("analyzeForm"),
  cvFile:      $("cvFile"),
  dropZone:    $("dropZone"),
  dropIcon:    $("dropIcon"),
  dropTitle:   $("dropTitle"),
  dropHint:    $("dropHint"),
  statusText:  $("statusText"),
  analyzeBtn:  $("analyzeBtn"),
  btnText:     document.querySelector(".btn-text"),
  btnLoader:   $("btnLoader"),
  useLlm:      $("useLlm"),
  results:     $("results"),
  atsScore:    $("atsScore"),
  ringFill:    $("ringFill"),
  grade:       $("grade"),
  gradeSub:    $("gradeSub"),
  gradeCard:   $("gradeCard"),
  confidence:  $("confidence"),
  llmModel:    $("llmModel"),
  expYears:    $("expYears"),
  recs:        $("recommendationsList"),
  missing:     $("missingSections"),
  keywords:    $("missingKeywords"),
  llmBanner:   $("llmSummaryBanner"),
  llmText:     $("llmSummaryText"),
  sgRow:       $("strengthsGapsRow"),
  strengths:   $("strengthsList"),
  gaps:        $("gapsList"),
  mistakesBox: $("mistakesSection"),
  mistakes:    $("mistakesList"),
  reanalyze:   $("reanalyzeBtn"),
  toast:       $("toast"),
};

/* ── Utility ───────────────────────────────────────────────────────── */
function showToast(msg, isError = false) {
  refs.toast.textContent = msg;
  refs.toast.classList.toggle("error", isError);
  refs.toast.classList.add("show");
  setTimeout(() => refs.toast.classList.remove("show"), 4000);
}

function setStatus(msg, isError = false) {
  refs.statusText.textContent = msg;
  refs.statusText.style.color = isError ? "var(--danger)" : "var(--muted)";
}

function setLoading(on) {
  refs.analyzeBtn.disabled = on;
  refs.btnLoader.classList.toggle("hidden", !on);
  refs.btnText.textContent = on ? "Analyzing Resume..." : "Launch Analysis";
  
  if (on) {
    refs.analyzeBtn.style.opacity = "0.8";
    refs.analyzeBtn.style.cursor = "wait";
  } else {
    refs.analyzeBtn.style.opacity = "1";
    refs.analyzeBtn.style.cursor = "pointer";
  }
}

function createTag(text, cls = "") {
  const el = document.createElement("span");
  el.className = ("tag " + cls).trim();
  el.textContent = text;
  return el;
}

/* ── Drag & Drop ───────────────────────────────────────────────────── */
["dragenter", "dragover"].forEach((ev) =>
  refs.dropZone.addEventListener(ev, (e) => { e.preventDefault(); refs.dropZone.classList.add("drag-over"); })
);
["dragleave", "drop"].forEach((ev) =>
  refs.dropZone.addEventListener(ev, () => refs.dropZone.classList.remove("drag-over"))
);
refs.dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  const file = e.dataTransfer.files[0];
  if (file && file.name.toLowerCase().endsWith(".pdf")) {
    const dt = new DataTransfer();
    dt.items.add(file);
    refs.cvFile.files = dt.files;
    onFileSelected(file);
  } else {
    showToast("Please upload a valid PDF file.", true);
  }
});
refs.cvFile.addEventListener("change", () => {
  if (refs.cvFile.files[0]) onFileSelected(refs.cvFile.files[0]);
});

function onFileSelected(file) {
  refs.dropZone.classList.add("has-file");
  refs.dropIcon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="color:var(--accent-2)"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`;
  refs.dropTitle.textContent = file.name;
  const kb = (file.size / 1024).toFixed(1);
  refs.dropHint.textContent = `${kb} KB · File selected and ready`;
  showToast(`Resume "${file.name}" ready for analysis.`);
}

/* ── LLM Toggle ────────────────────────────────────────────────────── */
document.querySelectorAll(".toggle[data-llm]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".toggle[data-llm]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.llmMode = btn.dataset.llm;
    refs.useLlm.value = state.llmMode;
  });
});

/* ── Charts ────────────────────────────────────────────────────────── */
// Premium palette from CSS variables (approximate HSL)
const palette = {
  cyan: "hsl(195, 85%, 65%)",
  green: "hsl(145, 75%, 65%)",
  purple: "hsl(260, 85%, 75%)",
  orange: "hsl(25, 95%, 60%)",
  yellow: "hsl(45, 95%, 65%)",
  red: "hsl(0, 85%, 65%)",
  grid: "hsla(226, 100%, 80%, 0.1)",
  text: "hsl(226, 20%, 75%)",
  textLight: "hsl(226, 20%, 96%)"
};

const chartColors = [palette.cyan, palette.green, palette.purple, palette.orange, palette.yellow, palette.red];
const chartFont = { family: "'Space Grotesk', sans-serif", size: 12, weight: '500' };

function destroyCharts() {
  Object.keys(state.charts).forEach((k) => {
    if (state.charts[k]) { state.charts[k].destroy(); state.charts[k] = null; }
  });
}

function renderCharts(report) {
  destroyCharts();
  const labels = Object.keys(report.component_scores).map((k) => k.replace(/_/g, " "));
  const values = Object.values(report.component_scores);

  const commonOptions = {
    responsive: true,
    maintainAspectRatio: true,
    plugins: {
      legend: { display: false }
    }
  };

  state.charts.radar = new Chart($("radarChart"), {
    type: "radar",
    data: { 
      labels, 
      datasets: [{ 
        label: "Score", 
        data: values, 
        borderColor: palette.cyan, 
        backgroundColor: "hsla(195, 85%, 65%, 0.15)", 
        pointBackgroundColor: palette.green, 
        pointBorderColor: "#fff",
        pointRadius: 4, 
        borderWidth: 3 
      }] 
    },
    options: {
      ...commonOptions,
      scales: { 
        r: { 
          min: 0, max: 100, 
          ticks: { display: false, stepSize: 25 }, 
          grid: { color: palette.grid }, 
          angleLines: { color: palette.grid }, 
          pointLabels: { color: palette.text, font: { ...chartFont, size: 10 } } 
        } 
      },
    },
  });

  state.charts.bar = new Chart($("barChart"), {
    type: "bar",
    data: { 
      labels, 
      datasets: [{ 
        data: values, 
        backgroundColor: chartColors.slice(0, values.length), 
        borderRadius: 8, 
        borderSkipped: false 
      }] 
    },
    options: {
      ...commonOptions,
      scales: { 
        y: { 
          min: 0, max: 100, 
          ticks: { color: palette.text, font: chartFont }, 
          grid: { color: palette.grid } 
        }, 
        x: { 
          ticks: { color: palette.text, font: { ...chartFont, size: 10 }, maxRotation: 45 }, 
          grid: { display: false } 
        } 
      },
    },
  });

  const matched = report.keyword_coverage.matched || 0;
  const missing = report.keyword_coverage.missing || 0;
  state.charts.doughnut = new Chart($("doughnutChart"), {
    type: "doughnut",
    data: { 
      labels: ["Matched", "Missing"], 
      datasets: [{ 
        data: [matched, missing], 
        backgroundColor: [palette.green, palette.red], 
        borderWidth: 0, 
        spacing: 4,
        hoverOffset: 10
      }] 
    },
    options: {
      ...commonOptions,
      cutout: "70%",
      plugins: { 
        legend: { 
          display: true, 
          position: 'bottom',
          labels: { color: palette.text, font: chartFont, padding: 20, usePointStyle: true } 
        } 
      },
    },
  });
}

/* ── Score Ring Animation ──────────────────────────────────────────── */
function animateScoreRing(score) {
  const circumference = 2 * Math.PI * 33; // r=33
  const offset = circumference - (score / 100) * circumference;
  refs.ringFill.style.strokeDasharray = circumference;
  refs.ringFill.style.strokeDashoffset = circumference;
  
  // Stagger the animation slightly
  setTimeout(() => {
    refs.ringFill.style.strokeDashoffset = offset;
  }, 200);
}

/* ── Grade Helpers ─────────────────────────────────────────────────── */
const gradeDescriptions = {
  A: "Outstanding alignment with ATS criteria",
  B: "Strong fit with minor optimization gaps",
  C: "Moderate fit — requires targeted improvements",
  D: "Significant gaps in structure or content",
  F: "Incomplete or poorly structured resume",
};

/* ── Render Report ─────────────────────────────────────────────────── */
function renderReport(report) {
  refs.results.classList.remove("hidden");
  setTimeout(() => refs.results.scrollIntoView({ behavior: "smooth", block: "start" }), 150);

  // ATS Score
  const score = report.ats_score || 0;
  refs.atsScore.textContent = score.toFixed(0);
  animateScoreRing(score);

  // Grade
  const g = report.grade || "—";
  refs.grade.textContent = g;
  refs.grade.style.color = score >= 85 ? "var(--accent-2)" : score >= 70 ? "var(--accent)" : score >= 50 ? "var(--warning)" : "var(--danger)";
  refs.gradeSub.textContent = gradeDescriptions[g] || "";

  // AI Confidence
  const llm = report.llm || {};
  if (llm.used && typeof llm.confidence === "number") {
    refs.confidence.textContent = (llm.confidence * 100).toFixed(0) + "%";
    refs.llmModel.textContent = llm.model || "Neural Engine";
  } else {
    refs.confidence.textContent = "N/A";
    refs.llmModel.textContent = "ATS Standard Engine";
  }

  // Experience
  const exp = report.experience || {};
  refs.expYears.textContent = exp.estimated_years_from_cv != null ? exp.estimated_years_from_cv.toFixed(1) : "—";

  // AI Summary banner
  if (llm.used && llm.summary) {
    refs.llmBanner.classList.remove("hidden");
    refs.llmText.textContent = llm.summary;
  } else {
    refs.llmBanner.classList.add("hidden");
  }

  // Mistakes & Errors
  const mistakes = report.mistakes || [];
  if (mistakes.length > 0) {
    refs.mistakesBox.classList.remove("hidden");
    refs.mistakes.innerHTML = "";
    mistakes.forEach((m) => {
      const li = document.createElement("li");
      li.textContent = m;
      refs.mistakes.appendChild(li);
    });
  } else {
    refs.mistakesBox.classList.add("hidden");
  }

  // Strengths & Gaps
  const strengths = llm.strengths || [];
  const gaps = llm.gaps || [];
  if (strengths.length || gaps.length) {
    refs.sgRow.classList.remove("hidden");
    refs.strengths.innerHTML = "";
    strengths.forEach((s) => { const li = document.createElement("li"); li.textContent = s; refs.strengths.appendChild(li); });
    refs.gaps.innerHTML = "";
    gaps.forEach((g) => { const li = document.createElement("li"); li.textContent = g; refs.gaps.appendChild(li); });
  } else {
    refs.sgRow.classList.add("hidden");
  }

  // Recommendations
  refs.recs.innerHTML = "";
  (report.recommendations || []).forEach((r) => {
    const li = document.createElement("li");
    li.textContent = r;
    refs.recs.appendChild(li);
  });

  // Missing sections
  refs.missing.innerHTML = "";
  const sections = report.missing_sections && report.missing_sections.length ? report.missing_sections : ["All standard sections found"];
  sections.forEach((s) => {
    const cls = report.missing_sections && report.missing_sections.length ? "warn" : "";
    refs.missing.appendChild(createTag(s, cls));
  });

  // Missing keywords
  refs.keywords.innerHTML = "";
  const kw = report.keyword_coverage || {};
  const kwList = kw.missing_top && kw.missing_top.length ? kw.missing_top.slice(0, 15) : ["No critical missing keywords"];
  kwList.forEach((w) => {
    const cls = kw.missing_top && kw.missing_top.length ? "danger" : "";
    refs.keywords.appendChild(createTag(w, cls));
  });

  // Charts
  renderCharts(report);
}

/* ── Submit ─────────────────────────────────────────────────────────── */
async function submitAnalysis(event) {
  event.preventDefault();

  if (!refs.cvFile.files[0]) {
    showToast("Please upload a resume to begin analysis.", true);
    return;
  }

  setLoading(true);
  setStatus("Engaging AI analysis engine...");
  refs.results.classList.add("hidden");

  const formData = new FormData();
  formData.append("cv_file", refs.cvFile.files[0]);
  formData.append("use_llm", state.llmMode);

  try {
    const res = await fetch(`${API_URL}/analyze`, { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) throw new Error(data.error || "System error during analysis.");

    renderReport(data.report);
    setStatus("Analysis completed successfully.");
    showToast("Intelligence report generated.");
  } catch (err) {
    setStatus(err.message, true);
    showToast(err.message, true);
  } finally {
    setLoading(false);
  }
}

/* ── Re-analyze ────────────────────────────────────────────────────── */
refs.reanalyze.addEventListener("click", () => {
  refs.results.classList.add("hidden");
  refs.cvFile.value = "";
  refs.dropZone.classList.remove("has-file");
  refs.dropIcon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>`;
  refs.dropTitle.textContent = "Drop your resume here";
  refs.dropHint.textContent = "or click to browse · PDF only · Max 10 MB";
  refs.mistakesBox.classList.add("hidden");
  setStatus("");
  window.scrollTo({ top: 0, behavior: "smooth" });
  destroyCharts();
});

/* ── Init ──────────────────────────────────────────────────────────── */
refs.form.addEventListener("submit", submitAnalysis);
