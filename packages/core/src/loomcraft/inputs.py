"""Structured file requests: how an agent asks the user for what's missing.

When an agent discovers it cannot proceed without files, it must not guess and it
must not free-text the ask.  It publishes a validated
:class:`FileInputRequest` — typed slots with labels, descriptions, accepted
extensions, and cardinality — and the turn ends.  The broker then refuses every
plan or execution action until the request is fulfilled or cancelled, so a
half-informed run can't start behind the user's back.

The interesting part is :func:`allocate_uploads`: given a request and whatever
the user actually uploaded, decide which file fills which slot.  Naive
first-match assignment fails on the common case (two slots, one accepting
``.csv`` only, one accepting ``.csv`` or ``.tsv``, and the ``.csv`` grabbed by
the wrong slot).  This uses augmenting-path matching so a slot can hand back a
file it took earlier when doing so lets everything fit, with filename similarity
as a tie-break — the same allocation the browser shows before upload.
"""

from __future__ import annotations

import secrets
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import InputFulfillmentError, InputRequestError

REQUEST_ID_PATTERN = r"^input-[0-9a-f]{16}$"
KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class FileRequirement(BaseModel):
    """One bounded, user-facing file slot."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(pattern=KEY_PATTERN)
    label: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)
    required: bool = True
    min_files: int = Field(default=1, ge=0, le=12)
    max_files: int = Field(default=1, ge=1, le=12)
    allowed_extensions: list[str] = Field(default_factory=list, max_length=32)
    #: Column/field names the agent expects to find inside the file. Rendered as
    #: hints so the user can check before uploading a 2 GB file.
    field_hints: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("allowed_extensions")
    @classmethod
    def _valid_extensions(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            extension = value.strip().lower()
            if (
                not extension.startswith(".")
                or not 2 <= len(extension) <= 32
                or any(
                    not (char.isalnum() or char in {".", "_", "-", "+"})
                    for char in extension[1:]
                )
            ):
                raise ValueError("invalid file extension")
            if extension in normalized:
                raise ValueError("duplicate file extension")
            normalized.append(extension)
        return normalized

    @field_validator("field_hints")
    @classmethod
    def _valid_hints(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            hint = value.strip()
            if not hint or len(hint) > 200:
                raise ValueError("field hints must contain 1..200 characters")
            if hint in normalized:
                raise ValueError("duplicate field hint")
            normalized.append(hint)
        return normalized

    @model_validator(mode="after")
    def _valid_cardinality(self) -> "FileRequirement":
        if self.required and self.min_files < 1:
            raise ValueError("a required requirement must have min_files >= 1")
        if not self.required and self.min_files != 0:
            raise ValueError("an optional requirement must have min_files = 0")
        if self.max_files < self.min_files:
            raise ValueError("max_files must be >= min_files")
        return self

    def accepts(self, filename: str) -> bool:
        if not self.allowed_extensions:
            return True
        lowered = filename.lower()
        return any(lowered.endswith(ext) for ext in self.allowed_extensions)


class FileInputRequest(BaseModel):
    """A published request that pauses the turn until the user responds."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=2000)
    requirements: list[FileRequirement] = Field(min_length=1, max_length=24)
    #: What to send back to the agent once files arrive. Hosts should *not*
    #: forward this verbatim as if the user typed it — see docs/03.
    continue_prompt: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _unique_keys(self) -> "FileInputRequest":
        keys = [requirement.key for requirement in self.requirements]
        if len(keys) != len(set(keys)):
            raise ValueError("file requirement keys must be unique")
        return self


def _public_validation_summary(error: Exception) -> str:
    errors_method = getattr(error, "errors", None)
    if not callable(errors_method):
        return "invalid file input request"
    try:
        rows = errors_method()
    except Exception:
        return "invalid file input request"
    hints: list[str] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        location = row.get("loc") or ()
        rendered = ""
        for part in location if isinstance(location, (tuple, list)) else ():
            rendered = (
                f"{rendered}[{part}]"
                if isinstance(part, int)
                else f"{rendered}.{part}"
                if rendered
                else str(part)
            )
        message = str(row.get("msg") or "invalid value")
        if message.startswith("Value error, "):
            message = message[len("Value error, ") :]
        hints.append(f"{rendered or 'request'}: {message}")
    return ("; ".join(hints) or "invalid file input request")[:800]


def validate_input_request(raw: object) -> dict[str, Any]:
    """Validate a model-authored request and stamp a server-owned request id.

    The id is generated here, never accepted from the model: a request id is the
    handle the UI uses to fulfil or cancel, and a model-chosen one could collide
    with or impersonate an earlier request.
    """
    if not isinstance(raw, dict):
        raise InputRequestError("file input request must be an object")
    if "request_id" in raw:
        raise InputRequestError("request_id is generated by the server")
    try:
        request = FileInputRequest.model_validate(
            {**raw, "request_id": f"input-{secrets.token_hex(8)}"}
        )
    except Exception as exc:
        raise InputRequestError(
            str(exc), public_message=_public_validation_summary(exc)
        ) from exc
    return request.model_dump(mode="json")


def _semantic_score(requirement: FileRequirement, upload: Mapping[str, Any]) -> int:
    """How strongly a filename suggests it belongs to this slot."""
    filename = str(upload.get("filename", "")).lower()
    key = requirement.key.lower()
    tokens = [
        token
        for token in key.replace("-", "_").split("_")
        if len(token) >= 3 and token not in {"file", "data"}
    ]
    score = 100 if key in filename else 0
    score += sum(20 for token in tokens if token in filename)
    label_tokens = [
        token for token in requirement.label.lower().split() if len(token) >= 4
    ]
    score += sum(5 for token in label_tokens if token in filename)
    return score


def _distinct(uploads: object) -> list[dict[str, Any]]:
    """Drop duplicates by checksum so one file cannot fill two slots."""
    rows = uploads if isinstance(uploads, list) else []
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        upload_id = row.get("id")
        filename = row.get("filename")
        if not isinstance(upload_id, str) or not isinstance(filename, str):
            continue
        checksum = row.get("checksum")
        identity = checksum if isinstance(checksum, str) and checksum else f"id:{upload_id}"
        if identity in seen:
            continue
        seen.add(identity)
        result.append(dict(row))
    return result


def allocate_uploads(
    raw_request: object,
    raw_uploads: object,
) -> dict[str, list[str]]:
    """Assign uploaded files to requirement slots.

    Two passes:

    1. **Maximum matching over required slots.** Each required slot is expanded
       into ``min_files`` positions and filled with augmenting-path search, so a
       position releases a file it already holds when that lets another position
       be satisfied. Without this, an extension-permissive slot can starve a
       strict one purely by being listed first.
    2. **Greedy top-up.** Remaining eligible files fill optional slots and the
       ``min..max`` headroom of flexible ones, preferring slots that still have
       unmet minimums, then narrower extension sets, then filename similarity.

    A file is assigned to at most one requirement in both passes.
    """
    try:
        request = FileInputRequest.model_validate(raw_request)
    except Exception as exc:
        raise InputRequestError(str(exc)) from exc

    uploads = _distinct(raw_uploads)
    requirements = request.requirements

    # Pass 1 — required minimums via augmenting-path matching.
    positions: list[int] = [
        index
        for index, requirement in enumerate(requirements)
        if requirement.required
        for _ in range(requirement.min_files)
    ]
    upload_to_position: dict[int, int] = {}

    def assign(position: int, visited: set[int]) -> bool:
        requirement = requirements[positions[position]]
        candidates = sorted(
            enumerate(uploads),
            key=lambda item: (-_semantic_score(requirement, item[1]), item[0]),
        )
        for upload_index, upload in candidates:
            if upload_index in visited:
                continue
            if not requirement.accepts(str(upload["filename"])):
                continue
            visited.add(upload_index)
            holder = upload_to_position.get(upload_index)
            if holder is None or assign(holder, visited):
                upload_to_position[upload_index] = position
                return True
        return False

    for position in range(len(positions)):
        assign(position, set())

    allocated: dict[str, list[str]] = {
        requirement.key: [] for requirement in requirements
    }
    assigned: set[int] = set()
    for upload_index, position in upload_to_position.items():
        requirement = requirements[positions[position]]
        allocated[requirement.key].append(str(uploads[upload_index]["id"]))
        assigned.add(upload_index)

    # Pass 2 — greedy top-up for optional slots and 1..N headroom.
    for upload_index, upload in enumerate(uploads):
        if upload_index in assigned:
            continue
        candidates = [
            (index, requirement)
            for index, requirement in enumerate(requirements)
            if requirement.accepts(str(upload["filename"]))
            and len(allocated[requirement.key]) < requirement.max_files
        ]
        if not candidates:
            continue
        _, requirement = min(
            candidates,
            key=lambda item: (
                # Unmet minimums first…
                -max(0, item[1].min_files - len(allocated[item[1].key])),
                # …then the most specific slot…
                len(item[1].allowed_extensions) or 999,
                # …then the best filename match, then declaration order.
                -_semantic_score(item[1], upload),
                item[0],
            ),
        )
        allocated[requirement.key].append(str(upload["id"]))
        assigned.add(upload_index)

    return allocated


