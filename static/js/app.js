/**
 * AgentPress - Client-Side Controller (app.js)
 * ============================================
 *
 * Powers the real-time, event-driven user interface for AgentPress.
 * Key responsibilities:
 * 1. Streaming execution via Server-Sent Events (SSE) over Fetch & ReadableStream.
 * 2. Visual stage tracking (Router -> Research -> Plan -> Parallel Workers -> Visuals).
 * 3. Editorial preference management (Format/Genre, Audience, Tone).
 * 4. Safe client-side Markdown rendering (Marked.js) with Highlight.js and XSS sanitization (DOMPurify).
 * 5. Reading statistics calculation (word count, reading time, visual count).
 * 6. Power-user features: Cmd/Ctrl+Enter, live char counter, per-block code copy, Print/PDF, Zen reading mode.
 */

// ============================================================================
// 1. DOM Element Cache & Application State
// ============================================================================

// Form and user input controls
const runForm = document.getElementById("runForm");
const topicInput = document.getElementById("topicInput");
const charCounter = document.getElementById("charCounter");
const runButton = document.getElementById("runButton");
const runButtonText = document.getElementById("runButtonText");
const runButtonIcon = document.getElementById("runButtonIcon");
const cancelButton = document.getElementById("cancelButton");
const executionStopButton = document.getElementById("executionStopButton");
const newRunButton = document.getElementById("newRunButton");

// Editorial settings controls
const genreSelect = document.getElementById("genreSelect");
const audienceSelect = document.getElementById("audienceSelect");
const toneSelect = document.getElementById("toneSelect");

// Execution timeline panel
const timeline = document.getElementById("timeline");
const activityEmpty = document.getElementById("activityEmpty");

// Outline strategy panel
const planList = document.getElementById("planList");
const planEmpty = document.getElementById("planEmpty");
const planMeta = document.getElementById("planMeta");
const taskCount = document.getElementById("taskCount");

// Global progress & status badges
const progressRing = document.getElementById("progressRing");
const progressText = document.getElementById("progressText");
const runBadge = document.getElementById("runBadge");

// Deliverable preview, reading metrics & actions
const readingStats = document.getElementById("readingStats");
const statsWordCount = document.getElementById("statsWordCount");
const statsReadTime = document.getElementById("statsReadTime");
const statsVisualCount = document.getElementById("statsVisualCount");

const articlePreview = document.getElementById("articlePreview");
const markdownOutput = document.getElementById("markdownOutput");
const previewTab = document.getElementById("previewTab");
const markdownTab = document.getElementById("markdownTab");
const telemetryTab = document.getElementById("telemetryTab");
const telemetryOutput = document.getElementById("telemetryOutput");
const telemetryEmpty = document.getElementById("telemetryEmpty");
const telemetryDetails = document.getElementById("telemetryDetails");
const liveTelLatency = document.getElementById("liveTelLatency");
const liveTelTokens = document.getElementById("liveTelTokens");
const liveTelCost = document.getElementById("liveTelCost");
const liveTelRevisions = document.getElementById("liveTelRevisions");
const liveTelTableBody = document.getElementById("liveTelTableBody");

// Navigation views
const navStudioBtn = document.getElementById("navStudioBtn");
const navBenchmarksBtn = document.getElementById("navBenchmarksBtn");
const studioView = document.getElementById("studioView");
const benchmarkView = document.getElementById("benchmarkView");

// Benchmark dashboard elements
const refreshBenchmarkBtn = document.getElementById("refreshBenchmarkBtn");
const kpiTokenVal = document.getElementById("kpiTokenVal");
const kpiLatencyVal = document.getElementById("kpiLatencyVal");
const kpiCostVal = document.getElementById("kpiCostVal");
const kpiWordsVal = document.getElementById("kpiWordsVal");
const topicCountBadge = document.getElementById("topicCountBadge");
const benchmarkTopicsTableBody = document.getElementById("benchmarkTopicsTableBody");

const abSelectedTopicTitle = document.getElementById("abSelectedTopicTitle");
const abTopicSelect = document.getElementById("abTopicSelect");
const revealIdentityBtn = document.getElementById("revealIdentityBtn");
const identityA = document.getElementById("identityA");
const identityB = document.getElementById("identityB");
const abMetaA = document.getElementById("abMetaA");
const abMetaB = document.getElementById("abMetaB");
const abContentA = document.getElementById("abContentA");
const abContentB = document.getElementById("abContentB");

const waterfallSubtitle = document.getElementById("waterfallSubtitle");
const waterfallTotalCost = document.getElementById("waterfallTotalCost");
const waterfallTableBody = document.getElementById("waterfallTableBody");

const copyButton = document.getElementById("copyButton");
const copyButtonText = document.getElementById("copyButtonText");
const printButton = document.getElementById("printButton");
const zenButton = document.getElementById("zenButton");
const zenButtonText = document.getElementById("zenButtonText");
const downloadButton = document.getElementById("downloadButton");

// Server health and feedback toast
const healthDot = document.getElementById("healthDot");
const healthText = document.getElementById("healthText");
const toast = document.getElementById("toast");

// Sidebar history library
const historyList = document.getElementById("historyList");
const historyEmpty = document.getElementById("historyEmpty");
const historyCount = document.getElementById("historyCount");
const refreshHistoryButton = document.getElementById("refreshHistoryButton");

// --- Runtime State Variables ---
let abortController = null; // Active AbortController instance for canceling requests
let finalMarkdown = ""; // Stores the completed blog post Markdown
let totalTasks = 0; // Total number of sections planned by orchestrator
let completedTasks = 0; // Number of sections finished by workers

/**
 * Fixed progress benchmarks for non-worker stages (in percentage).
 * Parallel worker progress dynamically scales between 35% and 75%.
 */
const stageProgress = {
    router: 10,
    research: 25,
    orchestrator: 35,
    workers: 75,
    reducer: 100,
};

// ============================================================================
// 2. Marked.js & Highlight.js Configuration
// ============================================================================

if (window.marked) {
    marked.setOptions({
        breaks: false,
        gfm: true,
        highlight: function (code, lang) {
            if (window.hljs) {
                const language = hljs.getLanguage(lang) ? lang : "plaintext";
                try {
                    return hljs.highlight(code, { language }).value;
                } catch {
                    return code;
                }
            }
            return code;
        },
    });
}

