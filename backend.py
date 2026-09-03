"""
AgentPress - Autonomous Long-Form Writing Studio & Editorial Engine
===================================================================

Architecture Overview:
----------------------
This module implements an Orchestrator-Worker (Map-Reduce) editorial agent with LangGraph.
It can research, plan, write, and visually illustrate in-depth writeups, essays, and long-form
articles across any domain (technology, thought leadership, case studies, personal narratives,
comprehensive guides, opinion essays, etc.).

Workflow Stages:
1. router_node:
   - Evaluates the writeup topic, tone, and audience.
   - Decides if web research is required ('closed_book', 'hybrid', or 'open_book').
   - Generates targeted web search queries if research is needed.

2. research_node (Conditional):
   - Executes multi-query search via the Tavily Search API.
   - Extracts, validates, and deduplicates high-signal EvidenceItem objects using an LLM extractor.

3. orchestrator_node:
   - Synthesizes the topic, evidence, audience, and format into a structured outline (Plan).
   - Breaks down the writeup into 5-8 concrete sections (Task objects) with goals and bullet points.

4. fanout (Dynamic Conditional Edge):
   - Spawns parallel worker nodes using LangGraph's dynamic `Send` primitive (one worker per section).

5. worker_node (Parallel Map Phase):
   - Writes each section in an authentic, engaging, human voice (no robotic AI clichés).
   - Employs tone, concrete examples, and real citations (where evidence is provided).
   - Returns indexed tuples `(task.id, section_md)` to guarantee deterministic ordering.

6. reducer (Compiled Subgraph):
   - merge_content: Stitches all sections together in strict task ID order.
   - decide_images: Technical/editorial review to decide where diagrams, illustrations,
     or visual aids materially enhance comprehension (max 3, using placeholders [[IMAGE_1]]).
   - generate_and_place_images: Generates images via Gemini (gemini-2.5-flash-image),
     caches them locally, and inserts Markdown image tags with graceful error handling.
"""

from __future__ import annotations

import os
import re
import operator
from pathlib import Path
from typing import TypedDict, List, Annotated, Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
import psycopg
from psycopg.rows import dict_row

from langchain.chat_models import init_chat_model
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_tavily import TavilySearch
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.memory import MemorySaver

# Load environment variables (.env)
load_dotenv()

# Automatically sanitize environment variables (Docker --env-file passes literal quotes)
for env_key in [
    "MISTRAL_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "TAVILY_API_KEY",
    "DATABASE_URL",
    "MODEL_NAME",
]:
    raw_val = os.environ.get(env_key)
    if raw_val:
        os.environ[env_key] = raw_val.strip().strip("'\"").strip()

# Synchronize GOOGLE_API_KEY and GEMINI_API_KEY
if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
if os.environ.get("GOOGLE_API_KEY") and not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]

DATABASE_URL = os.getenv("DATABASE_URL", "")


# ============================================================================
# 1. Pydantic Schemas (Contracts for Structured LLM Outputs)
# ============================================================================

class Task(BaseModel):
    """Defines a single section/chapter within the planned writeup."""
    id: int = Field(..., description="Unique 1-based sequential index.")
    title: str = Field(..., description="Evocative, authentic section heading.")
    section_type: str = Field(
        default="body",
        description="Role: 'hook', 'context', 'argument', 'breakdown', 'tutorial_step', 'reflection', 'synthesis'."
    )
    goal: str = Field(..., description="1-sentence clear purpose/outcome of this section.")
    bullets: List[str] = Field(default_factory=list, description="3-6 concrete takeaways, narrative points, or arguments.")
    target_words: int = Field(default=350, description="Target word count for this section (120 - 550 words).")
    requires_research: bool = Field(default=False, description="True if fresh factual claims or external events are used.")
    requires_citations: bool = Field(default=False, description="True if specific source links must be cited.")
    requires_code: bool = Field(default=False, description="True only if this section requires code snippets or technical syntax.")

    @field_validator("bullets", mode="before")
    @classmethod
    def sanitize_bullets(cls, v):
        if not v:
            return []
        if isinstance(v, str):
            return [v.strip()]
        cleaned: List[str] = []
        if isinstance(v, list):
            for item in v:
                if isinstance(item, list):
                    # Flatten nested lists emitted by LLM
                    for sub in item:
                        if sub is not None:
                            cleaned.append(str(sub).strip())
                elif isinstance(item, dict):
                    val = item.get("text") or item.get("point") or item.get("bullet") or str(item)
                    cleaned.append(str(val).strip())
                elif item is not None:
                    cleaned.append(str(item).strip())
        return [b for b in cleaned if b]


class Plan(BaseModel):
    """The master writeup blueprint generated by the orchestrator."""
    blog_title: str = Field(..., description="Captivating, thoughtful, and authentic writeup title.")
    audience: str = Field(default="Curious generalists", description="Target audience.")
    tone: str = Field(default="Conversational and engaging", description="Voice and style.")
    blog_kind: str = Field(default="explainer", description="The genre and format of the writeup.")
    constraints: List[str] = Field(default_factory=list, description="Specific stylistic or structural constraints.")
    tasks: List[Task] = Field(..., description="Ordered list of sections that make up the writeup.")

    @field_validator("constraints", mode="before")
    @classmethod
    def sanitize_constraints(cls, v):
        if not v:
            return []
        if isinstance(v, str):
            return [v.strip()]
        cleaned: List[str] = []
        if isinstance(v, list):
            for item in v:
                if isinstance(item, list):
                    for sub in item:
                        if sub is not None:
                            cleaned.append(str(sub).strip())
                elif item is not None:
                    cleaned.append(str(item).strip())
        return [c for c in cleaned if c]


class RouterDecision(BaseModel):
    """Routing evaluation determining whether web research is necessary."""
    needs_research: bool = Field(..., description="Whether external web search is needed before planning.")
    mode: Literal["closed_book", "hybrid", "open_book"] = Field(
        default="closed_book",
        description="Research mode: closed_book (evergreen), hybrid (needs recent examples), or open_book (recent events/pricing/rankings).",
    )
    queries: List[str] = Field(default_factory=list, description="3-10 focused web search queries if research is needed.")

    @field_validator("queries", mode="before")
    @classmethod
    def sanitize_queries(cls, v):
        if not v:
            return []
        if isinstance(v, str):
            return [v.strip()]
        cleaned: List[str] = []
        if isinstance(v, list):
            for item in v:
                if isinstance(item, list):
                    for sub in item:
                        if sub is not None:
                            cleaned.append(str(sub).strip())
                elif item is not None:
                    cleaned.append(str(item).strip())
        return [q for q in cleaned if q]


class EvidenceItem(BaseModel):
    """A validated, single piece of research evidence extracted from web search."""
    title: str = Field(..., description="Source page title.")
    url: str = Field(..., description="Direct link to the source.")
    published_at: Optional[str] = Field(default=None, description="Publication date as YYYY-MM-DD if available.")
    snippet: Optional[str] = Field(default=None, description="Concise summary of relevant facts.")
    source: Optional[str] = Field(default=None, description="Domain or publisher name.")


class EvidencePack(BaseModel):
    """A collection of synthesized evidence items returned by the research extractor."""
    evidence: List[EvidenceItem] = Field(default_factory=list)


class ImageSpec(BaseModel):
    """Specification for an inline image or diagram."""
    placeholder: str = Field(..., description="Unique placeholder tag in the text, e.g. [[IMAGE_1]].")
    filename: str = Field(..., description="Target file name under images/, e.g., 'workflow_architecture.png'.")
    alt: str = Field(..., description="Accessible alt text describing what the image depicts.")
    caption: str = Field(..., description="Informative, engaging caption displayed beneath the image.")
    prompt: str = Field(..., description="Detailed generative prompt sent to the image model.")
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = "1024x1024"
    quality: Literal["low", "medium", "high"] = "medium"


class GlobalImagePlan(BaseModel):
    """Editorial plan specifying image locations and descriptions across the full post."""
    md_with_placeholders: str = Field(..., description="The complete blog markdown with [[IMAGE_X]] tags placed contextually.")
    images: List[ImageSpec] = Field(default_factory=list, description="Specs for up to 3 high-impact images or diagrams.")


# ============================================================================
# 2. Graph State Definition
# ============================================================================
# Note: TypedDict is used for the LangGraph State channel because it supports
# partial dictionary updates from nodes, zero runtime validation overhead,
# and native integration with reducers via Annotated[..., operator.add].

class State(TypedDict):
    topic: str

    # Routing & Research channels
    mode: str
    needs_research: bool
    queries: List[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]

    # Parallel Worker channels
    # NOTE: Using tuple[int, str] with operator.add enables concurrent workers to append
    # their outputs safely, while allowing reducer to sort deterministically by task.id.
    sections: Annotated[List[tuple[int, str]], operator.add]

    # Reducer & Multimodal channels
    merged_md: str
    md_with_placeholders: str
    image_specs: List[dict]
    final: str


# ============================================================================
# 2. LLM Initialization with Seamless 429 Rate-Limit Fallback to Gemini
# ============================================================================

# Primary Chat LLM (Mistral Small)
primary_model = init_chat_model("mistralai:mistral-small-latest")

# Fallback Chat LLM (Google Gemini 2.5 Flash for 429 Rate-Limit Exceeded & Provider Errors)
try:
    gemini_fallback = init_chat_model("google_genai:gemini-2.5-flash")
    # Wrap primary with Gemini fallback: automatically activates on any 429, rate limit, or provider error
    model = primary_model.with_fallbacks([gemini_fallback])
    print("[Notice] AgentPress LLM configured with automatic Gemini 2.5 Flash fallback for 429 rate limits.")
except Exception as e:
    print(f"[Warning] Could not initialize Gemini fallback model: {e}")
    model = primary_model


# ============================================================================
# 3. Router Node (Adaptive Topic Categorization)
# ============================================================================

ROUTER_SYSTEM = """You are an expert editorial strategist and research director for an intelligent writing studio.
Your role is to analyze the user's writeup topic, requested tone, and audience, and determine
whether live web research is needed BEFORE architecting the outline.

Categories:
1. closed_book (needs_research=false):
   - Evergreen subjects, fundamental concepts, philosophies, creative writing, or foundational knowledge
     where accuracy does not depend on recent real-world events or volatile tools.
2. hybrid (needs_research=true):
   - Topics that discuss lasting concepts or frameworks but require fresh real-world examples,
     recent tool releases, current case studies, or up-to-date benchmarks to be high-signal.
3. open_book (needs_research=true):
   - Highly dynamic or time-sensitive topics: weekly/monthly news roundups, "this year/latest",
     pricing models, product comparisons, recent regulatory changes, or trending events.

If needs_research=true:
- Formulate 3–8 sharp, targeted, high-signal search queries.
- Avoid lazy or overly broad queries (e.g. do not just search "coffee" or "AI").
- Include temporal constraints in the queries if the topic implies recent developments.
"""

def router_node(state: State) -> dict:
    """Evaluates whether the blog topic requires web research and generates search queries."""
    topic = state["topic"]
    decider = model.with_structured_output(RouterDecision)
    decision = decider.invoke(
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(content=f"Topic: {topic}"),
        ]
    )

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
    }


def route_next(state: State) -> str:
    """Conditional edge routing to 'research' if research is required, otherwise straight to 'orchestrator'."""
    return "research" if state["needs_research"] else "orchestrator"


# ============================================================================
# 4. Research Node (Search Execution & Fact Synthesis)
# ============================================================================

def _tavily_search(query: str, max_results: int = 5) -> List[dict]:
    """Helper to query Tavily Search API and normalize the response format."""
    tool = TavilySearch(max_results=max_results)
    results = tool.invoke({"query": query})

    if isinstance(results, dict):
        items = results.get("results", [])
    elif isinstance(results, list):
        items = results
    else:
        items = []

    normalized: List[dict] = []
    for r in items:
        if isinstance(r, dict):
            normalized.append(
                {
                    "title": r.get("title") or "",
                    "url": r.get("url") or "",
                    "snippet": r.get("content") or r.get("snippet") or "",
                    "published_at": r.get("published_date") or r.get("published_at"),
                    "source": r.get("source"),
                }
            )
    return normalized


RESEARCH_SYSTEM = """You are an objective research synthesizer for a premier publishing outlet.
Your task is to take raw web search results and extract a clean, authoritative, deduplicated list of EvidenceItem objects.

Guidelines:
- Only include results that have a valid, accessible URL.
- Favor trustworthy, primary, and reputable sources (official documentation, company announcements,
  established industry analyses, expert blogs).
- If an exact publication date is present in the payload, preserve it as YYYY-MM-DD. Never hallucinate or guess dates.
- Extract crisp, information-dense snippets that capture facts, figures, and direct takeaways.
- Deduplicate sources pointing to the exact same URL.
"""

def research_node(state: State) -> dict:
    """Executes search queries, filters noise, and extracts structured evidence."""
    queries = state.get("queries", []) or []
    max_results = 5
    raw_results: List[dict] = []

    for q in queries:
        try:
            raw_results.extend(_tavily_search(q, max_results=max_results))
        except Exception as e:
            print(f"[Warning] Web search failed for query '{q}': {e}")

    if not raw_results:
        return {"evidence": []}

    try:
        extractor = model.with_structured_output(EvidencePack)
        pack = extractor.invoke(
            [
                SystemMessage(content=RESEARCH_SYSTEM),
                HumanMessage(content=f"Raw web search results:\n{raw_results[:20]}"),
            ]
        )
        evidence_items = pack.evidence or []
    except Exception as e:
        print(f"[Warning] Structured evidence extraction fallback triggered: {e}")
        evidence_items = []
        for r in raw_results:
            if r.get("url"):
                evidence_items.append(
                    EvidenceItem(
                        title=r.get("title") or "Authoritative Source",
                        url=r["url"],
                        published_at=r.get("published_at"),
                        snippet=r.get("snippet") or "",
                        source=r.get("source"),
                    )
                )

    # Deduplicate items by URL to avoid repetitive citations
    dedup: dict[str, EvidenceItem] = {}
    for e in evidence_items:
        if e.url:
            dedup[e.url] = e

    return {"evidence": list(dedup.values())}


# ============================================================================
# 5. Orchestrator Node (Outline Planning)
# ============================================================================

ORCH_SYSTEM = """You are a master editor-in-chief and publishing strategist for AgentPress.
Your mission is to architect a compelling, logically structured outline for an in-depth writeup
that resonates deeply with readers and fits the requested topic and format.

Broad Format Adaptability:
- This writeup can be of ANY format: technical deep-dive, personal story/narrative, thought leadership,
  opinion essay, practical how-to guide, case study, critique, or cultural exploration.
- Adapt the structure, tone, and goals to the specific subject:
  * For personal/narrative writeups: Focus on storytelling beats, tension, pivotal choices, and hard-won reflections.
  * For thought leadership/opinion: Focus on strong thesis, counterarguments, original perspectives, and future outlook.
  * For tutorials/guides: Focus on clarity, progressive disclosure, practical examples, and common pitfalls.
  * For technical writeups: Include architecture intuitions, code/design trade-offs, and edge cases.

Outline Requirements:
- Design 5–8 focused sections (tasks) that take the reader on a cohesive, satisfying journey.
- Each task must contain:
  1) goal: A 1-sentence outcome describing what the reader gains, understands, or feels from this section.
  2) 3–6 bullets: Concrete, non-overlapping arguments, narrative beats, or actionable insights.
  3) target_words: Word count allocation between 120 and 550 words.
  4) flags: Set `requires_code=True` ONLY when actual code is needed. Set `requires_citations=True`
     when citing external studies, releases, or claims.

Human Voice Mandate:
- Frame titles and goals with natural curiosity and warmth.
- Avoid robotic, formulaic titles like "Introduction to X", "Overview of X", or "Conclusion".
  Instead use evocative titles that reflect the core idea of that section (e.g., "The Breaking Point",
  "Why Most Teams Stumble Here", "The Unspoken Trade-off").
- Output must strictly match the Plan schema.
"""

def orchestrator_node(state: State) -> dict:
    """Produces the structured writeup outline (Plan) using available topic and research evidence."""
    planner = model.with_structured_output(Plan)

    evidence = state.get("evidence", [])
    mode = state.get("mode", "closed_book")

    plan = planner.invoke(
        [
            SystemMessage(content=ORCH_SYSTEM),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Research Mode: {mode}\n\n"
                    f"Available Evidence (use where relevant; may be empty):\n"
                    f"{[e.model_dump() for e in evidence][:16]}"
                )
            ),
        ]
    )

    return {"plan": plan}


# ============================================================================
# 6. Fan-Out & Parallel Workers (Map Phase)
# ============================================================================

def fanout(state: State) -> list[Send]:
    """
    Dynamic conditional edge: Spawns parallel worker nodes for each task in the plan.
    Uses LangGraph's Send primitive to pass isolated state payloads to each worker.
    """
    plan = state["plan"]
    assert plan is not None, "Plan must be populated before fanout."

    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "topic": state["topic"],
                "mode": state.get("mode", "closed_book"),
                "plan": plan.model_dump(),
                "evidence": [e.model_dump() for e in state.get("evidence", [])],
            },
        )
        for task in plan.tasks
    ]


