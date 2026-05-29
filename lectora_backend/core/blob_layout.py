"""Helpers for consistent blob container layout."""
from __future__ import annotations

from dataclasses import dataclass

from lectora_backend.core.course_storage import sanitize_course_slug


def _sanitize_segment(value: str) -> str:
    """Backward-compatible alias."""
    return sanitize_course_slug(value)


@dataclass(frozen=True)
class BlobLayout:
    root: str

    @property
    def doc_dir(self) -> str:
        return f"{self.root}/doc"

    @property
    def output_dir(self) -> str:
        return f"{self.root}/output"

    @property
    def logs_dir(self) -> str:
        return f"{self.root}/logs"

    @property
    def images_dir(self) -> str:
        return f"{self.root}/images"

    @property
    def state_dir(self) -> str:
        return f"{self.root}/state"

    @property
    def shared_state_blob_path(self) -> str:
        return f"{self.state_dir}/shared_state.json"

    def to_dict(self) -> dict[str, str]:
        return {
            "root": self.root,
            "doc": self.doc_dir,
            "output": self.output_dir,
            "logs": self.logs_dir,
            "images": self.images_dir,
            "state": self.state_dir,
        }

    @property
    def required_dirs(self) -> tuple[str, ...]:
        return (
            self.doc_dir,
            self.output_dir,
            self.logs_dir,
            self.images_dir,
            self.state_dir,
        )


def build_blob_layout_for_course(course_title: str) -> BlobLayout:
    """Layout rooted at ``{course_slug}/`` — reused when the slug already exists."""
    if not course_title or not course_title.strip():
        raise ValueError("course_title is required to build a blob layout.")
    slug = sanitize_course_slug(course_title)
    return BlobLayout(root=slug)


def build_blob_layout_from_input_blob(
    blob_path: str,
    job_id: str,
    *,
    course_title: str | None = None,
) -> BlobLayout:
    """Build layout from course title when provided; legacy job_id layout otherwise."""
    from lectora_backend.core.course_storage import resolve_course_title

    if course_title or (blob_path and blob_path.strip()):
        title = resolve_course_title(
            explicit_title=course_title,
            blob_path=blob_path,
            fallback=job_id or "course",
        )
        return build_blob_layout_for_course(title)

    if not job_id or not job_id.strip():
        raise ValueError("job_id or course_title is required to build a blob layout.")
    if not blob_path or not blob_path.strip():
        raise ValueError("blob_path is required to build a blob layout.")

    from pathlib import PurePosixPath

    file_name = sanitize_course_slug(PurePosixPath(blob_path).stem)
    sanitized_job = sanitize_course_slug(job_id)
    return BlobLayout(root=f"{sanitized_job}/{file_name}")