// ============================================================================
// 3. Security & Utility Functions
// ============================================================================

/**
 * Escapes unsafe characters in strings to prevent Cross-Site Scripting (XSS).
 */
function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

/**
 * Displays a brief popup notification toast.
 */
function showToast(message) {
    toast.textContent = message;
    toast.classList.add("show");

    window.setTimeout(() => {
        toast.classList.remove("show");
    }, 2200);
}

/**
 * Updates the circular progress ring and percentage text.
 */
function updateProgress(value) {
    const progress = Math.max(0, Math.min(100, Math.round(value)));
    progressRing.style.setProperty("--progress", progress);
    progressText.textContent = `${progress}%`;
}

/**
 * Computes word count, estimated reading time, and image count from markdown.
 */
function calculateReadingStats(markdownText) {
    const cleanText = markdownText
        .replace(/```[\s\S]*?```/g, "")
        .replace(/!\[.*?\]\(.*?\)/g, "")
        .replace(/[#*_`~>-]/g, " ")
        .trim();
    const words = cleanText.split(/\s+/).filter(Boolean).length;
    const readTimeMinutes = Math.max(1, Math.ceil(words / 225));
    const imagesMatch = markdownText.match(/!\[.*?\]\(.*?\)/g);
    const imageCount = imagesMatch ? imagesMatch.length : 0;

    return {
        words,
        readTimeMinutes,
        imageCount,
    };
}

// ============================================================================
// 4. UI State Lifecycle (Reset & Running States)
// ============================================================================

/**
 * Clears all previous run data and restores panels to initial empty states.
 */
function resetInterface() {
    finalMarkdown = "";
    totalTasks = 0;
    completedTasks = 0;

    // Reset Timeline
    timeline.innerHTML = "";
    activityEmpty.hidden = false;
    timeline.appendChild(activityEmpty);

    // Reset Plan
    planList.innerHTML = "";
    planEmpty.hidden = false;
    planList.appendChild(planEmpty);
    planMeta.innerHTML = "";
    planMeta.hidden = true;
    taskCount.textContent = "0 tasks";

    // Reset Deliverable
    articlePreview.innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">✎</div>
            <h3>Your writeup will appear here</h3>
            <p>
                The final writeup appears after every
                section is completed.
            </p>
        </div>
    `;
    markdownOutput.textContent = "";
    markdownOutput.hidden = true;
    articlePreview.hidden = false;

    // Reset Tabs and Action Buttons
    previewTab.classList.add("active");
    markdownTab.classList.remove("active");
    if (telemetryTab) telemetryTab.classList.remove("active");
    if (telemetryOutput) telemetryOutput.hidden = true;
    if (telemetryEmpty) telemetryEmpty.hidden = false;
    if (telemetryDetails) telemetryDetails.hidden = true;
    if (liveTelTableBody) liveTelTableBody.innerHTML = "";

    readingStats.hidden = true;
    copyButton.disabled = true;
    copyButtonText.textContent = "Copy Markdown";
    copyButton.classList.remove("copy-success");

    printButton.disabled = true;
    zenButton.disabled = true;

    downloadButton.href = "#";
    downloadButton.classList.add("disabled");
    downloadButton.setAttribute("aria-disabled", "true");

    // Exit zen mode if active
    document.body.classList.remove("zen-mode");
    zenButtonText.textContent = "Zen Mode";

    // Reset Badges and Progress
    if (runBadge) {
        runBadge.textContent = "Ready";
        runBadge.className = "run-badge";
    }
    updateProgress(0);
}

/**
 * Toggles interactive buttons and inputs while agent execution is running.
 */
function setRunningState(isRunning) {
    topicInput.disabled = isRunning;
    runButton.disabled = isRunning;

    if (cancelButton) {
        cancelButton.hidden = !isRunning;
    }
    if (executionStopButton) {
        executionStopButton.hidden = !isRunning;
    }

    if (isRunning) {
        runButton.classList.add("running");
        if (runButtonText) runButtonText.textContent = "Writing...";
        if (runButtonIcon) runButtonIcon.textContent = "⏳";
    } else {
        runButton.classList.remove("running");
        if (runButtonText) runButtonText.textContent = "Run agent";
        if (runButtonIcon) runButtonIcon.textContent = "➜";
    }

    if (isRunning && runBadge) {
        runBadge.textContent = "Running";
        runBadge.className = "run-badge running";
    }
}

// ============================================================================
// 5. Timeline Stage Rendering
// ============================================================================

function getStageElement(stageId) {
    return document.querySelector(`[data-stage-id="${stageId}"]`);
}

function createStageElement(stageId) {
    const element = document.createElement("div");
    element.className = "timeline-item";
    element.dataset.stageId = stageId;

    element.innerHTML = `
        <div class="timeline-marker">
            <span></span>
        </div>
        <div class="timeline-content">
            <div class="timeline-title"></div>
            <div class="timeline-detail"></div>
        </div>
    `;

    timeline.appendChild(element);
    return element;
}

/**
 * Updates or creates a main stage item in the execution timeline.
 */
function updateStage(event) {
    if (activityEmpty && activityEmpty.parentNode === timeline) {
        timeline.removeChild(activityEmpty);
    }

    let element = getStageElement(event.id);
    if (!element) {
        element = createStageElement(event.id);
    }

    element.classList.remove("running", "completed", "failed");
    element.classList.add(event.status || "running");

    const title = element.querySelector(".timeline-title");
    const detail = element.querySelector(".timeline-detail");

    title.textContent = event.label;
    detail.textContent = event.detail || "";

    if (event.status === "completed" && stageProgress[event.id] !== undefined) {
        updateProgress(stageProgress[event.id]);
    }
}

/**
 * Appends a minor substage item under the reducer phase.
 */
function addSubstage(event) {
    if (activityEmpty && activityEmpty.parentNode === timeline) {
        timeline.removeChild(activityEmpty);
    }

    const element = document.createElement("div");
    element.className = "timeline-item substage completed";
    element.innerHTML = `
        <div class="timeline-marker">
            <span></span>
        </div>
        <div class="timeline-content">
            <div class="timeline-title">${escapeHtml(event.label)}</div>
            <div class="timeline-detail">Completed</div>
        </div>
    `;

    timeline.appendChild(element);
}

// ============================================================================
// 6. Research & Routing Metadata Rendering
// ============================================================================

/**
 * Displays the router's analysis and search queries.
 */
function showRouting(event) {
    const modeLabels = {
        closed_book: "Evergreen topic",
        hybrid: "Research-assisted topic",
        open_book: "Current information topic",
    };

    const detail = modeLabels[event.mode] || event.mode;
    const element = getStageElement("router");

    if (element) {
        const detailElement = element.querySelector(".timeline-detail");
        detailElement.textContent = `${detail}. Research: ${
            event.needs_research ? "required" : "not required"
        }.`;
    }

    if (Array.isArray(event.queries) && event.queries.length) {
        const queriesElement = document.createElement("div");
        queriesElement.className = "query-list";
        queriesElement.innerHTML = event.queries
            .map((query) => `<span>${escapeHtml(query)}</span>`)
            .join("");

        if (element) {
            element
                .querySelector(".timeline-content")
                .appendChild(queriesElement);
        }
    }
}

/**
 * Renders verified sources discovered during web research.
 */
function showResearch(event) {
    if (!event.evidence?.length) {
        return;
    }

    const stage = getStageElement("research");
    if (!stage) {
        return;
    }

    const sources = document.createElement("div");
    sources.className = "source-list";
    sources.innerHTML = event.evidence
        .slice(0, 5)
        .map(
            (source) => `
            <a
                href="${escapeHtml(source.url)}"
                target="_blank"
                rel="noopener noreferrer"
            >
                ${escapeHtml(source.title || source.url)}
            </a>
        `,
        )
        .join("");

    stage.querySelector(".timeline-content").appendChild(sources);
}

// ============================================================================
// 7. Article Plan & Task Cards Rendering
// ============================================================================

/**
 * Renders the orchestrator's plan and task cards.
 */
function showPlan(plan) {
    planEmpty.hidden = true;
    planList.innerHTML = "";

    const tasks = plan.tasks || [];
    totalTasks = tasks.length;
    completedTasks = 0;

    taskCount.textContent = `${tasks.length} ${
        tasks.length === 1 ? "task" : "tasks"
    }`;

    // Render metadata header
    planMeta.hidden = false;
    planMeta.innerHTML = `
        <div>
            <span>Audience</span>
            <strong>${escapeHtml(plan.audience || "General Readers")}</strong>
        </div>
        <div>
            <span>Format</span>
            <strong>${escapeHtml((plan.blog_kind || "explainer").replaceAll("_", " "))}</strong>
        </div>
        <div>
            <span>Tone</span>
            <strong>${escapeHtml(plan.tone || "Conversational & Insightful")}</strong>
        </div>
    `;

    // Render individual task cards
    tasks.forEach((task, index) => {
        const item = document.createElement("article");
        item.className = "plan-item";
        item.dataset.taskId = String(task.id);

        const tags = [];
        if (task.requires_research) tags.push("Research");
        if (task.requires_citations) tags.push("Citations");
        if (task.requires_code) tags.push("Code");

        item.innerHTML = `
            <div class="plan-number">
                ${String(index + 1).padStart(2, "0")}
            </div>
            <div class="plan-content">
                <div class="plan-title-row">
                    <h3>${escapeHtml(task.title)}</h3>
                    <span class="plan-status">Waiting</span>
                </div>
                <p>${escapeHtml(task.goal)}</p>
                <div class="plan-tags">
                    <span>${escapeHtml(task.target_words)} words</span>
                    ${tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}
                </div>
                <div class="task-progress">
                    <span></span>
                </div>
            </div>
        `;

        planList.appendChild(item);
    });
}

// ============================================================================
// 8. Parallel Worker Section Completion
// ============================================================================

/**
 * Marks individual section tasks as completed and advances progress.
 */
function completeSection(event) {
    const taskElement = document.querySelector(
        `[data-task-id="${event.task_id}"]`,
    );

    if (taskElement) {
        taskElement.classList.remove("active");
        taskElement.classList.add("completed");
        const status = taskElement.querySelector(".plan-status");
        status.textContent = "Completed";

        const progressBar = taskElement.querySelector(".task-progress span");
        progressBar.style.width = "100%";
    }

    completedTasks = event.completed || completedTasks + 1;
    totalTasks = event.total || totalTasks;

    // Progress moves from 35% to 75% as sections finish
    const workerProgress = totalTasks
        ? 35 + (completedTasks / totalTasks) * 40
        : 35;

    updateProgress(workerProgress);

    const workerStage = getStageElement("workers");
    if (workerStage) {
        const detail = workerStage.querySelector(".timeline-detail");
        detail.textContent = `${completedTasks} of ${totalTasks} sections completed.`;
    }
}

// ============================================================================
// 9. Final Article Presentation & Metrics
// ============================================================================

/**
 * Renders final Markdown, sanitizes HTML, attaches per-code copy buttons,
 * and displays calculated reading metrics.
 */
function displayFinalResult(event, shouldRefreshHistory = true) {
    finalMarkdown = event.markdown || "";

    // Raw Markdown tab
    markdownOutput.textContent = finalMarkdown;

    // Sanitized HTML Preview tab
    const rendered = marked.parse(finalMarkdown);
    articlePreview.innerHTML = DOMPurify.sanitize(rendered);

    // Calculate reading stats
    const stats = calculateReadingStats(finalMarkdown);
    statsWordCount.textContent = `~${stats.words.toLocaleString()} words`;
    statsReadTime.textContent = `${stats.readTimeMinutes} min read`;
    statsVisualCount.textContent = `${stats.imageCount} visual${stats.imageCount === 1 ? "" : "s"}`;
    readingStats.hidden = false;

    // Enable action buttons
    copyButton.disabled = false;
    printButton.disabled = false;
    zenButton.disabled = false;

    downloadButton.href = event.download_url;
    downloadButton.classList.remove("disabled");
    downloadButton.setAttribute("aria-disabled", "false");

    // Attach per-block copy button to every generated <pre> code block
    articlePreview.querySelectorAll("pre").forEach((preBlock) => {
        const codeCopy = document.createElement("button");
        codeCopy.className = "code-copy-btn";
        codeCopy.type = "button";
        codeCopy.textContent = "Copy";
        codeCopy.addEventListener("click", async () => {
            const codeEl = preBlock.querySelector("code");
            const codeText = codeEl ? codeEl.innerText : preBlock.innerText;
            await navigator.clipboard.writeText(codeText);
            codeCopy.textContent = "Copied!";
            setTimeout(() => {
                codeCopy.textContent = "Copy";
            }, 1800);
        });
        preBlock.appendChild(codeCopy);
    });

    if (runBadge) {
        runBadge.textContent = "Completed";
        runBadge.className = "run-badge completed";
    }
    updateProgress(100);

    // Render telemetry panel if available
    renderTelemetryPanel(event.telemetry);

    // Refresh history list so the new writeup appears in sidebar
    if (shouldRefreshHistory) {
        loadHistory();
    }

    // Smooth-scroll down to deliverable
    document.getElementById("resultCard")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
    });
}

/**
 * Displays an error item on the execution timeline and triggers a toast.
 */
function displayError(message) {
    if (runBadge) {
        runBadge.textContent = "Failed";
        runBadge.className = "run-badge failed";
    }

    const errorItem = document.createElement("div");
    errorItem.className = "timeline-item failed";
    errorItem.innerHTML = `
        <div class="timeline-marker">
            <span></span>
        </div>
        <div class="timeline-content">
            <div class="timeline-title">Agent execution failed</div>
            <div class="timeline-detail">${escapeHtml(message)}</div>
        </div>
    `;

    timeline.appendChild(errorItem);
    showToast("Agent execution failed");
}

// ============================================================================
// 10. Server-Sent Events (SSE) Processing
// ============================================================================

/**
 * Dispatches SSE events to specific rendering functions.
 */
function handleEvent(event) {
    switch (event.type) {
        case "run_started":
            if (runBadge) runBadge.textContent = "Running";
            break;
        case "stage":
            updateStage(event);
            break;
        case "substage":
            addSubstage(event);
            break;
        case "routing":
            showRouting(event);
            break;
        case "research_complete":
            showResearch(event);
            break;
        case "plan":
            showPlan(event.plan || {});
            break;
        case "section_complete":
            completeSection(event);
            break;
        case "images_planned":
            showToast(
                `${event.count} visual${event.count === 1 ? "" : "s"} planned`,
            );
            break;
        case "final":
            displayFinalResult(event);
            break;
        case "error":
            displayError(event.message);
            break;
        case "done":
            setRunningState(false);
            break;
    }
}

/**
 * Parses raw SSE chunk stream data into JSON events.
 */
function parseSSEChunk(buffer) {
    const events = buffer.split("\n\n");
    const remaining = events.pop() || "";

    events.forEach((block) => {
        const dataLines = block
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trim());

        if (!dataLines.length) {
            return;
        }

        const rawData = dataLines.join("\n");
        try {
            const event = JSON.parse(rawData);
            handleEvent(event);
        } catch (error) {
            console.error("Could not parse stream event:", rawData, error);
        }
    });

    return remaining;
}

// ============================================================================
// 11. Streaming Network Execution (Fetch + ReadableStream)
// ============================================================================

/**
 * Initiates the POST /api/run request with topic and editorial preferences.
 */
async function executeAgent(topic) {
    abortController = new AbortController();

    const genre = genreSelect?.value || "auto";
    const audience = audienceSelect?.value || "";
    const tone = toneSelect?.value || "";

    const response = await fetch("/api/run", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            topic,
            genre,
            audience,
            tone,
        }),
        signal: abortController.signal,
    });

    if (!response.ok) {
        const message = await response.text();
        throw new Error(message || "Could not start the agent.");
    }

    if (!response.body) {
        throw new Error("Streaming is not supported by this browser.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();
        if (done) {
            break;
        }

        buffer += decoder.decode(value, { stream: true });
        buffer = parseSSEChunk(buffer);
    }

    buffer += decoder.decode();
    if (buffer.trim()) {
        parseSSEChunk(`${buffer}\n\n`);
    }
}

// ============================================================================
// 12. Event Listeners & Power-User Interactions
// ============================================================================

// Submit handler
runForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    const topic = topicInput.value.trim();
    if (topic.length < 3) {
        showToast("Enter a more detailed topic.");
        return;
    }

    resetInterface();
    setRunningState(true);
    if (activityEmpty && activityEmpty.parentNode === timeline) {
        timeline.removeChild(activityEmpty);
    }
    planEmpty.hidden = false;

    try {
        await executeAgent(topic);
    } catch (error) {
        if (error.name === "AbortError") {
            if (runBadge) {
                runBadge.textContent = "Stopped";
                runBadge.className = "run-badge stopped";
            }
            showToast("Generation stopped.");
        } else {
            displayError(error.message || "Unexpected error.");
        }
    } finally {
        setRunningState(false);
        abortController = null;
    }
});

// Power-User Keyboard Shortcut: Cmd/Ctrl + Enter to run
topicInput.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        runForm.requestSubmit();
    }
});

// Live character counter
topicInput.addEventListener("input", () => {
    const len = topicInput.value.length;
    charCounter.textContent = `${len} / 1000`;
});

// Unified Stop Execution Handler
function handleStopExecution() {
    if (abortController) {
        abortController.abort();
        abortController = null;
    }

    setRunningState(false);
    showToast("Execution stopped.");

    if (runBadge) {
        runBadge.textContent = "Stopped";
        runBadge.className = "run-badge stopped";
    }

    // Add clear visual timeline entry
    const existingStopped = document.querySelector(".timeline-item.stopped");
    if (!existingStopped && timeline) {
        const stopItem = document.createElement("div");
        stopItem.className = "timeline-item stopped";
        stopItem.innerHTML = `
            <div class="timeline-marker">
                <span>■</span>
            </div>
            <div class="timeline-content">
                <div class="timeline-header">
                    <h4>Execution Stopped</h4>
                    <span class="timeline-badge stopped">Halted</span>
                </div>
                <p>Execution was stopped by user request. You can adjust your topic or click Run again.</p>
            </div>
        `;
        timeline.prepend(stopItem);
    }
}

// Stop button handlers
cancelButton?.addEventListener("click", handleStopExecution);
executionStopButton?.addEventListener("click", handleStopExecution);

// New Article button handler
newRunButton.addEventListener("click", () => {
    switchMainView("studio");
    if (abortController) {
        abortController.abort();
    }

    resetInterface();
    topicInput.disabled = false;
    topicInput.value = "";
    charCounter.textContent = "0 / 1000";
    topicInput.focus();
});

// Quick example buttons
document.querySelectorAll("[data-topic]").forEach((button) => {
    button.addEventListener("click", () => {
        switchMainView("studio");
        topicInput.value = button.dataset.topic || "";
        charCounter.textContent = `${topicInput.value.length} / 1000`;
        topicInput.focus();
    });
});

// Result tab switching
function switchDeliverableTab(tabName) {
    const isPreview = tabName === "preview";
    const isMarkdown = tabName === "markdown";
    const isTelemetry = tabName === "telemetry";

    if (previewTab) previewTab.classList.toggle("active", isPreview);
    if (markdownTab) markdownTab.classList.toggle("active", isMarkdown);
    if (telemetryTab) telemetryTab.classList.toggle("active", isTelemetry);

    if (articlePreview) articlePreview.hidden = !isPreview;
    if (markdownOutput) markdownOutput.hidden = !isMarkdown;
    if (telemetryOutput) telemetryOutput.hidden = !isTelemetry;
}

previewTab?.addEventListener("click", () => switchDeliverableTab("preview"));
markdownTab?.addEventListener("click", () => switchDeliverableTab("markdown"));
telemetryTab?.addEventListener("click", () => switchDeliverableTab("telemetry"));

// Copy button with visual feedback
copyButton.addEventListener("click", async () => {
    if (!finalMarkdown) {
        return;
    }
    await navigator.clipboard.writeText(finalMarkdown);
    copyButtonText.textContent = "✓ Copied!";
    copyButton.classList.add("copy-success");
    showToast("Markdown copied to clipboard.");
    setTimeout(() => {
        copyButtonText.textContent = "Copy Markdown";
        copyButton.classList.remove("copy-success");
    }, 2000);
});

// Print / PDF export
printButton.addEventListener("click", () => {
    window.print();
});

// Zen reading mode toggle
zenButton.addEventListener("click", () => {
    const isZen = document.body.classList.toggle("zen-mode");
    zenButtonText.textContent = isZen ? "Exit Zen" : "Zen Mode";
    if (isZen) {
        document
            .getElementById("resultCard")
            .scrollIntoView({ behavior: "smooth" });
    }
});

// Prevent download if disabled
downloadButton.addEventListener("click", (event) => {
    if (downloadButton.classList.contains("disabled")) {
        event.preventDefault();
    }
});

// ============================================================================
// 13. Health Check & Bootstrapping
// ============================================================================

async function checkHealth() {
    try {
        const response = await fetch("/api/health");
        if (!response.ok) {
            throw new Error("Server unavailable");
        }
        if (healthDot) healthDot.classList.add("online");
        if (healthText) healthText.textContent = "Server online";
    } catch {
        if (healthDot) healthDot.classList.remove("online");
        if (healthText) healthText.textContent = "Server offline";
    }
}

// ============================================================================
// 14. Sidebar History & Library Management
// ============================================================================

function formatRelativeTime(isoString) {
    if (!isoString) return "Recently";
    try {
        const date = new Date(isoString);
        const now = new Date();
        const diffSeconds = Math.floor((now - date) / 1000);

        if (diffSeconds < 60) return "Just now";
        const diffMinutes = Math.floor(diffSeconds / 60);
        if (diffMinutes < 60) return `${diffMinutes}m ago`;
        const diffHours = Math.floor(diffMinutes / 60);
        if (diffHours < 24) return `${diffHours}h ago`;
        const diffDays = Math.floor(diffHours / 24);
        if (diffDays === 1) return "Yesterday";
        if (diffDays < 7) return `${diffDays}d ago`;

        return date.toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
        });
    } catch {
        return "Earlier";
    }
}

async function loadHistory() {
    if (!historyList) return;

    try {
        const res = await fetch("/api/history");
        if (!res.ok) return;
        const items = await res.json();

        if (historyCount) {
            historyCount.textContent = String(items.length);
        }

        if (!items || items.length === 0) {
            historyList.innerHTML = `
                <div id="historyEmpty" class="history-empty">
                    <span class="history-empty-icon">✎</span>
                    <p>Completed writeups will appear here.</p>
                </div>
            `;
            return;
        }

        historyList.innerHTML = "";
        items.forEach((item) => {
            const card = document.createElement("div");
            card.className = "history-item";
            card.dataset.runId = item.run_id;

            const wordsText = item.word_count
                ? `${Math.round(item.word_count / 100) / 10}k words`
                : "";
            const relTime = formatRelativeTime(item.created_at);

            card.innerHTML = `
                <div class="history-item-top">
                    <span class="history-item-title">${escapeHtml(item.title || item.topic || "Untitled Writeup")}</span>
                    <button type="button" class="history-item-del" title="Delete writeup">✕</button>
                </div>
                <div class="history-item-meta">
                    <span>${escapeHtml(relTime)}</span>
                    ${wordsText ? `<span class="history-badge">${wordsText}</span>` : ""}
                </div>
            `;

            // Click to load article
            card.addEventListener("click", (e) => {
                if (e.target.closest(".history-item-del")) return;
                openPreviousWriteup(item.run_id);
            });

            // Delete button
            const delBtn = card.querySelector(".history-item-del");
            delBtn?.addEventListener("click", (e) => {
                e.stopPropagation();
                deletePreviousWriteup(item.run_id);
            });

            historyList.appendChild(card);
        });
    } catch (err) {
        console.warn("Could not load writeup history:", err);
    }
}

async function openPreviousWriteup(runId) {
    if (!runId) return;

    try {
        // Highlight active item in sidebar
        document.querySelectorAll(".history-item").forEach((el) => {
            el.classList.toggle("active", el.dataset.runId === runId);
        });

        showToast("Loading previous writeup...");

        const res = await fetch(`/api/runs/${runId}`);
        if (!res.ok) {
            showToast("Failed to load writeup.");
            return;
        }

        const data = await res.json();

        // Ensure user is in Studio view when opening an article
        switchMainView("studio");

        // Switch to Preview tab
        if (previewTab) {
            previewTab.click();
        }

        // Render into deliverable pane without re-fetching history
        displayFinalResult(
            {
                markdown: data.markdown,
                download_url: data.download_url,
                telemetry: data.telemetry,
                metadata: data.pipeline_metadata,
            },
            false,
        );

        // Set topic input to article topic
        if (topicInput && data.topic) {
            topicInput.value = data.topic;
            if (charCounter) {
                charCounter.textContent = `${data.topic.length} / 1000`;
            }
        }

        showToast(`Opened: ${data.title}`);
    } catch (err) {
        console.error("Error opening previous writeup:", err);
        showToast("Error opening writeup.");
    }
}

async function deletePreviousWriteup(runId) {
    if (!confirm("Are you sure you want to delete this saved writeup?")) return;

    try {
        const res = await fetch(`/api/runs/${runId}`, { method: "DELETE" });
        if (res.ok) {
            showToast("Writeup deleted.");
            loadHistory();
        } else {
            showToast("Failed to delete.");
        }
    } catch {
        showToast("Error deleting writeup.");
    }
}

refreshHistoryButton?.addEventListener("click", () => {
    loadHistory();
    showToast("Refreshed writeup library.");
});

// ============================================================================
// 14. Benchmarking Dashboard & Observability Controller
// ============================================================================

let benchmarkSummary = null;
let benchmarkTopics = [];
let selectedTopicIndex = 0;
let identitiesRevealed = false;

/**
 * Switches between Studio and Benchmarks view panels.
 */
function switchMainView(viewName) {
    const isStudio = viewName === "studio";
    const isBenchmarks = viewName === "benchmarks";

    if (navStudioBtn) navStudioBtn.classList.toggle("active", isStudio);
    if (navBenchmarksBtn) navBenchmarksBtn.classList.toggle("active", isBenchmarks);

    if (studioView) studioView.hidden = !isStudio;
    if (benchmarkView) benchmarkView.hidden = !isBenchmarks;

    if (isBenchmarks) {
        loadBenchmarkDashboard();
    }
}

navStudioBtn?.addEventListener("click", () => switchMainView("studio"));
navBenchmarksBtn?.addEventListener("click", () => switchMainView("benchmarks"));

/**
 * Formatting helpers for telemetry and benchmarks
 */
function formatNodeName(name) {
    if (!name) return "Unknown";
    const map = {
        router: "01 Router",
        research: "02 Web Research",
        orchestrator: "03 Outline Strategy",
        worker: "04 Worker Section",
        editor: "05 Editor Critique",
        fact_checker: "06 Fact-Checker Critique",
        revision_writer: "07 Revision Writer",
        reducer: "08 Reducer Final",
        vanilla_baseline: "Vanilla Baseline",
    };
    return map[name] || name.charAt(0).toUpperCase() + name.slice(1).replace(/_/g, " ");
}

function formatLatency(ms) {
    if (typeof ms !== "number" || isNaN(ms)) return "-";
    if (ms >= 1000) {
        return `${(ms / 1000).toFixed(1)}s`;
    }
    return `${Math.round(ms)}ms`;
}

function formatCost(cost) {
    if (typeof cost !== "number" || isNaN(cost)) return "$0.0000";
    return `$${cost.toFixed(4)}`;
}

/**
 * Renders live or historical telemetry into the Studio deliverable panel.
 */
function renderTelemetryPanel(telemetry) {
    if (!telemetry || !telemetry.total_tokens) {
        if (telemetryEmpty) telemetryEmpty.hidden = false;
        if (telemetryDetails) telemetryDetails.hidden = true;
        return;
    }

    if (telemetryEmpty) telemetryEmpty.hidden = true;
    if (telemetryDetails) telemetryDetails.hidden = false;

    if (liveTelLatency) liveTelLatency.textContent = formatLatency(telemetry.latency_ms);
    if (liveTelTokens) liveTelTokens.textContent = (telemetry.total_tokens || 0).toLocaleString();
    if (liveTelCost) liveTelCost.textContent = formatCost(telemetry.estimated_cost_usd);
    if (liveTelRevisions) {
        const revCount = telemetry.revision_loop_count || 0;
        liveTelRevisions.textContent = `${revCount} loop${revCount === 1 ? "" : "s"}`;
    }

    if (liveTelTableBody) {
        liveTelTableBody.innerHTML = "";
        const invocations = telemetry.invocations || [];
        if (invocations.length === 0) {
            liveTelTableBody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:18px;">No granular invocation records.</td></tr>`;
        } else {
            invocations.forEach((inv) => {
                const tr = document.createElement("tr");
                const statusBadge = inv.status === "error"
                    ? `<span class="table-badge warning">Error</span>`
                    : `<span class="table-badge success">Success</span>`;

                tr.innerHTML = `
                    <td><strong>${escapeHtml(formatNodeName(inv.node_name))}</strong></td>
                    <td><code>${escapeHtml(inv.model || "-")}</code></td>
                    <td>${formatLatency(inv.latency_ms)}</td>
                    <td>${(inv.prompt_tokens || 0).toLocaleString()} / ${(inv.completion_tokens || 0).toLocaleString()}</td>
                    <td><strong>${(inv.total_tokens || 0).toLocaleString()}</strong></td>
                    <td>${formatCost(inv.estimated_cost_usd)}</td>
                    <td>${statusBadge}</td>
                `;
                liveTelTableBody.appendChild(tr);
            });
        }
    }
}

/**
 * Loads and renders the benchmarking dashboard data from API.
 */
async function loadBenchmarkDashboard(force = false) {
    if (benchmarkSummary && benchmarkTopics.length > 0 && !force) {
        return;
    }

    try {
        const [sumRes, topRes] = await Promise.all([
            fetch("/api/benchmark/summary"),
            fetch("/api/benchmark/topics")
        ]);

        if (!sumRes.ok || !topRes.ok) {
            showToast("Failed to load benchmark data.");
            return;
        }

        benchmarkSummary = await sumRes.json();
        benchmarkTopics = await topRes.json();

        if (!benchmarkSummary.available) {
            showToast(benchmarkSummary.message || "No benchmark records found.");
            return;
        }

        renderBenchmarkSummary(benchmarkSummary);
        renderBenchmarkTopicsTable(benchmarkSummary.topics || []);
        setupBenchmarkTopicSelector(benchmarkTopics);
        renderABComparison(selectedTopicIndex);
        renderWaterfall(selectedTopicIndex);

    } catch (err) {
        console.error("Error loading benchmark dashboard:", err);
        showToast("Error loading benchmark dashboard.");
    }
}

/**
 * Updates KPI cards and comparison progress bars with empirical metrics.
 */
function renderBenchmarkSummary(data) {
    const agg = data.aggregate;
    if (!agg) return;

    if (kpiTokenVal) {
        kpiTokenVal.innerHTML = `${Math.round(agg.pipeline.average_tokens).toLocaleString()} <small>vs ${Math.round(agg.baseline.average_tokens).toLocaleString()}</small>`;
    }
    if (kpiLatencyVal) {
        kpiLatencyVal.innerHTML = `${(agg.pipeline.average_latency_ms / 1000).toFixed(1)}s <small>vs ${(agg.baseline.average_latency_ms / 1000).toFixed(1)}s</small>`;
    }
    if (kpiCostVal) {
        kpiCostVal.innerHTML = `${formatCost(agg.pipeline.average_cost_usd)} <small>vs ${formatCost(agg.baseline.average_cost_usd)}</small>`;
    }
    if (kpiWordsVal) {
        kpiWordsVal.innerHTML = `${Math.round(agg.pipeline.average_words).toLocaleString()} <small>vs ${Math.round(agg.baseline.average_words).toLocaleString()} words</small>`;
    }

    if (topicCountBadge) {
        topicCountBadge.textContent = `${data.total_runs} Topics Evaluated`;
    }

    // Dynamic Comparison Bars
    const totalTokB = agg.baseline.total_tokens || 1;
    const totalTokP = agg.pipeline.total_tokens || 1;
    const sumTok = totalTokB + totalTokP;
    const pctTokB = Math.max(8, Math.round((totalTokB / sumTok) * 100));
    const pctTokP = 100 - pctTokB;

    const latB = agg.baseline.average_latency_ms || 1;
    const latP = agg.pipeline.average_latency_ms || 1;
    const sumLat = latB + latP;
    const pctLatB = Math.max(8, Math.round((latB / sumLat) * 100));
    const pctLatP = 100 - pctLatB;

    const costB = agg.baseline.total_cost_usd || 0.001;
    const costP = agg.pipeline.total_cost_usd || 0.001;
    const sumCost = costB + costP;
    const pctCostB = Math.max(8, Math.round((costB / sumCost) * 100));
    const pctCostP = 100 - pctCostB;

    const barRows = document.querySelectorAll(".comparison-bar-row");
    if (barRows.length >= 3) {
        // Row 1: Tokens
        const fills1 = barRows[0].querySelectorAll(".progress-bar-fill");
        if (fills1.length === 2) {
            fills1[0].style.width = `${pctTokB}%`;
            fills1[1].style.width = `${pctTokP}%`;
        }
        const nums1 = barRows[0].querySelector(".bar-nums");
        if (nums1) {
            nums1.innerHTML = `<strong>Baseline: ${totalTokB.toLocaleString()}</strong> | <strong>Pipeline: ${totalTokP.toLocaleString()}</strong> (${agg.overhead.token_overhead_percent > 0 ? "+" : ""}${agg.overhead.token_overhead_percent}%)`;
        }

        // Row 2: Latency
        const fills2 = barRows[1].querySelectorAll(".progress-bar-fill");
        if (fills2.length === 2) {
            fills2[0].style.width = `${pctLatB}%`;
            fills2[1].style.width = `${pctLatP}%`;
        }
        const nums2 = barRows[1].querySelector(".bar-nums");
        if (nums2) {
            nums2.innerHTML = `<strong>Baseline: ${(latB / 1000).toFixed(1)}s</strong> | <strong>Pipeline: ${(latP / 1000).toFixed(1)}s</strong> (${agg.overhead.latency_overhead_percent > 0 ? "+" : ""}${agg.overhead.latency_overhead_percent}%)`;
        }

        // Row 3: Cost
        const fills3 = barRows[2].querySelectorAll(".progress-bar-fill");
        if (fills3.length === 2) {
            fills3[0].style.width = `${pctCostB}%`;
            fills3[1].style.width = `${pctCostP}%`;
        }
        const nums3 = barRows[2].querySelector(".bar-nums");
        if (nums3) {
            nums3.innerHTML = `<strong>Baseline: ${formatCost(costB)}</strong> | <strong>Pipeline: ${formatCost(costP)}</strong> (${agg.overhead.cost_overhead_percent > 0 ? "+" : ""}${agg.overhead.cost_overhead_percent}%)`;
        }
    }
}

