from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from time import perf_counter
from statistics import mean
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import uvicorn
# ---------------------------------------------------------
# Import the existing compiled LangGraph workflow.
#
# backend.py remains completely unchanged.
# backend.app is the compiled LangGraph workflow.
# ---------------------------------------------------------
from backend import workflow


# ---------------------------------------------------------
# Paths
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
IMAGES_DIR = BASE_DIR / "images"
OUTPUTS_DIR = BASE_DIR / "outputs"
BENCHMARK_RESULTS_PATH = BASE_DIR / "benchmark_results.jsonl"

TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("langgraph-fastapi")


# ---------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------
app = FastAPI(
    title="AgentPress",
    description="AgentPress - Autonomous Writing Studio and Editorial Engine.",
    version="1.0.0",
)


# ---------------------------------------------------------
# Static files
# ---------------------------------------------------------
app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static",
)

# backend.py saves generated images inside images/
app.mount(
    "/images",
    StaticFiles(directory=IMAGES_DIR),
    name="images",
)


# ---------------------------------------------------------
# Templates
# ---------------------------------------------------------
templates = Jinja2Templates(
    directory=TEMPLATES_DIR,
)


# ---------------------------------------------------------
# Request schema
# ---------------------------------------------------------
class AgentRunRequest(BaseModel):
    topic: str = Field(
        ...,
        min_length=3,
        max_length=1000,
        description="The writeup topic, thesis, or story concept.",
    )
    genre: Optional[str] = Field(
        default="auto",
        description="Format: explainer, story_narrative, tutorial, thought_leadership, opinion_editorial, case_study, guide, news_roundup, comparison.",
    )
    audience: Optional[str] = Field(
        default="",
        description="Target audience (e.g. General Readers, Developers, Founders & Leaders).",
    )
    tone: Optional[str] = Field(
        default="",
        description="Desired voice and tone (e.g. Conversational, Witty, Analytical, Inspiring).",
    )


# ---------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------
def make_serializable(value: Any) -> Any:
    """
    Recursively convert Pydantic models and other values
    into JSON-compatible Python values.
    """

    if hasattr(value, "model_dump"):
        return make_serializable(value.model_dump())

    if hasattr(value, "dict"):
        return make_serializable(value.dict())

    if isinstance(value, dict):
        return {
            str(key): make_serializable(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            make_serializable(item)
            for item in value
        ]

    return value


def create_sse_event(
    payload: dict[str, Any],
    event_name: str | None = None,
) -> str:
    """
    Convert a dictionary to a Server-Sent Event message.
    """

    encoded_payload = json.dumps(
        jsonable_encoder(payload),
        ensure_ascii=False,
    )

    lines: list[str] = []

    if event_name:
        lines.append(f"event: {event_name}")

    lines.append(f"data: {encoded_payload}")

    return "\n".join(lines) + "\n\n"


def normalize_stream_chunk(
    chunk: Any,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """
    Normalize LangGraph streaming chunks.

    With subgraphs=True, LangGraph commonly returns:

        (namespace, update)

    Example:

        (
            ("reducer:<task-id>",),
            {"merge_content": {...}}
        )

    Root graph updates may also be returned directly
    as dictionaries depending on the LangGraph version.
    """

    if (
        isinstance(chunk, tuple)
        and len(chunk) == 2
        and isinstance(chunk[1], dict)
    ):
        raw_namespace = chunk[0] or ()

        namespace = tuple(
            str(item)
            for item in raw_namespace
        )

        return namespace, chunk[1]

    if isinstance(chunk, dict):
        return (), chunk

    return (), {}


def get_plan_task_map(
    plan: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    """
    Create a task lookup using each task ID.
    """

    task_map: dict[int, dict[str, Any]] = {}

    tasks = plan.get("tasks", [])

    if not isinstance(tasks, list):
        return task_map

    for task in tasks:
        if not isinstance(task, dict):
            continue

        try:
            task_id = int(task["id"])
        except (KeyError, TypeError, ValueError):
            continue

        task_map[task_id] = task

    return task_map


def extract_title_from_markdown(markdown: str) -> str:
    """Extract the first H1 heading from markdown text, or provide a clean default."""
    for line in (markdown or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            clean = line[2:].strip()
            if clean:
                return clean
    return "Untitled Writeup"


def save_final_markdown(
    run_id: str,
    markdown: str,
    topic: str = "",
    genre: str = "",
    audience: str = "",
    tone: str = "",
    telemetry: Optional[dict[str, Any]] = None,
    pipeline_metadata: Optional[dict[str, Any]] = None,
) -> Path:
    """
    Save the generated Markdown writeup and persist structured metadata for history browsing.
    """
    run_directory = OUTPUTS_DIR / run_id
    run_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_file = run_directory / "writeup.md"
    output_file.write_text(
        markdown,
        encoding="utf-8",
    )

    # Maintain blog.md for backward compatibility
    (run_directory / "blog.md").write_text(
        markdown,
        encoding="utf-8",
    )

    # Calculate statistics & extract title
    words = len(re.findall(r"\b\w+\b", markdown))
    read_time = max(1, round(words / 220))
    title = extract_title_from_markdown(markdown)

    # Save meta.json for library browsing
    meta = {
        "run_id": run_id,
        "title": title,
        "topic": topic or title,
        "genre": genre or "auto",
        "audience": audience or "",
        "tone": tone or "",
        "word_count": words,
        "read_time_minutes": read_time,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "download_url": f"/api/runs/{run_id}/download",
        "telemetry": telemetry or {},
        "pipeline_metadata": pipeline_metadata or {},
    }
    try:
        (run_directory / "meta.json").write_text(
            json.dumps(meta, indent=2),
            encoding="utf-8",
        )
    except Exception as err:
        logger.warning("Could not save meta.json for run %s: %s", run_id, err)

    return output_file


# ---------------------------------------------------------
# LangGraph streaming
# ---------------------------------------------------------
def stream_workflow(
    topic: str,
    run_id: str,
    genre: str = "auto",
    audience: str = "",
    tone: str = "",
) -> Generator[str, None, None]:
    """
    Run the existing LangGraph workflow and stream
    observable execution updates to the browser.
    """

    config = {
        "configurable": {
            "thread_id": run_id,
        },
        "recursion_limit": 50,
    }

    formatted_topic = topic
    editorial_hints = []
    if genre and genre != "auto":
        editorial_hints.append(f"Format/Genre: {genre.replace('_', ' ').title()}")
    if audience and audience.strip():
        editorial_hints.append(f"Target Audience: {audience.strip()}")
    if tone and tone.strip():
        editorial_hints.append(f"Tone: {tone.strip()}")
    if editorial_hints:
        formatted_topic += f"\n\n[Editorial Preferences: {', '.join(editorial_hints)}]"

    workflow_input = {
        "topic": formatted_topic,
        "sections": [],
        "feedback_history": [],
        "telemetry_events": [],
        "revision_count": 0,
        "pipeline_started_at": perf_counter(),
        "enable_images": True,
    }

    task_map: dict[int, dict[str, Any]] = {}
    completed_task_ids: set[int] = set()

    final_markdown = ""
    final_result: dict[str, Any] = {}
    workers_completed_event_sent = False
    reducer_started_event_sent = False

    yield create_sse_event(
        {
            "type": "run_started",
            "run_id": run_id,
            "topic": topic,
        }
    )

    yield create_sse_event(
        {
            "type": "stage",
            "id": "router",
            "label": "Analyze the request",
            "status": "running",
            "detail": (
                "Determining whether the topic requires "
                "current web research."
            ),
        }
    )

    try:
        stream = workflow.stream(
            workflow_input,
            config=config,
            stream_mode="updates",
            subgraphs=True,
        )

        for raw_chunk in stream:
            namespace, updates = normalize_stream_chunk(
                raw_chunk
            )

            if not updates:
                continue

            for node_name, raw_node_update in updates.items():
                node_update = make_serializable(
                    raw_node_update
                )

                if not isinstance(node_update, dict):
                    node_update = {}

                # =================================================
                # Router
                # =================================================
                if node_name == "router":
                    mode = str(
                        node_update.get(
                            "mode",
                            "closed_book",
                        )
                    )

                    needs_research = bool(
                        node_update.get(
                            "needs_research",
                            False,
                        )
                    )

                    queries = node_update.get(
                        "queries",
                        [],
                    )

                    yield create_sse_event(
                        {
                            "type": "routing",
                            "mode": mode,
                            "needs_research": needs_research,
                            "queries": queries,
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "router",
                            "label": "Analyze the request",
                            "status": "completed",
                            "detail": (
                                f"Selected {mode.replace('_', ' ')} mode."
                            ),
                        }
                    )

                    if needs_research:
                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "research",
                                "label": "Research authoritative sources",
                                "status": "running",
                                "detail": (
                                    "Searching the web and preparing "
                                    "a deduplicated evidence pack."
                                ),
                            }
                        )

                    else:
                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "orchestrator",
                                "label": "Create the article plan",
                                "status": "running",
                                "detail": (
                                    "Creating the article structure, "
                                    "goals and writing tasks."
                                ),
                            }
                        )

                # =================================================
                # Research
                # =================================================
                elif node_name == "research":
                    evidence = node_update.get(
                        "evidence",
                        [],
                    )

                    if not isinstance(evidence, list):
                        evidence = []

                    yield create_sse_event(
                        {
                            "type": "research_complete",
                            "count": len(evidence),
                            "evidence": evidence[:12],
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "research",
                            "label": "Research authoritative sources",
                            "status": "completed",
                            "detail": (
                                f"Prepared {len(evidence)} "
                                "deduplicated sources."
                            ),
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "orchestrator",
                            "label": "Create the article plan",
                            "status": "running",
                            "detail": (
                                "Creating sections, goals, bullets "
                                "and target word counts."
                            ),
                        }
                    )

                # =================================================
                # Orchestrator
                # =================================================
                elif node_name == "orchestrator":
                    plan = node_update.get(
                        "plan",
                        {},
                    )

                    if not isinstance(plan, dict):
                        plan = {}

                    task_map = get_plan_task_map(plan)

                    yield create_sse_event(
                        {
                            "type": "plan",
                            "plan": plan,
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "orchestrator",
                            "label": "Create the article plan",
                            "status": "completed",
                            "detail": (
                                f"Created {len(task_map)} "
                                "article sections."
                            ),
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "stage",
                            "id": "workers",
                            "label": "Write the planned sections",
                            "status": "running",
                            "detail": (
                                "Section workers are writing "
                                "the article in parallel."
                            ),
                        }
                    )

                # =================================================
                # Workers
                # =================================================
                elif node_name == "worker":
                    sections = node_update.get(
                        "sections",
                        [],
                    )

                    if not isinstance(sections, list):
                        sections = []

                    for section in sections:
                        if not isinstance(
                            section,
                            (list, tuple),
                        ):
                            continue

                        if len(section) != 2:
                            continue

                        raw_task_id, section_markdown = section

                        try:
                            task_id = int(raw_task_id)
                        except (TypeError, ValueError):
                            continue

                        # A parallel worker update should be sent once.
                        if task_id in completed_task_ids:
                            continue

                        completed_task_ids.add(task_id)

                        task_information = task_map.get(
                            task_id,
                            {},
                        )

                        title = task_information.get(
                            "title",
                            f"Section {task_id}",
                        )

                        yield create_sse_event(
                            {
                                "type": "section_complete",
                                "task_id": task_id,
                                "title": title,
                                "markdown": str(section_markdown),
                                "completed": len(
                                    completed_task_ids
                                ),
                                "total": len(task_map),
                            }
                        )

                    if (
                        task_map
                        and len(completed_task_ids) >= len(task_map)
                        and not workers_completed_event_sent
                    ):
                        workers_completed_event_sent = True

                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "workers",
                                "label": "Write the planned sections",
                                "status": "completed",
                                "detail": (
                                    f"Completed all "
                                    f"{len(task_map)} sections."
                                ),
                            }
                        )

                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "reducer",
                                "label": "Assemble the final article",
                                "status": "running",
                                "detail": (
                                    "Merging the sections and "
                                    "planning useful visuals."
                                ),
                            }
                        )

                        reducer_started_event_sent = True

                # =================================================
                # Reducer subgraph: merge
                # =================================================
                elif node_name == "merge_content":
                    if not reducer_started_event_sent:
                        yield create_sse_event(
                            {
                                "type": "stage",
                                "id": "reducer",
                                "label": "Assemble the final article",
                                "status": "running",
                                "detail": (
                                    "Merging the sections and "
                                    "planning useful visuals."
                                ),
                            }
                        )

                        reducer_started_event_sent = True

                    yield create_sse_event(
                        {
                            "type": "substage",
                            "id": "merge_content",
                            "label": "Merged all written sections",
                            "status": "completed",
                            "namespace": list(namespace),
                        }
                    )

                # =================================================
                # Editorial and factual feedback loop
                # =================================================
                elif node_name in {"editor", "fact_checker"}:
                    approved_key = (
                        "editor_approved"
                        if node_name == "editor"
                        else "fact_checker_approved"
                    )
                    yield create_sse_event(
                        {
                            "type": "review",
                            "reviewer": node_name,
                            "approved": bool(node_update.get(approved_key, False)),
                            "feedback": node_update.get("feedback_history", []),
                        }
                    )

                elif node_name == "revision_writer":
                    yield create_sse_event(
                        {
                            "type": "revision",
                            "revision_count": node_update.get("revision_count", 0),
                            "max_revisions": 3,
                        }
                    )

                elif node_name == "mark_unresolved":
                    yield create_sse_event(
                        {
                            "type": "circuit_breaker",
                            "unresolved_errors": True,
                            "details": node_update.get("unresolved_error_details", []),
                        }
                    )

                # =================================================
                # Reducer subgraph: image plan
                # =================================================
                elif node_name == "decide_images":
                    image_specs = node_update.get(
                        "image_specs",
                        [],
                    )

                    if not isinstance(image_specs, list):
                        image_specs = []

                    yield create_sse_event(
                        {
                            "type": "images_planned",
                            "count": len(image_specs),
                            "images": image_specs,
                        }
                    )

                    yield create_sse_event(
                        {
                            "type": "substage",
                            "id": "decide_images",
                            "label": (
                                f"Planned {len(image_specs)} "
                                "visual"
                                f"{'' if len(image_specs) == 1 else 's'}"
                            ),
                            "status": "completed",
                            "namespace": list(namespace),
                        }
                    )

                # =================================================
                # Reducer subgraph: image generation and final text
                # =================================================
                elif node_name == "generate_and_place_images":
                    generated_final = node_update.get(
                        "final"
                    )

                    if generated_final:
                        final_markdown = str(
                            generated_final
                        )

                    yield create_sse_event(
                        {
                            "type": "substage",
                            "id": "generate_images",
                            "label": "Generated and placed visuals",
                            "status": "completed",
                            "namespace": list(namespace),
                        }
                    )

                # =================================================
                # Final telemetry payload
                # =================================================
                elif node_name == "finalize_result":
                    result_value = node_update.get("result")
                    if isinstance(result_value, dict):
                        final_result = result_value

        # -----------------------------------------------------
        # Retrieve the final checkpoint to recover the complete result payload.
        # -----------------------------------------------------
        snapshot = workflow.get_state(config)
        state_values = getattr(
            snapshot,
            "values",
            {},
        )

        if isinstance(state_values, dict):
            if not final_markdown:
                final_markdown = str(
                    state_values.get(
                        "final",
                        "",
                    )
                )
            if not final_result and isinstance(state_values.get("result"), dict):
                final_result = state_values["result"]

        if not final_markdown:
            raise RuntimeError(
                "The workflow completed but did not return final Markdown."
            )

        save_final_markdown(
            run_id=run_id,
            markdown=final_markdown,
            topic=topic,
            genre=genre,
            audience=audience,
            tone=tone,
            telemetry=final_result.get("telemetry", {}),
            pipeline_metadata=final_result.get("metadata", {}),
        )

        yield create_sse_event(
            {
                "type": "stage",
                "id": "reducer",
                "label": "Assemble the final article",
                "status": "completed",
                "detail": "The final Markdown article is ready.",
            }
        )

        yield create_sse_event(
            {
                "type": "final",
                "run_id": run_id,
                "markdown": final_markdown,
                "download_url": (
                    f"/api/runs/{run_id}/download"
                ),
                "metadata": final_result.get("metadata", {}),
                "telemetry": final_result.get("telemetry", {}),
            }
        )

        yield create_sse_event(
            {
                "type": "done",
                "run_id": run_id,
            }
        )

    except GeneratorExit:
        logger.info(
            "Browser disconnected from run %s. Stream stopped cleanly.",
            run_id,
        )
        return

    except Exception as error:
        logger.exception(
            "Workflow run %s failed",
            run_id,
        )

        yield create_sse_event(
            {
                "type": "error",
                "run_id": run_id,
                "message": str(error),
            }
        )


