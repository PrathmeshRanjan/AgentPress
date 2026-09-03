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
function displayFinalResult(event) {
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

    // Refresh history list so the new writeup appears in sidebar
    loadHistory();

    // Smooth-scroll down to deliverable
    document.getElementById("resultCard").scrollIntoView({
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
        topicInput.value = button.dataset.topic || "";
        charCounter.textContent = `${topicInput.value.length} / 1000`;
        topicInput.focus();
    });
});

// Result tab switching
previewTab.addEventListener("click", () => {
    previewTab.classList.add("active");
    markdownTab.classList.remove("active");
    articlePreview.hidden = false;
    markdownOutput.hidden = true;
});

markdownTab.addEventListener("click", () => {
    markdownTab.classList.add("active");
    previewTab.classList.remove("active");
    markdownOutput.hidden = false;
    articlePreview.hidden = true;
});

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

        // Switch to Preview tab
        tabPreview.click();

        // Render into deliverable pane
        displayFinalResult({
            markdown: data.markdown,
            download_url: data.download_url,
        });

        // Set topic input to article topic
        if (topicInput && data.topic) {
            topicInput.value = data.topic;
            charCounter.textContent = `${data.topic.length} / 1000`;
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

// Initial bootstrap on page load
resetInterface();
checkHealth();
loadHistory();