/**
 * Renders the Seed Topics Performance Table.
 */
function renderBenchmarkTopicsTable(topics) {
    if (!benchmarkTopicsTableBody) return;
    benchmarkTopicsTableBody.innerHTML = "";

    topics.forEach((t, idx) => {
        const tr = document.createElement("tr");
        tr.className = "clickable-row";
        if (idx === selectedTopicIndex) {
            tr.classList.add("selected-row");
        }

        const overheadSign = t.token_overhead_percent > 0 ? "+" : "";
        const overheadBadge = t.token_overhead_percent > 0 ? "warning" : "success";

        tr.innerHTML = `
            <td><code>${String(t.index).padStart(2, "0")}</code></td>
            <td style="font-weight: 600; max-width: 280px;">${escapeHtml(t.topic)}</td>
            <td>${(t.baseline_tokens || 0).toLocaleString()}</td>
            <td><strong>${(t.pipeline_tokens || 0).toLocaleString()}</strong></td>
            <td><span class="table-badge ${overheadBadge}">${overheadSign}${t.token_overhead_percent}%</span></td>
            <td>${t.revision_loops} loop${t.revision_loops === 1 ? "" : "s"}</td>
            <td>${formatCost(t.pipeline_cost_usd)}</td>
            <td><span class="table-badge success">${escapeHtml(t.status || "Completed")}</span></td>
            <td><button type="button" class="table-action-btn">Inspect A/B</button></td>
        `;

        tr.addEventListener("click", () => {
            selectTopic(idx);
            document.getElementById("abReviewSection")?.scrollIntoView({ behavior: "smooth" });
        });

        benchmarkTopicsTableBody.appendChild(tr);
    });
}

/**
 * Sets up the topic dropdown selector for A/B review.
 */
function setupBenchmarkTopicSelector(topics) {
    if (!abTopicSelect) return;
    abTopicSelect.innerHTML = "";

    topics.forEach((t, idx) => {
        const opt = document.createElement("option");
        opt.value = String(idx);
        opt.textContent = `Topic ${t.index}: ${t.topic}`;
        abTopicSelect.appendChild(opt);
    });

    abTopicSelect.value = String(selectedTopicIndex);

    abTopicSelect.onchange = (e) => {
        selectTopic(parseInt(e.target.value, 10));
    };
}

/**
 * Selects a topic for A/B comparison and waterfall inspection.
 */
function selectTopic(index) {
    selectedTopicIndex = index;
    if (abTopicSelect) {
        abTopicSelect.value = String(index);
    }

    if (benchmarkTopicsTableBody) {
        const rows = benchmarkTopicsTableBody.querySelectorAll("tr");
        rows.forEach((r, idx) => {
            r.classList.toggle("selected-row", idx === index);
        });
    }

    renderABComparison(index);
    renderWaterfall(index);
}

/**
 * Renders side-by-side blinded A/B outputs for the selected topic.
 */
function renderABComparison(topicIndex) {
    const topic = benchmarkTopics[topicIndex];
    if (!topic) return;

    if (abSelectedTopicTitle) {
        abSelectedTopicTitle.textContent = topic.topic;
    }

    // System A
    const sysA = topic.system_a;
    if (abMetaA) {
        const wordsA = sysA.text_metrics ? sysA.text_metrics.word_count : (sysA.telemetry?.word_count || 0);
        const tokensA = sysA.telemetry ? sysA.telemetry.total_tokens : 0;
        const costA = sysA.telemetry ? sysA.telemetry.estimated_cost_usd : 0;
        abMetaA.innerHTML = `
            <span>~${wordsA.toLocaleString()} words</span>
            <span>${tokensA.toLocaleString()} tokens</span>
            <span>${formatCost(costA)}</span>
        `;
    }
    if (abContentA) {
        const renderedA = marked.parse(sysA.content || "");
        abContentA.innerHTML = DOMPurify.sanitize(renderedA);
    }
    if (identityA) {
        identityA.textContent = sysA.identity;
        identityA.className = `identity-tag ${sysA.is_pipeline ? "pipeline" : "baseline"}`;
        identityA.hidden = !identitiesRevealed;
    }

    // System B
    const sysB = topic.system_b;
    if (abMetaB) {
        const wordsB = sysB.text_metrics ? sysB.text_metrics.word_count : (sysB.telemetry?.word_count || 0);
        const tokensB = sysB.telemetry ? sysB.telemetry.total_tokens : 0;
        const costB = sysB.telemetry ? sysB.telemetry.estimated_cost_usd : 0;
        abMetaB.innerHTML = `
            <span>~${wordsB.toLocaleString()} words</span>
            <span>${tokensB.toLocaleString()} tokens</span>
            <span>${formatCost(costB)}</span>
        `;
    }
    if (abContentB) {
        const renderedB = marked.parse(sysB.content || "");
        abContentB.innerHTML = DOMPurify.sanitize(renderedB);
    }
    if (identityB) {
        identityB.textContent = sysB.identity;
        identityB.className = `identity-tag ${sysB.is_pipeline ? "pipeline" : "baseline"}`;
        identityB.hidden = !identitiesRevealed;
    }
}