WORKER_SYSTEM = """You are an exceptional essayist, storyteller, and domain expert writer.
Write ONE complete, polished section of an in-depth writeup in Markdown.

CRITICAL MANDATE: NATURAL, ENGAGING HUMAN VOICE
- Write like a thoughtful human expert having an engaging, high-context conversation with an intelligent peer.
- Rhythm & Cadence (Burstiness): Vary your sentence lengths intentionally. Use short, punchy statements
  to emphasize key thoughts. Follow them with richer, nuanced sentences that unpack details.
- Conversational Warmth & Directness: Use active voice. Ground abstract ideas in vivid analogies,
  practical scenarios, or relatable real-world tension.
- STRICT PROHIBITION ON AI-ESE CLICHÉS:
  Do NOT use the following tired words and phrases:
  * "delve", "tapestry", "beacon", "testament", "crucial", "paramount", "furthermore", "moreover"
  * "in conclusion", "it is important to remember", "realm", "ever-evolving", "game-changer", "landscape"
  * "pivotal", "plethora", "nestled", "unleash", "in today's fast-paced world"
  Express yourself with fresh, precise vocabulary and authentic human phrasing instead.

Section Construction Rules:
- Start immediately with a '## <Section Title>' heading. Do NOT output a top-level '# Writeup Title' H1.
- Honor the provided Section Goal and address ALL listed bullets in order without skipping or lumping them together.
- Word Count: Stay close to the specified Target Words (±15%).
- Formatting: Use clean markdown, short paragraphs (2-4 sentences), selective bullet lists, and bold text for key insights.
- Code: If requires_code is true, provide clean, idiomatic, minimal code blocks with syntax highlighting.
- Evidence & Citations:
  * If mode == 'open_book' or requires_citations == true: Back external claims with source links from the
    provided Evidence URLs using markdown: ([Source Name](URL)).
  * Do NOT invent external links. If a fact is unverified by evidence, frame it as an observation or omit the citation.
- No meta commentary: Output strictly the section content in Markdown.
"""

def worker_node(payload: dict) -> dict:
    """Executes an individual section generation task in parallel."""
    task = Task(**payload["task"])
    plan = Plan(**payload["plan"])
    evidence = [EvidenceItem(**e) for e in payload.get("evidence", [])]
    topic = payload["topic"]
    mode = payload.get("mode", "closed_book")

    bullets_text = "\n- " + "\n- ".join(task.bullets)

    evidence_text = ""
    if evidence:
        evidence_text = "\n".join(
            f"- {e.title} | {e.url} | {e.published_at or 'date:unknown'} | {e.snippet or ''}".strip()
            for e in evidence[:20]
        )

    section_md = model.invoke(
        [
            SystemMessage(content=WORKER_SYSTEM),
            HumanMessage(
                content=(
                    f"Blog Title: {plan.blog_title}\n"
                    f"Genre: {plan.blog_kind}\n"
                    f"Target Audience: {plan.audience}\n"
                    f"Tone: {plan.tone}\n"
                    f"Constraints: {plan.constraints}\n"
                    f"Topic: {topic}\n"
                    f"Research Mode: {mode}\n\n"
                    f"--- CURRENT SECTION ---\n"
                    f"Section Index: {task.id}\n"
                    f"Section Title: {task.title}\n"
                    f"Section Role: {task.section_type}\n"
                    f"Goal: {task.goal}\n"
                    f"Target Words: {task.target_words}\n"
                    f"Requires Code: {task.requires_code}\n"
                    f"Requires Citations: {task.requires_citations}\n"
                    f"Key Bullets to cover:\n{bullets_text}\n\n"
                    f"Available Verified Evidence (cite with [Source](URL) where relevant):\n{evidence_text}\n"
                )
            ),
        ]
    ).content.strip()

    # Return indexed tuple so the reducer can sort deterministically
    return {"sections": [(task.id, section_md)]}


# ============================================================================
# 7. Reducer Subgraph (Assembly, Visual Planning & Gemini Generation)
# ============================================================================

def _sanitize_filename(title: str, max_length: int = 80) -> str:
    """Converts a blog title into a safe, filesystem-friendly markdown filename."""
    clean = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_').lower()
    return (clean[:max_length] if clean else "blog_post") + ".md"


def merge_content(state: State) -> dict:
    """
    Stitches all parallel worker outputs in strict task ID order.
    Eliminates race conditions where workers completing out of order could scramble the article.
    """
    plan = state["plan"]
    assert plan is not None, "Plan must be present to merge content."

    # Sort sections deterministically by task.id (item 0 in tuple)
    ordered_sections = [md for _, md in sorted(state["sections"], key=lambda x: x[0])]
    body = "\n\n".join(ordered_sections).strip()
    merged_md = f"# {plan.blog_title}\n\n{body}\n"

    return {"merged_md": merged_md}


DECIDE_IMAGES_SYSTEM = """You are an expert creative director and editorial illustrator for AgentPress.
Review the complete writeup draft and decide where visual aids, diagrams, or illustrations
will materially elevate the reader's understanding and aesthetic engagement.

Visual Strategy Guidelines:
- Maximum 3 images per writeup. Only propose images where they add genuine value.
- Style adaptation based on format and domain:
  * Technical / Data: Clean architecture flows, process diagrams, system blueprints, or data visual comparisons.
  * Story / Narrative / Essay: Evocative conceptual illustrations, cinematic scene visuals, or atmospheric sketches.
  * Business / Guides: Clear frameworks, timeline roadmaps, decision trees, or visual checklists.
  * Culture / Opinion: Stylized editorial photography or contextual visual compositions.
- Contextual Placement:
  * Place [[IMAGE_1]], [[IMAGE_2]], [[IMAGE_3]] placeholders on their own line immediately after
    the paragraph where the concept is introduced.
  * Never cluster all images at the very top or bottom.
- If no images are warranted, return `images=[]` and keep `md_with_placeholders` identical to the input.
- Image Prompts:
  * Write clear, evocative prompts for modern diffusion/generative models.
  * Specify art style, composition, lighting, and elements clearly. Avoid generating tiny unreadable text.

Output strictly as GlobalImagePlan.
"""

def decide_images(state: State) -> dict:
    """Analyzes the full draft and determines strategic image prompts and placeholder positions."""
    planner = model.with_structured_output(GlobalImagePlan)
    merged_md = state["merged_md"]
    plan = state["plan"]
    assert plan is not None

    image_plan = planner.invoke(
        [
            SystemMessage(content=DECIDE_IMAGES_SYSTEM),
            HumanMessage(
                content=(
                    f"Blog Genre: {plan.blog_kind}\n"
                    f"Audience: {plan.audience}\n"
                    f"Tone: {plan.tone}\n"
                    f"Topic: {state['topic']}\n\n"
                    f"Full Blog Draft:\n\n{merged_md}"
                )
            ),
        ]
    )

    return {
        "md_with_placeholders": image_plan.md_with_placeholders,
        "image_specs": [img.model_dump() for img in image_plan.images],
    }


def _gemini_generate_image_bytes(prompt: str) -> bytes:
    """
    Calls the Google GenAI SDK to generate image bytes via gemini-2.5-flash-image.
    Requires `google-genai` package and `GOOGLE_API_KEY` set in environment.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("google-genai library is not installed. Install via `pip install google-genai`.") from exc

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Neither GOOGLE_API_KEY nor GEMINI_API_KEY environment variable is set.")

    client = genai.Client(api_key=api_key)

    resp = client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            safety_settings=[
                types.SafetySetting(
                    category="HARM_CATEGORY_DANGEROUS_CONTENT",
                    threshold="BLOCK_ONLY_HIGH",
                )
            ],
        ),
    )

    parts = getattr(resp, "parts", None)
    if not parts and getattr(resp, "candidates", None):
        try:
            parts = resp.candidates[0].content.parts
        except Exception:
            parts = None

    if not parts:
        raise RuntimeError("No image content returned from Gemini API.")

    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline and getattr(inline, "data", None):
            return inline.data

    raise RuntimeError("No inline image data found in Gemini response.")


def generate_and_place_images(state: State) -> dict:
    """
    Generates requested images, caches them locally in './images/', replaces placeholders,
    and saves the finished Markdown document to disk.
    """
    plan = state["plan"]
    assert plan is not None

    md = state.get("md_with_placeholders") or state["merged_md"]
    image_specs = state.get("image_specs", []) or []
    base_dir = Path(__file__).resolve().parent

    clean_title = re.sub(r'[^\w\s-]', '', plan.blog_title).strip()
    clean_title = re.sub(r'[-\s]+', '_', clean_title) or "writeup"
    safe_filename = f"{clean_title}.md"
    out_file = base_dir / safe_filename

    # If no images were planned, export the text directly
    if not image_specs:
        out_file.write_text(md, encoding="utf-8")
        return {"final": md}

    images_dir = base_dir / "images"
    images_dir.mkdir(exist_ok=True)

    for spec in image_specs:
        placeholder = spec["placeholder"]
        raw_filename = spec.get("filename", "illustration.png")
        # Ensure filename is safe and has .png extension
        clean_name = re.sub(r'[^\w\.-]', '', Path(raw_filename).name) or "image.png"
        out_path = images_dir / clean_name

        # Local caching: Skip generation if the image was already generated previously
        if not out_path.exists():
            try:
                img_bytes = _gemini_generate_image_bytes(spec["prompt"])
                out_path.write_bytes(img_bytes)
            except Exception as e:
                # Graceful degradation: If image generation fails (rate limit, missing key, etc.),
                # preserve the text and embed a styled callout block instead of halting the entire pipeline.
                callout = (
                    f"\n> 🖼️ **[Visual Note]** {spec.get('caption', '')}\n>\n"
                    f"> *Alt:* {spec.get('alt', '')}\n>\n"
                    f"> *Illustration Concept:* {spec.get('prompt', '')}\n"
                )
                md = md.replace(placeholder, callout)
                continue

        # Replace placeholder with standard Markdown image syntax and caption
        img_md = f"\n![{spec['alt']}](images/{clean_name})\n*{spec['caption']}*\n"
        md = md.replace(placeholder, img_md)

    out_file.write_text(md, encoding="utf-8")
    return {"final": md}


# ============================================================================
# 8. Graph Construction & Compilation
# ============================================================================

# Step A: Build the Reducer Subgraph
# Encapsulates text merging, image planning, and generation into a modular sub-pipeline.
reducer_graph = StateGraph(State)
reducer_graph.add_node("merge_content", merge_content)
reducer_graph.add_node("decide_images", decide_images)
reducer_graph.add_node("generate_and_place_images", generate_and_place_images)

reducer_graph.add_edge(START, "merge_content")
reducer_graph.add_edge("merge_content", "decide_images")
reducer_graph.add_edge("decide_images", "generate_and_place_images")
reducer_graph.add_edge("generate_and_place_images", END)
reducer_subgraph = reducer_graph.compile()

# Step B: Build the Main Graph
g = StateGraph(State)
g.add_node("router", router_node)
g.add_node("research", research_node)
g.add_node("orchestrator", orchestrator_node)
g.add_node("worker", worker_node)
g.add_node("reducer", reducer_subgraph)

# Step C: Define Control Flow & Edges
g.add_edge(START, "router")
g.add_conditional_edges(
    "router",
    route_next,
    {"research": "research", "orchestrator": "orchestrator"},
)
g.add_edge("research", "orchestrator")
g.add_conditional_edges("orchestrator", fanout, ["worker"])
g.add_edge("worker", "reducer")
g.add_edge("reducer", END)


# Step D: Resilient Checkpointer Setup
# For FastAPI streaming servers, an in-memory checkpointer is rock-solid and eliminates
# SSL connection timeouts, dead pool sockets, and external database latency during runs.
# Deliverables (writeup.md and images) are persisted to disk automatically upon completion.
USE_POSTGRES_CHECKPOINTER = os.getenv("USE_POSTGRES_CHECKPOINTER", "false").lower() in ("1", "true", "yes")

checkpointer = None
if USE_POSTGRES_CHECKPOINTER and DATABASE_URL:
    try:
        from psycopg_pool import ConnectionPool
        pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={"autocommit": True},
            check=ConnectionPool.check_connection,
        )
        checkpointer = PostgresSaver(pool)
        checkpointer.setup()
        print("[Notice] Using PostgreSQL checkpointer with ConnectionPool.")
    except Exception as err:
        print(f"[Warning] PostgreSQL checkpointer setup failed ({err}). Falling back to MemorySaver.")
        checkpointer = MemorySaver()
else:
    # MemorySaver is zero-latency, thread-safe, and never closes SSL connections
    checkpointer = MemorySaver()

# Final Compiled Agent Workflow
workflow = g.compile(checkpointer=checkpointer)