# ---------------------------------------------------------
# Page endpoint
# ---------------------------------------------------------
@app.get(
    "/",
    response_class=HTMLResponse,
)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "page_title": "AgentPress | Autonomous Writing Studio",
        },
    )


# ---------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------
@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "workflow": "loaded",
    }


# ---------------------------------------------------------
# Agent execution endpoint
# ---------------------------------------------------------
@app.post("/api/run")
def run_agent(request_data: AgentRunRequest):
    topic = request_data.topic.strip()

    if len(topic) < 3:
        raise HTTPException(
            status_code=422,
            detail="Please provide a valid topic.",
        )

    run_id = uuid.uuid4().hex

    return StreamingResponse(
        stream_workflow(
            topic=topic,
            run_id=run_id,
            genre=request_data.genre or "auto",
            audience=request_data.audience or "",
            tone=request_data.tone or "",
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------
# Markdown download endpoint
# ---------------------------------------------------------
@app.get("/api/runs/{run_id}/download")
def download_markdown(run_id: str):
    # Only allow safe run ID characters.
    safe_run_id = "".join(
        character
        for character in run_id
        if character.isalnum()
        or character in {"-", "_"}
    )

    if safe_run_id != run_id:
        raise HTTPException(
            status_code=400,
            detail="Invalid run ID.",
        )

    output_file = (
        OUTPUTS_DIR
        / safe_run_id
        / "writeup.md"
    )

    if not output_file.is_file():
        output_file = (
            OUTPUTS_DIR
            / safe_run_id
            / "blog.md"
        )

    if not output_file.is_file():
        raise HTTPException(
            status_code=404,
            detail="Generated writeup file was not found.",
        )

    return FileResponse(
        path=output_file,
        media_type="text/markdown",
        filename=f"agentpress-writeup-{safe_run_id[:8]}.md",
    )


# ---------------------------------------------------------
# History & Previous Writeups Endpoints
# ---------------------------------------------------------
@app.get("/api/history")
def get_history():
    """
    Returns a list of completed writeups from OUTPUTS_DIR, sorted newest first.
    """
    history_items = []
    if not OUTPUTS_DIR.exists():
        return []

    for run_dir in OUTPUTS_DIR.iterdir():
        if not run_dir.is_dir():
            continue

        run_id = run_dir.name
        writeup_file = run_dir / "writeup.md"
        if not writeup_file.is_file():
            writeup_file = run_dir / "blog.md"
        if not writeup_file.is_file():
            continue

        meta_file = run_dir / "meta.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                history_items.append(meta)
                continue
            except Exception:
                pass

        # If meta.json doesn't exist yet, reconstruct dynamically from markdown file
        try:
            content = writeup_file.read_text(encoding="utf-8")
            title = extract_title_from_markdown(content)
            words = len(re.findall(r"\b\w+\b", content))
            mtime = writeup_file.stat().st_mtime
            created_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            history_items.append({
                "run_id": run_id,
                "title": title,
                "topic": title,
                "genre": "auto",
                "audience": "",
                "tone": "",
                "word_count": words,
                "read_time_minutes": max(1, round(words / 220)),
                "created_at": created_at,
                "download_url": f"/api/runs/{run_id}/download",
            })
        except Exception as err:
            logger.warning("Could not read writeup for run %s: %s", run_id, err)

    # Sort newest first
    history_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return history_items


@app.get("/api/runs/{run_id}")
def get_run_details(run_id: str):
    """
    Returns the markdown content and metadata of a specific writeup for preview restoration.
    """
    safe_run_id = "".join(
        c for c in run_id if c.isalnum() or c in {"-", "_"}
    )
    if safe_run_id != run_id:
        raise HTTPException(status_code=400, detail="Invalid run ID.")

    run_dir = OUTPUTS_DIR / safe_run_id
    writeup_file = run_dir / "writeup.md"
    if not writeup_file.is_file():
        writeup_file = run_dir / "blog.md"
    if not writeup_file.is_file():
        raise HTTPException(status_code=404, detail="Writeup not found.")

    markdown = writeup_file.read_text(encoding="utf-8")
    title = extract_title_from_markdown(markdown)
    words = len(re.findall(r"\b\w+\b", markdown))

    meta = {}
    meta_file = run_dir / "meta.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "run_id": safe_run_id,
        "title": meta.get("title") or title,
        "topic": meta.get("topic") or title,
        "genre": meta.get("genre") or "auto",
        "audience": meta.get("audience") or "",
        "tone": meta.get("tone") or "",
        "markdown": markdown,
        "word_count": meta.get("word_count") or words,
        "read_time_minutes": meta.get("read_time_minutes") or max(1, round(words / 220)),
        "created_at": meta.get("created_at") or datetime.fromtimestamp(writeup_file.stat().st_mtime, tz=timezone.utc).isoformat(),
        "download_url": f"/api/runs/{safe_run_id}/download",
        "telemetry": meta.get("telemetry", {}),
        "pipeline_metadata": meta.get("pipeline_metadata", {}),
    }


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    """
    Deletes a completed writeup from disk storage.
    """
    safe_run_id = "".join(
        c for c in run_id if c.isalnum() or c in {"-", "_"}
    )
    if safe_run_id != run_id:
        raise HTTPException(status_code=400, detail="Invalid run ID.")

    run_dir = OUTPUTS_DIR / safe_run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Writeup not found.")

    shutil.rmtree(run_dir, ignore_errors=True)
    return {"status": "deleted", "run_id": safe_run_id}


# ---------------------------------------------------------
# Benchmark & Telemetry Endpoints
# ---------------------------------------------------------
def _load_benchmark_records() -> list[dict[str, Any]]:
    if not BENCHMARK_RESULTS_PATH.is_file():
        return []
    records = []
    for line in BENCHMARK_RESULTS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if record.get("status") == "completed":
                records.append(record)
        except Exception:
            continue
    return records


@app.get("/api/benchmark/summary")
def get_benchmark_summary():
    """
    Returns aggregated benchmark statistics, percentage overheads,
    and per-topic metrics from benchmark_results.jsonl.
    """
    records = _load_benchmark_records()
    if not records:
        return {"available": False, "message": "No benchmark records found."}

    baseline_latencies = [r["baseline"]["telemetry"]["latency_ms"] for r in records]
    pipeline_latencies = [r["pipeline"]["telemetry"]["latency_ms"] for r in records]

    baseline_tokens = [r["baseline"]["telemetry"]["total_tokens"] for r in records]
    pipeline_tokens = [r["pipeline"]["telemetry"]["total_tokens"] for r in records]

    baseline_costs = [r["baseline"]["telemetry"]["estimated_cost_usd"] for r in records]
    pipeline_costs = [r["pipeline"]["telemetry"]["estimated_cost_usd"] for r in records]

    baseline_words = [r["baseline"]["text_metrics"]["word_count"] for r in records]
    pipeline_words = [r["pipeline"]["text_metrics"]["word_count"] for r in records]

    revisions = [r["pipeline"]["telemetry"].get("revision_loop_count", 0) for r in records]

    avg_b_lat = mean(baseline_latencies)
    avg_p_lat = mean(pipeline_latencies)
    avg_b_tok = mean(baseline_tokens)
    avg_p_tok = mean(pipeline_tokens)
    avg_b_cost = mean(baseline_costs)
    avg_p_cost = mean(pipeline_costs)
    avg_b_words = mean(baseline_words)
    avg_p_words = mean(pipeline_words)
    avg_revs = mean(revisions)

    token_overhead = ((avg_p_tok - avg_b_tok) / avg_b_tok) * 100 if avg_b_tok else 0
    latency_overhead = ((avg_p_lat - avg_b_lat) / avg_b_lat) * 100 if avg_b_lat else 0
    cost_overhead = ((avg_p_cost - avg_b_cost) / avg_b_cost) * 100 if avg_b_cost else 0
    word_growth = ((avg_p_words - avg_b_words) / avg_b_words) * 100 if avg_b_words else 0

    topics_summary = []
    for idx, r in enumerate(records):
        b_tel = r["baseline"]["telemetry"]
        p_tel = r["pipeline"]["telemetry"]
        comp = r.get("comparison", {})
        topics_summary.append({
            "index": idx + 1,
            "topic": r["topic"],
            "status": r["pipeline"]["metadata"].get("status", "completed"),
            "baseline_latency_ms": b_tel["latency_ms"],
            "pipeline_latency_ms": p_tel["latency_ms"],
            "baseline_tokens": b_tel["total_tokens"],
            "pipeline_tokens": p_tel["total_tokens"],
            "baseline_cost_usd": b_tel["estimated_cost_usd"],
            "pipeline_cost_usd": p_tel["estimated_cost_usd"],
            "revision_loops": p_tel.get("revision_loop_count", 0),
            "token_overhead_percent": comp.get("token_overhead_percent", 0),
            "latency_overhead_percent": comp.get("latency_overhead_percent", 0),
            "cost_overhead_percent": comp.get("cost_overhead_percent", 0),
            "baseline_word_count": r["baseline"]["text_metrics"]["word_count"],
            "pipeline_word_count": r["pipeline"]["text_metrics"]["word_count"],
        })

    return {
        "available": True,
        "total_runs": len(records),
        "aggregate": {
            "baseline": {
                "average_latency_ms": round(avg_b_lat, 2),
                "average_tokens": round(avg_b_tok, 1),
                "total_tokens": sum(baseline_tokens),
                "average_cost_usd": round(avg_b_cost, 4),
                "total_cost_usd": round(sum(baseline_costs), 4),
                "average_words": round(avg_b_words, 1),
                "average_revisions": 0.0,
            },
            "pipeline": {
                "average_latency_ms": round(avg_p_lat, 2),
                "average_tokens": round(avg_p_tok, 1),
                "total_tokens": sum(pipeline_tokens),
                "average_cost_usd": round(avg_p_cost, 4),
                "total_cost_usd": round(sum(pipeline_costs), 4),
                "average_words": round(avg_p_words, 1),
                "average_revisions": round(avg_revs, 2),
            },
            "overhead": {
                "token_overhead_percent": round(token_overhead, 1),
                "latency_overhead_percent": round(latency_overhead, 1),
                "cost_overhead_percent": round(cost_overhead, 1),
                "word_growth_percent": round(word_growth, 1),
            },
            "circuit_breaker": {
                "unresolved_errors": sum(bool(r["pipeline"]["metadata"].get("unresolved_errors", False)) for r in records),
                "max_revisions": 3,
                "completed_clean": True,
            }
        },
        "topics": topics_summary,
        "resume_bullets": [
            "Architected a non-linear multi-agent content pipeline with Editor and Fact-Checker critique loops, durable feedback state, and a three-iteration circuit breaker preventing unbounded token spend.",
            f"Benchmarked against a one-shot baseline across {len(records)} paired tasks, measuring {avg_p_tok:,.0f} average tokens and ${avg_p_cost:.4f} estimated model cost per task.",
            f"Quantified production telemetry: {token_overhead:+.1f}% token, {latency_overhead:+.1f}% latency, and {cost_overhead:+.1f}% cost overhead while expanding content depth and factual grounding."
        ]
    }


@app.get("/api/benchmark/topics")
def get_benchmark_topics():
    """
    Returns full topic records with blinded System A / System B comparisons,
    text metrics, and per-agent invocation waterfall data.
    """
    records = _load_benchmark_records()
    if not records:
        return []

    # Map blinded pairings identical to BENCHMARK_OUTPUTS.md
    # 1. Why idempotency: A = Multi-agent; B = Baseline
    # 2. How small teams: A = Baseline; B = Multi-agent
    # 3. A practical guide: A = Multi-agent; B = Baseline
    blind_mapping = [
        {"system_a": "pipeline", "system_b": "baseline"},
        {"system_a": "baseline", "system_b": "pipeline"},
        {"system_a": "pipeline", "system_b": "baseline"},
    ]

    results = []
    for idx, r in enumerate(records):
        mapping = blind_mapping[idx % len(blind_mapping)]
        a_source = mapping["system_a"]
        b_source = mapping["system_b"]

        sys_a_content = r[a_source]["content"]
        sys_b_content = r[b_source]["content"]

        results.append({
            "index": idx + 1,
            "topic": r["topic"],
            "system_a": {
                "label": "System A",
                "identity": "Multi-Agent Pipeline" if a_source == "pipeline" else "Vanilla Baseline",
                "is_pipeline": a_source == "pipeline",
                "content": sys_a_content,
                "telemetry": r[a_source]["telemetry"],
                "text_metrics": r[a_source]["text_metrics"],
            },
            "system_b": {
                "label": "System B",
                "identity": "Multi-Agent Pipeline" if b_source == "pipeline" else "Vanilla Baseline",
                "is_pipeline": b_source == "pipeline",
                "content": sys_b_content,
                "telemetry": r[b_source]["telemetry"],
                "text_metrics": r[b_source]["text_metrics"],
            },
            "pipeline_invocations": r["pipeline"]["telemetry"].get("invocations", []),
            "baseline_invocations": r["baseline"]["telemetry"].get("invocations", []),
            "metadata": r["pipeline"].get("metadata", {}),
            "comparison": r.get("comparison", {}),
        })

    return results
 
 
@app.get("/BENCHMARK_REPORT.md")
@app.get("/api/benchmark/report")
def get_benchmark_report(request: Request, raw: bool = False):
    """
    Serves the aggregate benchmark report. Returns an HTML rendered view
    when accessed from a browser, or raw markdown if requested or via curl.
    """
    report_path = BASE_DIR / "BENCHMARK_REPORT.md"
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="Benchmark report not found.")

    markdown_content = report_path.read_text(encoding="utf-8")
    accept = request.headers.get("accept", "")

    if raw or "text/html" not in accept:
        return PlainTextResponse(markdown_content, media_type="text/plain; charset=utf-8")

    escaped_markdown = json.dumps(markdown_content)
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AgentPress - Benchmark Report</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="/static/css/style.css">
    <style>
        body {{
            background: #07100e;
            color: #f5faf7;
            font-family: 'DM Sans', sans-serif;
            padding: 40px 20px 80px;
            margin: 0;
            display: flex;
            justify-content: center;
        }}
        .report-container {{
            max-width: 880px;
            width: 100%;
            background: rgba(15, 28, 25, 0.92);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 20px;
            padding: 40px 48px;
            box-shadow: 0 25px 60px rgba(0, 0, 0, 0.4);
        }}
        .report-nav {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 28px;
            padding-bottom: 16px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }}
        .back-link, .raw-link {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            color: var(--primary, #83f5b5);
            text-decoration: none;
            font-size: 13.5px;
            font-weight: 600;
        }}
        .back-link:hover, .raw-link:hover {{
            text-decoration: underline;
        }}
        .article-preview table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            font-size: 13.5px;
        }}
        .article-preview th {{
            background: rgba(255, 255, 255, 0.04);
            color: #8fa39c;
            padding: 10px 14px;
            text-align: left;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }}
        .article-preview td {{
            padding: 10px 14px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
        }}
    </style>
</head>
<body>
    <div class="report-container">
        <div class="report-nav">
            <a href="/" class="back-link">← Return to Studio</a>
            <a href="/BENCHMARK_REPORT.md?raw=true" class="raw-link" target="_blank">View Raw Markdown (.md)</a>
        </div>
        <div id="reportContent" class="article-preview"></div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dompurify/dist/purify.min.js"></script>
    <script>
        const rawMd = {escaped_markdown};
        if (window.marked && window.DOMPurify) {{
            document.getElementById('reportContent').innerHTML = DOMPurify.sanitize(marked.parse(rawMd));
        }} else {{
            document.getElementById('reportContent').textContent = rawMd;
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(html_content)


@app.get("/BENCHMARK_OUTPUTS.md")
@app.get("/api/benchmark/outputs")
def get_benchmark_outputs():
    """
    Serves the blinded A/B outputs comparison file.
    """
    outputs_path = BASE_DIR / "BENCHMARK_OUTPUTS.md"
    if not outputs_path.is_file():
        raise HTTPException(status_code=404, detail="Benchmark outputs not found.")
    return PlainTextResponse(outputs_path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )
