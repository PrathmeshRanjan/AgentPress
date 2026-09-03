from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
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
        }
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
    }

    task_map: dict[int, dict[str, Any]] = {}
    completed_task_ids: set[int] = set()

    final_markdown = ""
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
                # Root reducer update
                # =================================================
                elif node_name == "reducer":
                    generated_final = node_update.get(
                        "final"
                    )

                    if generated_final:
                        final_markdown = str(
                            generated_final
                        )

        # -----------------------------------------------------
        # Retrieve the final checkpoint when the root reducer
        # update did not contain the complete final output.
        # -----------------------------------------------------
        if not final_markdown:
            snapshot = workflow.get_state(config)

            state_values = getattr(
                snapshot,
                "values",
                {},
            )

            if isinstance(state_values, dict):
                final_markdown = str(
                    state_values.get(
                        "final",
                        "",
                    )
                )

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




if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )