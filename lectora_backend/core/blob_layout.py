"""Helpers for consistent blob container layout."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


def _sanitize_segment(value: str) -> str:
    normalized = re.sub(r"\s+", "_", value.strip())
    normalized = re.sub(r"[^A-Za-z0-9._-]", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("._")
    return normalized or "job"


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


def build_blob_layout_from_input_blob(blob_path: str, job_id: str) -> BlobLayout:
    if not job_id or not job_id.strip():
        raise ValueError("job_id is required to build a blob layout.")
    if not blob_path or not blob_path.strip():
        raise ValueError("blob_path is required to build a blob layout.")

    file_name = _sanitize_segment(PurePosixPath(blob_path).stem)
    sanitized_job = _sanitize_segment(job_id)
    return BlobLayout(root=f"{sanitized_job}/{file_name}")
