"""Application controlled local file storage service.

Provides a clean storage abstraction for medical imaging attachments,
documents, and clinical binary files, decoupling storage from database rows
and enabling future seamless migration to S3/Azure Blob/GCS if needed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import UUID

# Base storage path relative to backend root
STORAGE_ROOT = Path(os.environ.get("HMS_STORAGE_ROOT") or (Path(__file__).resolve().parent.parent.parent / "storage"))


def _sanitize_filename(name: str) -> str:
    """Strip dangerous characters to avoid directory traversal or OS path issues."""
    clean = re.sub(r"[^\w\.\-]", "_", os.path.basename(name))
    return clean or "unnamed_file"


def get_order_storage_dir(module: str, hospital_id: UUID | str, order_id: UUID | str) -> Path:
    """Return and create target directory for an order's files."""
    path = STORAGE_ROOT / module / str(hospital_id) / str(order_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_attachment_file(
    module: str,
    hospital_id: UUID | str,
    order_id: UUID | str,
    file_id: UUID | str,
    file_name: str,
    data: bytes,
) -> str:
    """Save raw binary file to disk and return relative storage path."""
    target_dir = get_order_storage_dir(module, hospital_id, order_id)
    safe_name = _sanitize_filename(file_name)
    stored_filename = f"{file_id}_{safe_name}"
    full_path = target_dir / stored_filename
    full_path.write_bytes(data)
    # Return path relative to STORAGE_ROOT
    return str(full_path.relative_to(STORAGE_ROOT)).replace("\\", "/")


def read_attachment_file(storage_path: str) -> bytes:
    """Read binary file from relative storage path."""
    full_path = STORAGE_ROOT / storage_path
    if not full_path.is_file():
        raise FileNotFoundError(f"Storage file not found: {storage_path}")
    return full_path.read_bytes()


def delete_attachment_file(storage_path: str) -> bool:
    """Delete file if it exists."""
    full_path = STORAGE_ROOT / storage_path
    if full_path.is_file():
        try:
            full_path.unlink()
            return True
        except OSError:
            return False
    return False


def attachment_file_exists(storage_path: str) -> bool:
    """Check if file exists on disk."""
    full_path = STORAGE_ROOT / storage_path
    return full_path.is_file()