def validate_fulfillment(
    raw_request: object,
    raw_uploads: object,
) -> dict[str, list[str]]:
    """Allocate, then require that every mandatory slot is satisfied."""
    request = FileInputRequest.model_validate(raw_request)
    allocated = allocate_uploads(request.model_dump(mode="json"), raw_uploads)
    missing = [
        requirement.label
        for requirement in request.requirements
        if requirement.required
        and len(allocated.get(requirement.key, [])) < requirement.min_files
    ]
    if missing:
        message = "required files are still missing: " + ", ".join(missing)
        raise InputFulfillmentError(message, public_message=message)
    return allocated


# Names used by the extracted runtime before the package layout was unified.
allocate_input_uploads = allocate_uploads
validate_input_fulfillment = validate_fulfillment


def pending_requests(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Replay the event log to find requests still awaiting the user.

    ``input_invalidated`` re-opens a previously fulfilled request — that is what
    happens when a user deletes a file they had already supplied.
    """
    known: dict[str, dict[str, Any]] = {}
    pending: dict[str, dict[str, Any]] = {}
    for event in events:
        name = event.get("event")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if name == "input_required":
            request = data.get("request")
            request_id = request.get("request_id") if isinstance(request, dict) else None
            if isinstance(request_id, str):
                known[request_id] = dict(request)  # type: ignore[arg-type]
                pending[request_id] = dict(request)  # type: ignore[arg-type]
        elif name in {"input_fulfilled", "input_cancelled"}:
            request_id = data.get("request_id")
            if isinstance(request_id, str):
                pending.pop(request_id, None)
        elif name == "input_invalidated":
            request_id = data.get("request_id")
            if isinstance(request_id, str) and request_id in known:
                pending[request_id] = known[request_id]
    return list(pending.values())


def requests_using_upload(
    events: Sequence[Mapping[str, Any]], upload_id: str
) -> list[str]:
    """Requests whose latest fulfilment consumed ``upload_id``.

    Deleting that upload must invalidate them so the agent asks again rather
    than executing against a file that no longer exists.
    """
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            continue
        if event.get("event") == "input_fulfilled":
            allocation = data.get("allocated")
            latest[request_id] = dict(allocation) if isinstance(allocation, dict) else {}
        elif event.get("event") in {"input_cancelled", "input_invalidated"}:
            latest.pop(request_id, None)
    affected: list[str] = []
    for request_id, allocation in latest.items():
        for ids in allocation.values():
            if isinstance(ids, list) and upload_id in ids:
                affected.append(request_id)
                break
    return affected


__all__ = [
    "FileInputRequest",
    "FileRequirement",
    "allocate_uploads",
    "allocate_input_uploads",
    "pending_requests",
    "requests_using_upload",
    "validate_fulfillment",
    "validate_input_fulfillment",
    "validate_input_request",
]
