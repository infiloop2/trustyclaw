"""Runway media generation tool package (Runway Developer API)."""

from __future__ import annotations

import ipaddress
import re
import secrets
import urllib.parse
from contextlib import contextmanager
from typing import BinaryIO, Iterator, cast

from host.tools.json_types import JSONObject
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, DataSummaryPoint, SetupStep, ToolManifest
from host.tools.results import (
    ActionExecuted,
    ActionFailed,
    ActionResult,
    ApprovalResult,
    OpenedStreamingAsset,
    StreamingAsset,
    StreamingAssetError,
)
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.shared.web import WebRequestError, json_request, open_response_stream, stream_request_bytes

# Runway's Developer API is a single Bearer-authenticated JSON surface. Every
# generation is an async task: POST a generation endpoint to get a task id, then
# poll GET /v1/tasks/{id} until a terminal status. One pinned API version rides
# on every request as a header.
RUNWAY_API_BASE = "https://api.dev.runwayml.com"
RUNWAY_API_VERSION = "2024-11-06"
TEXT_TO_VIDEO_ENDPOINT = f"{RUNWAY_API_BASE}/v1/text_to_video"
IMAGE_TO_VIDEO_ENDPOINT = f"{RUNWAY_API_BASE}/v1/image_to_video"
VIDEO_TO_VIDEO_ENDPOINT = f"{RUNWAY_API_BASE}/v1/video_to_video"
TEXT_TO_IMAGE_ENDPOINT = f"{RUNWAY_API_BASE}/v1/text_to_image"
TEXT_TO_SPEECH_ENDPOINT = f"{RUNWAY_API_BASE}/v1/text_to_speech"
TASKS_ENDPOINT = f"{RUNWAY_API_BASE}/v1/tasks"
UPLOADS_ENDPOINT = f"{RUNWAY_API_BASE}/v1/uploads"

MAX_PROMPT_CHARS = 1_000

# Video generation models Runway exposes on text_to_video / image_to_video.
# Runway is now a model aggregator, so this spans first-party Gen-4 models,
# Google Veo, and ByteDance Seedance 2 (the successor to the Seedance models
# this tool used directly before). gen4_turbo is image-to-video only.
SUPPORTED_VIDEO_MODELS = (
    "gen4.5",
    "gen4_turbo",
    "veo3.1",
    "veo3.1_fast",
    "seedance2",
    "seedance2_fast",
)
IMAGE_ONLY_VIDEO_MODELS = frozenset({"gen4_turbo"})
DEFAULT_TEXT_MODEL = "gen4.5"
DEFAULT_IMAGE_MODEL = "gen4_turbo"

# The video-editing model (Aleph 2) drives the video_to_video endpoint; it edits
# an existing video from an instruction prompt rather than generating from
# scratch.
EDIT_MODEL = "aleph2"
IMAGE_MODEL = "gpt_image_2"
SPEECH_MODEL = "eleven_multilingual_v2"

IMAGE_RATIOS = ("1920:1920", "1920:1280", "1280:1920")
DEFAULT_IMAGE_RATIO = "1920:1920"
IMAGE_QUALITIES = ("low", "medium", "high", "auto")
DEFAULT_IMAGE_QUALITY = "low"
SPEECH_VOICES = (
    "Maya", "Arjun", "Serene", "Bernard", "Billy", "Mark", "Clint", "Mabel",
    "Chad", "Leslie", "Eleanor", "Elias", "Elliot", "Grungle", "Brodie", "Sandra",
    "Kirk", "Kylie", "Lara", "Lisa", "Malachi", "Marlene", "Martin", "Miriam",
    "Monster", "Paula", "Pip", "Rusty", "Ragnar", "Xylar", "Maggie", "Jack",
    "Katie", "Noah", "James", "Rina", "Ella", "Mariah", "Frank", "Claudia", "Niki",
    "Vincent", "Kendrick", "Myrna", "Tom", "Wanda", "Benjamin", "Kiana", "Rachel",
)
DEFAULT_SPEECH_VOICE = "Maya"

# Keep one ratio contract that every exposed video model accepts. Runway exposes
# more model-specific ratios, but admitting those here would require a second
# matrix in the tool schema that can drift independently from provider support.
SUPPORTED_RATIOS = (
    "1280:720",
    "720:1280",
)
DEFAULT_RATIO = "1280:720"
DEFAULT_DURATION_SECONDS = 5
VEO_DURATION_SECONDS = frozenset({4, 6, 8})
VIDEO_DURATION_RANGES = {
    "gen4.5": (2, 10),
    "gen4_turbo": (2, 10),
    "seedance2": (4, 15),
    "seedance2_fast": (4, 15),
}

TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

RUNWAY_GENERATE_POLICY = (
    "The prompt (and optional first-frame image URL or image from the agent workspace) supplied by the user or "
    "agent is sent to Runway's Developer API to render a video, billed as "
    "credits against the deployment's Runway organization. This action runs "
    "directly with no approval; it publishes nothing anywhere, and the result is a "
    "task id returned to active model context to poll with get_task."
)
RUNWAY_EDIT_POLICY = (
    "The source video and editing-instruction prompt supplied by the user or "
    "agent are sent to Runway's Developer API (Aleph 2) to render an "
    "edited video, billed as credits against the deployment's Runway "
    "organization. This action runs directly with no approval; it publishes "
    "nothing anywhere; the result is a task id returned to active model context to poll with get_task."
)
RUNWAY_IMAGE_POLICY = (
    "The image prompt and rendering parameters supplied by the user or agent are sent "
    "to Runway's Developer API and forwarded by Runway to OpenAI's GPT Image 2. The generation is billed as "
    "Runway credits. This action runs directly with no approval and publishes nothing; "
    "it returns a task id to active model context to poll with get_task."
)
RUNWAY_SPEECH_POLICY = (
    "The speech text and selected Runway voice preset supplied by the user or agent are "
    "sent to Runway's Developer API and forwarded by Runway to ElevenLabs Multilingual v2. The generation "
    "is billed as Runway credits. This action runs directly with no approval and publishes "
    "nothing; it returns a task id to active model context to poll with get_task."
)
RUNWAY_POLL_POLICY = (
    "Read-only poll. Sends only the task id to Runway's Developer API and "
    "returns the task status and, once finished, a temporary download URL for "
    "the generated video, image, or audio into active model context. Runs directly with no approval."
)
RUNWAY_SAVE_VIDEO_POLICY = (
    "Read-only handoff. Sends the task id to Runway, downloads the completed video from "
    "Runway's authoritative temporary output URL, and streams it through the agent-side "
    "bridge into a host-generated path under /tool_assets in the agent workspace."
)


class ToolInputValidationError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


RUNWAY_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}


MANIFEST = ToolManifest(
    tool_id="runway",
    display_name="Runway Media Generation",
    description="Connect Runway and let your agent generate images, speech, and short videos, and edit videos.",
    connection="enable_only",
    actions=(
        ActionSpec(
            id="generate_video",
            description=(
                "Start an async Runway video generation task from a text prompt and optional "
                "first-frame image from a public URL or the agent workspace. Returns a "
                "task_id to poll with get_task; renders "
                "typically take one to three minutes. This runs immediately, spends Runway "
                "credits, and creates no public post."
            ),
            data_policy=RUNWAY_GENERATE_POLICY,
            input_schema={
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string", "description": "What to render (up to 1000 chars)."},
                    "model": {
                        "type": "string",
                        "enum": list(SUPPORTED_VIDEO_MODELS),
                        "description": "Default: gen4.5 (or gen4_turbo when image_url or image_asset_id is set). gen4_turbo is image-to-video only.",
                    },
                    "image_url": {"type": "string", "description": "Optional public HTTPS image URL used as the first frame (image-to-video)."},
                    "image_asset_id": {"type": "string", "description": "Built-in reference for a JPEG, PNG, or WebP from the agent workspace. Use at most one of image_url or image_asset_id."},
                    "ratio": {"type": "string", "enum": list(SUPPORTED_RATIOS), "description": "Output aspect ratio, e.g. 1280:720 (default) or 720:1280."},
                    "duration_seconds": {"type": "string", "description": "Video length in seconds. Gen-4 models accept 2-10 (default 5), Seedance accepts 4-15 (default 5), and Veo accepts only 4, 6, or 8 (default 4)."},
                    "seed": {"type": "string", "description": "Optional integer seed for reproducible output."},
                },
                "additionalProperties": False,
            },
            output_schema=RUNWAY_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="edit_video",
            description=(
                "Start an async Runway video-editing task (Aleph 2): restyle or modify an "
                "existing video from a public HTTPS URL or the agent workspace. Returns a "
                "task_id to poll with get_task. A workspace video is uploaded to Runway only "
                "when this action runs. This spends Runway credits "
                "and creates no public post."
            ),
            data_policy=RUNWAY_EDIT_POLICY,
            input_schema={
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "video_url": {"type": "string", "description": "Public HTTPS URL of the source video to edit."},
                    "video_asset_id": {"type": "string", "description": "Built-in reference for an MP4 or MOV from the agent workspace. Use exactly one of video_url or video_asset_id."},
                    "prompt": {"type": "string", "description": "The editing instruction (up to 1000 chars), e.g. 'make it night time'."},
                    "seed": {"type": "string", "description": "Optional integer seed for reproducible output."},
                },
                "additionalProperties": False,
            },
            output_schema=RUNWAY_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="generate_image",
            description=(
                "Start an async GPT Image 2 text-to-image task through Runway and return a "
                "task_id. Poll get_task with output_kind=image for the temporary image URL. "
                "This runs immediately, spends Runway credits, and publishes nothing."
            ),
            data_policy=RUNWAY_IMAGE_POLICY,
            input_schema={
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string", "description": "What to render (up to 1000 chars)."},
                    "ratio": {"type": "string", "enum": list(IMAGE_RATIOS), "description": "Output resolution: square 1920:1920 (default), landscape 1920:1280, or portrait 1280:1920."},
                    "quality": {"type": "string", "enum": list(IMAGE_QUALITIES), "description": "Rendering quality (default low); higher quality spends more Runway credits."},
                },
                "additionalProperties": False,
            },
            output_schema=RUNWAY_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="generate_speech",
            description=(
                "Start an async ElevenLabs Multilingual v2 text-to-speech task through Runway "
                "and return a task_id. Poll get_task with output_kind=audio for the temporary "
                "audio URL. This runs immediately, spends Runway credits, and publishes nothing."
            ),
            data_policy=RUNWAY_SPEECH_POLICY,
            input_schema={
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Words to speak, up to 1000 characters."},
                    "voice": {"type": "string", "enum": list(SPEECH_VOICES), "description": "Runway's ElevenLabs voice preset (default Maya)."},
                },
                "additionalProperties": False,
            },
            output_schema=RUNWAY_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="get_task",
            description="Poll a task_id returned by any Runway generation action. Set output_kind to the originating action's media type; pending tasks have no output, while success returns a temporary video, image, or audio URL valid about 24-48 hours.",
            data_policy=RUNWAY_POLL_POLICY,
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {"type": "string", "description": "Runway task id returned when generation or editing starts."},
                    "output_kind": {"type": "string", "enum": ["video", "image", "audio"], "description": "Originating action's output type (default video); controls whether success returns video_url, image_url, or audio_url."},
                },
                "additionalProperties": False,
            },
            output_schema=RUNWAY_OUTPUT_SCHEMA,
        ),
        ActionSpec(
            id="save_video",
            description=(
                "Save a completed Runway video under /tool_assets in the agent workspace. "
                "The agent-side bridge creates the filename and returns the durable path."
            ),
            data_policy=RUNWAY_SAVE_VIDEO_POLICY,
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {"type": "string", "description": "Completed Runway video task id."},
                },
                "additionalProperties": False,
            },
            output_schema=RUNWAY_OUTPUT_SCHEMA,
        ),
    ),
    config=(ConfigRequirement(key="RUNWAY_API_SECRET", description="Runway Developer API key (org-scoped) from the dev.runwayml.com dashboard."),),
    protections=(
        "Your Runway key stays in write-only tool config. Inputs are bounded, and local images and videos are uploaded to Runway only when used as inputs.",
        "Generation is billed to your Runway organization. TrustyClaw does not publish the media. A completed video can be saved from Runway's authoritative temporary URL into the agent workspace for durable operator review and later approval-gated publishing.",
    ),
    setup_steps=(
        SetupStep(
            title="Create a Runway developer account",
            description="Open dev.runwayml.com, sign in, and create or select the developer organization that should own all agent-generated media. Confirm the organization name before funding it; Developer API credits, keys, tasks, and billing are separate from Runway's consumer web-application plan and credits.",
            link_url="https://dev.runwayml.com/",
            link_label="Open the Runway developer portal",
        ),
        SetupStep(
            title="Fund the API organization and create a key",
            description="In the developer portal, add API credits to the selected organization, open its API Keys area, create a clearly named organization-scoped secret, and copy it immediately to a password manager or TrustyClaw. Generation, editing, GPT Image, and ElevenLabs speech actions spend the same Runway organization balance; web-app credits do not cover these calls.",
            link_url="https://docs.dev.runwayml.com/guides/using-the-api/",
            link_label="View Runway's API guide",
        ),
        SetupStep(
            title="Configure and enable Runway",
            show_config=True,
            description="Expand Runway Media Generation in Internet Access and Tools, save the developer secret as RUNWAY_API_SECRET, then enable the tool. There is no OAuth or separate OpenAI/ElevenLabs key. Never place the Runway key in a prompt, source URL, or media filename.",
        ),
    ),
    data_summary=DataSummary(
        cards=(
            DataSummaryCard(
                title="What leaves this host",
                points=(
                    DataSummaryPoint(label="Generation requests", text="The prompt or speech text, generation options (model, ratio, duration, quality, voice, seed), and any public image or video URL given as input go to Runway."),
                    DataSummaryPoint(label="Workspace media", text="When an image or video from the agent workspace is used as an input, its bytes and original filename upload to Runway. Its local workspace path is not sent."),
                ),
            ),
            DataSummaryCard(
                title="Where it can go",
                points=(
                    DataSummaryPoint(label="Runway models", text="Every request first goes to Runway. Gen-4.5, Gen-4 Turbo, and Aleph 2 generations use Runway's own models."),
                    DataSummaryPoint(label="Third-party video models", text="When the agent explicitly selects Google Veo 3.1 or ByteDance Seedance 2, Runway sends that provider the prompt, output ratio and duration, optional seed, and any first-frame image. TrustyClaw does not let Runway silently choose one of these models."),
                    DataSummaryPoint(label="Image and speech models", text="For image generation, Runway sends the prompt, ratio, and quality to OpenAI's GPT Image 2. For speech generation, Runway sends the speech text and selected voice to ElevenLabs Multilingual v2."),
                ),
            ),
            DataSummaryCard(
                title="What Runway can do with it",
                description=(
                    "Runway treats prompts and media as user content: its terms let it review Inputs and Outputs and use them to "
                    "train and improve Runway models under a broad license, alongside service operation, research, analytics, "
                    "vendor processing, safety, and legal uses. Ordinary API accounts have no self-service training opt-out. "
                    "Enterprise accounts use separate negotiated terms; Runway says its third-party model providers do not train "
                    "on Enterprise customer inputs or outputs, but the treatment of Runway's own models depends on that contract."
                ),
                links=(
                    DataSummaryLink(label="Runway privacy policy", url="https://runwayml.com/privacy-policy"),
                    DataSummaryLink(label="Runway terms of use", url="https://runwayml.com/terms-of-use"),
                    DataSummaryLink(label="Runway data security", url="https://runwayml.com/data-security"),
                    DataSummaryLink(label="Runway Enterprise third-party model policy", url="https://help.runwayml.com/hc/en-us/articles/51248305153683-Enterprise-FAQ-Third-party-Models-in-Runway"),
                ),
            ),
            DataSummaryCard(
                title="How long Runway retains it",
                description=(
                    "Runway keeps content as long as necessary for its stated purposes, with no fixed period. Generated output "
                    "URLs are temporary and typically expire within 24 to 48 hours."
                ),
                links=(
                    DataSummaryLink(label="Runway privacy policy", url="https://runwayml.com/privacy-policy"),
                ),
            ),
        ),
    ),
)


def _duration_seconds(tool_input: JSONObject, model: str) -> int:
    value = tool_input.get("duration_seconds")
    if value is None:
        return 4 if model in {"veo3.1", "veo3.1_fast"} else DEFAULT_DURATION_SECONDS
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        digits = value.strip()
        if len(digits) > 2:
            raise ToolInputValidationError(
                "Runway tool_input.duration_seconds is outside the supported range."
            )
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputValidationError("Runway tool_input.duration_seconds must be an integer or digit string.")
    if model in {"veo3.1", "veo3.1_fast"}:
        if value not in VEO_DURATION_SECONDS:
            raise ToolInputValidationError("Runway Veo duration_seconds must be 4, 6, or 8.")
        return value
    low, high = VIDEO_DURATION_RANGES[model]
    if not low <= value <= high:
        raise ToolInputValidationError(
            f"Runway {model} duration_seconds must be between {low} and {high}."
        )
    return value


def _optional_seed(tool_input: JSONObject) -> int | None:
    value = tool_input.get("seed")
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        digits = value.strip()
        if len(digits.lstrip("-")) > 10:
            raise ToolInputValidationError(
                "Runway tool_input.seed must be between 0 and 4294967295."
            )
        value = int(digits)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputValidationError("Runway tool_input.seed must be an integer or digit string.")
    if not 0 <= value <= 4_294_967_295:
        raise ToolInputValidationError(
            "Runway tool_input.seed must be between 0 and 4294967295."
        )
    return value


def _string_choice(tool_input: JSONObject, key: str, allowed: tuple[str, ...], default: str) -> str:
    value = tool_input.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputValidationError(f"Runway tool_input.{key} must be one of {', '.join(allowed)}.")
    return value


def _prompt_text(tool_input: JSONObject) -> str:
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolInputValidationError("Runway tool_input.prompt is required.")
    prompt = prompt.strip()
    # Reject rather than truncate: a silently cut prompt would spend credits on a
    # render of an input the agent did not ask for.
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ToolInputValidationError(f"Runway prompt must be at most {MAX_PROMPT_CHARS} characters.")
    return prompt


def _https_url(tool_input: JSONObject, key: str) -> str:
    value = tool_input.get(key)
    if not isinstance(value, str):
        raise ToolInputValidationError(f"Runway tool_input.{key} must be an https URL.")
    value = value.strip()
    if len(value) > 4_096:
        raise ToolInputValidationError(f"Runway tool_input.{key} must be an https URL.")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ToolInputValidationError(f"Runway tool_input.{key} must be an https URL.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ToolInputValidationError(f"Runway tool_input.{key} must be an https URL.")
    return value


def _generation_request(
    tool_input: JSONObject, staged_image_uri: str | None = None
) -> tuple[str, JSONObject]:
    """Build the (endpoint, body) for generate_video, routing to image- or
    text-to-video by whether a first-frame image was supplied."""
    extra = set(tool_input) - {
        "prompt", "model", "image_url", "image_asset_id", "ratio", "duration_seconds", "seed"
    }
    if extra:
        raise ToolInputValidationError(
            "Runway generate_video only supports prompt, model, image_url, image_asset_id, ratio, duration_seconds, and seed."
        )
    prompt = _prompt_text(tool_input)
    has_url = tool_input.get("image_url") is not None
    has_asset = tool_input.get("image_asset_id") is not None
    if has_url and has_asset:
        raise ToolInputValidationError(
            "Runway generate_video supports at most one of image_url or image_asset_id."
        )
    prompt_image: str | None = None
    if has_url:
        prompt_image = _https_url(tool_input, "image_url")
    elif has_asset:
        asset_id = tool_input.get("image_asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise ToolInputValidationError(
                "Runway tool_input.image_asset_id must be a non-empty string."
            )
        prompt_image = staged_image_uri or "runway://pending"
    has_image = prompt_image is not None
    default_model = DEFAULT_IMAGE_MODEL if has_image else DEFAULT_TEXT_MODEL
    model = _string_choice(tool_input, "model", SUPPORTED_VIDEO_MODELS, default_model)
    if not has_image and model in IMAGE_ONLY_VIDEO_MODELS:
        raise ToolInputValidationError(
            f"Runway model {model} is image-to-video only; supply image_url or image_asset_id, or pick another model."
        )
    ratio = _string_choice(tool_input, "ratio", SUPPORTED_RATIOS, DEFAULT_RATIO)
    duration = _duration_seconds(tool_input, model)
    seed = _optional_seed(tool_input)
    body: JSONObject = {"model": model, "promptText": prompt, "ratio": ratio, "duration": duration}
    endpoint = TEXT_TO_VIDEO_ENDPOINT
    if prompt_image is not None:
        endpoint = IMAGE_TO_VIDEO_ENDPOINT
        body["promptImage"] = prompt_image
    if seed is not None:
        body["seed"] = seed
    return endpoint, body


def _edit_request(tool_input: JSONObject, video_uri: str | None = None) -> JSONObject:
    extra = set(tool_input) - {"video_url", "video_asset_id", "prompt", "seed"}
    if extra:
        raise ToolInputValidationError(
            "Runway edit_video only supports video_url, video_asset_id, prompt, and seed."
        )
    has_url = tool_input.get("video_url") is not None
    has_asset = tool_input.get("video_asset_id") is not None
    if has_url == has_asset:
        raise ToolInputValidationError(
            "Runway edit_video requires exactly one of video_url or video_asset_id."
        )
    if video_uri is None:
        video_uri = _https_url(tool_input, "video_url")
    prompt = _prompt_text(tool_input)
    seed = _optional_seed(tool_input)
    body: JSONObject = {"model": EDIT_MODEL, "videoUri": video_uri, "promptText": prompt}
    if seed is not None:
        body["seed"] = seed
    return body


def _multipart_parts(
    fields: JSONObject,
    *,
    filename: str,
    media_type: str,
    boundary: str,
) -> tuple[bytes, bytes]:
    chunks: list[bytes] = []
    for name, value in fields.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise RuntimeError("Runway upload initialization returned invalid form fields.")
        if any(ord(character) < 32 or ord(character) == 127 for character in name + value):
            raise RuntimeError("Runway upload initialization returned invalid form fields.")
        safe_name = name.replace('"', "")
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise RuntimeError("Runway staged filename contains invalid control characters.")
    safe_filename = filename.replace('"', "")
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode("utf-8")
    )
    return b"".join(chunks), f"\r\n--{boundary}--\r\n".encode("ascii")


def _multipart_stream(prefix: bytes, source: BinaryIO, suffix: bytes) -> Iterator[bytes]:
    yield prefix
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        yield chunk
    yield suffix


def _upload_staged_asset(
    asset_id: str, headers: dict[str, str], api: HostAPI, *, kind: str
) -> str:
    metadata = api.assets.describe(asset_id)
    if not metadata.media_type.startswith(f"{kind}/"):
        raise ToolInputValidationError(
            f"Runway {kind}_asset_id does not refer to a staged {kind}."
        )
    initialized = json_request(
        "POST",
        UPLOADS_ENDPOINT,
        headers=headers,
        body={"filename": metadata.filename, "type": "ephemeral"},
        failure_message=f"Runway {kind} upload initialization failed.",
        invalid_response_message=f"Runway {kind} upload initialization returned an invalid response.",
    )
    upload_url = initialized.get("uploadUrl")
    runway_uri = initialized.get("runwayUri")
    fields = initialized.get("fields")
    if not isinstance(upload_url, str) or not _is_https_runway_url(upload_url):
        raise RuntimeError(f"Runway {kind} upload initialization returned no HTTPS upload URL.")
    if (
        not isinstance(runway_uri, str)
        or len(runway_uri) > 2_048
        or not runway_uri.startswith("runway://")
    ):
        raise RuntimeError(f"Runway {kind} upload initialization returned no runway URI.")
    if not isinstance(fields, dict):
        raise RuntimeError(f"Runway {kind} upload initialization returned invalid form fields.")
    boundary = f"trustyclaw-{secrets.token_hex(16)}"
    prefix, suffix = _multipart_parts(
        cast(JSONObject, fields),
        filename=metadata.filename,
        media_type=metadata.media_type,
        boundary=boundary,
    )
    with api.assets.open(asset_id) as source:
        stream_request_bytes(
            "POST",
            upload_url,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            body=_multipart_stream(prefix, source, suffix),
            content_length=len(prefix) + metadata.size_bytes + len(suffix),
            failure_message=f"Runway {kind} upload failed.",
            timeout=120,
        )
    return runway_uri


def _image_request(tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"prompt", "ratio", "quality"}
    if extra:
        raise ToolInputValidationError("Runway generate_image only supports prompt, ratio, and quality.")
    return {
        "model": IMAGE_MODEL,
        "promptText": _prompt_text(tool_input),
        "ratio": _string_choice(tool_input, "ratio", IMAGE_RATIOS, DEFAULT_IMAGE_RATIO),
        "quality": _string_choice(tool_input, "quality", IMAGE_QUALITIES, DEFAULT_IMAGE_QUALITY),
        "outputCount": 1,
    }


def _speech_request(tool_input: JSONObject) -> JSONObject:
    extra = set(tool_input) - {"text", "voice"}
    if extra:
        raise ToolInputValidationError("Runway generate_speech only supports text and voice.")
    text = tool_input.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolInputValidationError("Runway tool_input.text is required.")
    text = text.strip()
    if len(text) > MAX_PROMPT_CHARS:
        raise ToolInputValidationError(f"Runway speech text must be at most {MAX_PROMPT_CHARS} characters.")
    voice = _string_choice(tool_input, "voice", SPEECH_VOICES, DEFAULT_SPEECH_VOICE)
    return {
        "model": SPEECH_MODEL,
        "promptText": text,
        "voice": {"type": "runway-preset", "presetId": voice},
    }


def _task_result(response: JSONObject, output_kind: str = "video") -> JSONObject:
    task_id = response.get("id")
    task_status = response.get("status")
    result: JSONObject = {
        "status": "success_executed",
        "task_id": task_id if isinstance(task_id, str) else "",
        "task_status": task_status if isinstance(task_status, str) else "unknown",
    }
    if task_status == "SUCCEEDED":
        output = response.get("output")
        output_url = ""
        if isinstance(output, list) and output and isinstance(output[0], str):
            output_url = output[0]
        if output_url and _is_https_output_url(output_url):
            result[f"{output_kind}_url"] = output_url
            result["message"] = (
                f"Generation succeeded. The {output_kind}_url is a temporary link valid for about "
                "24-48 hours; download or hand off the URL promptly."
            )
        else:
            result["message"] = "Runway reported success but returned no output URL. Submit a new task."
    elif task_status == "FAILED":
        failure_code = response.get("failureCode")
        code = f" (code: {failure_code})" if isinstance(failure_code, str) and failure_code else ""
        result["message"] = f"Runway generation failed{code}. Submit a new task."
    elif task_status == "CANCELLED":
        result["message"] = "Runway task was cancelled. Submit a new task."
    elif task_status == "THROTTLED":
        result["message"] = (
            "Runway queued the task (concurrency limit reached). Poll get_task again in ~15 seconds."
        )
    else:
        progress = response.get("progress")
        percent = ""
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            percent = f" ({round(progress * 100)}%)"
        result["message"] = f"Runway task is {result['task_status']}{percent}. Poll get_task again in ~15 seconds."
    return result


def _is_https_output_url(value: str) -> bool:
    return _is_https_runway_url(value)


def _is_https_runway_url(value: str) -> bool:
    if len(value) > 2_048:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname or ""
    try:
        ipaddress.ip_address(hostname)
        return False
    except ValueError:
        pass
    return (
        parsed.scheme == "https"
        and "." in hostname
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )


def _failure_from_status(exc: WebRequestError) -> str:
    if exc.status == 401:
        return "Runway rejected the configured API key."
    if exc.status == 403:
        return "Runway denied the request (insufficient permissions for this model or action)."
    if exc.status == 404:
        return "Runway task was not found."
    if exc.status == 429:
        return "Runway rate limit or daily generation quota was reached."
    if exc.status in {400, 422}:
        return "Runway rejected the request. Check that the model, prompt, aspect ratio, and duration are compatible."
    if exc.status:
        return f"Runway API returned HTTP {exc.status}."
    return "Runway API request failed."


def _save_video(task_id: str, headers: dict[str, str]) -> ActionResult:
    if not TASK_ID_RE.fullmatch(task_id):
        raise ToolInputValidationError("Runway tool_input.task_id is invalid.")
    response = json_request(
        "GET",
        f"{TASKS_ENDPOINT}/{urllib.parse.quote(task_id, safe='')}",
        headers=headers,
        failure_message="Runway task lookup failed.",
        invalid_response_message="Runway returned an invalid task response.",
    )
    if response.get("status") != "SUCCEEDED":
        return ActionFailed("Runway video is not complete. Poll get_task and try again after it succeeds.")
    output = response.get("output")
    output_url = output[0] if isinstance(output, list) and output and isinstance(output[0], str) else ""
    if not output_url or not _is_https_output_url(output_url):
        return ActionFailed("Runway reported success but returned no valid video URL.")
    @contextmanager
    def open_video() -> Iterator[OpenedStreamingAsset]:
        try:
            with open_response_stream(
                "GET", output_url, failure_message="Runway video download failed.", timeout=120
            ) as (source, response_headers):
                raw_length = response_headers.get("content-length", "")
                if not raw_length.isascii() or not raw_length.isdecimal():
                    raise StreamingAssetError(
                        "Runway video download did not include a valid size."
                    )
                size_bytes = int(raw_length)
                if not 512 <= size_bytes <= 200_000_000:
                    raise StreamingAssetError(
                        "Runway video download size is outside the supported range."
                    )
                media_type = (
                    response_headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                suffixes = {"video/mp4": ".mp4", "video/quicktime": ".mov"}
                suffix = suffixes.get(media_type)
                if suffix is None:
                    raise StreamingAssetError(
                        "Runway video download returned an unsupported media type."
                    )
                yield OpenedStreamingAsset(
                    filename=f"runway-{task_id}{suffix}",
                    media_type=media_type,
                    size_bytes=size_bytes,
                    source=source,
                )
        except StreamingAssetError:
            raise
        except WebRequestError as exc:
            raise StreamingAssetError(_failure_from_status(exc)) from exc
        except ValueError as exc:
            raise StreamingAssetError(str(exc) or "Runway video download failed.") from exc

    return StreamingAsset(open_video)


class RunwayTool:
    @property
    def manifest(self) -> ToolManifest:
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def _create_task(
        self, endpoint: str, body: JSONObject, headers: dict[str, str], model: str, output_kind: str
    ) -> ActionResult:
        response = json_request(
            "POST",
            endpoint,
            headers=headers,
            body=body,
            failure_message="Runway API request failed.",
            invalid_response_message="Runway API returned an invalid response.",
        )
        task_id = response.get("id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            return ActionFailed("Runway API returned no task id.")
        return ActionExecuted(
            {
                "status": "success_executed",
                "message": f"Runway task created. Poll get_task with output_kind={output_kind} until it succeeds.",
                "task_id": task_id,
                "task_status": "PENDING",
                "model": model,
                "output_kind": output_kind,
            }
        )

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            api_key = api.config["RUNWAY_API_SECRET"]
            headers = {
                "authorization": f"Bearer {api_key}",
                "x-runway-version": RUNWAY_API_VERSION,
            }
            if action == "generate_video":
                asset_id = tool_input.get("image_asset_id")
                endpoint, body = _generation_request(tool_input)
                if asset_id is None:
                    return self._create_task(endpoint, body, headers, cast(str, body["model"]), "video")
                runway_uri = _upload_staged_asset(cast(str, asset_id), headers, api, kind="image")
                endpoint, body = _generation_request(tool_input, runway_uri)
                result = self._create_task(endpoint, body, headers, cast(str, body["model"]), "video")
                if isinstance(result, ActionExecuted):
                    api.assets.delete(cast(str, asset_id))
                return result
            if action == "edit_video":
                asset_id = tool_input.get("video_asset_id")
                if asset_id is None:
                    body = _edit_request(tool_input)
                    return self._create_task(
                        VIDEO_TO_VIDEO_ENDPOINT, body, headers, EDIT_MODEL, "video"
                    )
                if not isinstance(asset_id, str):
                    raise ToolInputValidationError(
                        "Runway tool_input.video_asset_id must be a string."
                    )
                # Validate the full request shape before uploading, so a bad
                # input never streams the staged video to Runway first.
                _edit_request(tool_input, "runway://pending")
                runway_uri = _upload_staged_asset(asset_id, headers, api, kind="video")
                body = _edit_request(tool_input, runway_uri)
                result = self._create_task(
                    VIDEO_TO_VIDEO_ENDPOINT, body, headers, EDIT_MODEL, "video"
                )
                # Delete only after Runway confirms a task id. A malformed
                # success response keeps the source available for a clean retry.
                if isinstance(result, ActionExecuted):
                    api.assets.delete(asset_id)
                return result
            if action == "save_video":
                if set(tool_input) != {"task_id"} or not isinstance(tool_input.get("task_id"), str):
                    raise ToolInputValidationError(
                        "Runway save_video requires exactly one string task_id."
                    )
                return _save_video(cast(str, tool_input["task_id"]), headers)
            if action == "generate_image":
                body = _image_request(tool_input)
                return self._create_task(TEXT_TO_IMAGE_ENDPOINT, body, headers, IMAGE_MODEL, "image")
            if action == "generate_speech":
                body = _speech_request(tool_input)
                return self._create_task(TEXT_TO_SPEECH_ENDPOINT, body, headers, SPEECH_MODEL, "audio")
            if action == "get_task":
                extra = set(tool_input) - {"task_id", "output_kind"}
                if extra:
                    raise ToolInputValidationError("Runway get_task only supports task_id and output_kind.")
                task_id_value = tool_input.get("task_id")
                if not isinstance(task_id_value, str) or not TASK_ID_RE.fullmatch(task_id_value.strip()):
                    raise ToolInputValidationError("Runway tool_input.task_id must be a valid task id string.")
                response = json_request(
                    "GET",
                    f"{TASKS_ENDPOINT}/{task_id_value.strip()}",
                    headers=headers,
                    failure_message="Runway API request failed.",
                    invalid_response_message="Runway API returned an invalid response.",
                )
                output_kind = _string_choice(tool_input, "output_kind", ("video", "image", "audio"), "video")
                return ActionExecuted(_task_result(response, output_kind))
            return ActionFailed("Unsupported Runway action.")
        except ToolInputValidationError as exc:
            return ActionFailed(exc.message)
        except WebRequestError as exc:
            return ActionFailed(_failure_from_status(exc))
        except (ValueError, RuntimeError) as exc:
            # The tool's own errors (validation, config-unset) carry curated,
            # secret-free messages; an unexpected exception must not leak its
            # raw text (e.g. internal filesystem paths) to the agent.
            return ActionFailed(str(exc) or "Runway tool request failed.")
        except Exception:
            return ActionFailed("Runway tool request failed.")

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("Runway has no approval-gated actions.")


# The instance the host discovers (see host.runtime.tools.tools_host).
BUNDLED_TOOL = RunwayTool()
