"""Small transactional SQLite document store. Only opaque IDs reach paths."""
import json
import re
import sqlite3
import threading
from pathlib import Path

from fastapi import HTTPException

from .models import Project, now


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "studio.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS documents "
                        "(kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id))")
        self.db.commit()

    def all(self, kind):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT body FROM documents WHERE kind=? ORDER BY rowid DESC", (kind,))]

    def get(self, kind, identifier):
        with self.lock:
            row = self.db.execute("SELECT body FROM documents WHERE kind=? AND id=?",
                                  (kind, identifier)).fetchone()
            if row is None:
                raise HTTPException(404, f"{kind.capitalize()} not found")
            return json.loads(row[0])

    def put(self, kind, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?)",
                            (kind, value["id"], json.dumps(value, allow_nan=False)))
        return value

    def delete(self, kind, identifier):
        with self.lock, self.db:
            self.db.execute("DELETE FROM documents WHERE kind=? AND id=?", (kind, identifier))

    def save_project(self, project: Project):
        with self.lock:
            current = self.get("project", project.id)
            if current["revision"] != project.revision:
                raise HTTPException(409, "Project revision is stale; reload before saving")
            value = project.model_dump()
            value.update(revision=project.revision + 1, created_at=current["created_at"],
                         updated_at=now())
            return self.put("project", value)

    def directory(self, kind, identifier):
        if kind not in {"assets", "jobs"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
            raise HTTPException(404, "Not found")
        directory = self.root / kind / identifier
        if not directory.resolve().is_relative_to(self.root):
            raise HTTPException(404, "Not found")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def owned_file(self, path: Path):
        if path.is_symlink() or not path.resolve().is_relative_to(self.root) or not path.is_file():
            raise HTTPException(404, "File not found")
        return path

    def asset_path(self, project_id, asset_id, kind=None):
        asset = self.get("asset", asset_id)
        if asset["project_id"] != project_id or (kind and asset["kind"] != kind):
            raise HTTPException(422, "Asset does not belong to this project or has wrong kind")
        return self.owned_file(self.directory("assets", project_id) / asset["filename"])

    def validate_assets(self, project):
        for character in project.characters:
            if character.asset_id:
                self.asset_path(project.id, character.asset_id, "image")
        if project.audio.asset_id:
            self.asset_path(project.id, project.audio.asset_id, "audio")