/**
 * Renders the 12-step per-agent telemetry waterfall table.
 */
function renderWaterfall(topicIndex) {
    const topic = benchmarkTopics[topicIndex];
    if (!topic) return;

    const invocations = topic.pipeline_invocations || [];
    const totalCost = invocations.reduce((acc, i) => acc + (i.estimated_cost_usd || 0), 0);

    if (waterfallSubtitle) {
        waterfallSubtitle.textContent = `12-step pipeline execution waterfall for: "${topic.topic}"`;
    }
    if (waterfallTotalCost) {
        waterfallTotalCost.textContent = `${formatCost(totalCost)} Total Pipeline Spend`;
    }

    if (!waterfallTableBody) return;
    waterfallTableBody.innerHTML = "";

    if (invocations.length === 0) {
        waterfallTableBody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--muted); padding: 18px;">No invocation logs recorded.</td></tr>`;
        return;
    }

    invocations.forEach((inv, idx) => {
        const tr = document.createElement("tr");
        const statusBadge = inv.status === "error"
            ? `<span class="table-badge warning">Error</span>`
            : `<span class="table-badge success">Success</span>`;

        tr.innerHTML = `
            <td><code>${String(idx + 1).padStart(2, "0")}</code></td>
            <td><strong>${escapeHtml(formatNodeName(inv.node_name))}</strong></td>
            <td><code>${escapeHtml(inv.model || "-")}</code></td>
            <td>${formatLatency(inv.latency_ms)}</td>
            <td>${(inv.prompt_tokens || 0).toLocaleString()}</td>
            <td>${(inv.completion_tokens || 0).toLocaleString()}</td>
            <td><strong>${(inv.total_tokens || 0).toLocaleString()}</strong></td>
            <td>${formatCost(inv.estimated_cost_usd)}</td>
            <td>${statusBadge}</td>
        `;

        waterfallTableBody.appendChild(tr);
    });
}

// Reveal System Identity button toggle
revealIdentityBtn?.addEventListener("click", () => {
    identitiesRevealed = !identitiesRevealed;
    if (identitiesRevealed) {
        revealIdentityBtn.innerHTML = `<span>🙈</span> Hide System Identity`;
        revealIdentityBtn.classList.add("revealed");
        if (identityA) identityA.hidden = false;
        if (identityB) identityB.hidden = false;
        showToast("System identities revealed.");
    } else {
        revealIdentityBtn.innerHTML = `<span>👁️</span> Reveal System Identity`;
        revealIdentityBtn.classList.remove("revealed");
        if (identityA) identityA.hidden = true;
        if (identityB) identityB.hidden = true;
        showToast("System identities blinded.");
    }
});

// Refresh button for benchmark dashboard
refreshBenchmarkBtn?.addEventListener("click", async () => {
    showToast("Refreshing benchmark metrics...");
    await loadBenchmarkDashboard(true);
    showToast("Benchmark metrics up to date.");
});

// Initial bootstrap on page load
resetInterface();
checkHealth();
loadHistory();
