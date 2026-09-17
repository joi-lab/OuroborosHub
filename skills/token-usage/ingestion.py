"""Read-only numeric projection of the canonical usage journal.

No accounting modules are imported. Call refresh in a worker thread, never on an
ASGI event loop. The in-memory projection is disposable; journal order wins.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import threading
import uuid
from pathlib import Path, PurePosixPath


MAX_LINE = 1024 * 1024
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens")
INPUT_USAGE_FIELDS = frozenset(("total_tokens", "cache_read_tokens", "cache_write_tokens"))
MONEY_FIELDS = ("cost_usd", "reservation_upper_bound_usd")
TEXT_FIELDS = ("attempt_id", "kind", "state", "ts", "model", "provider", "task_id",
               "root_task_id", "parent_task_id", "category", "source", "subscription_route")


class RefreshCancelled(RuntimeError):
    """Catch-up was stopped; the previously published snapshot remains intact."""


def _text(value):
    return value if isinstance(value, str) and len(value) <= 1024 else None


def _normalized_input_token_usage(value):
    """Mirror the recorded SSOT shape without inferring absent observations.

    This independent session measurement may coexist with legacy prompt/cache
    fields. Its total includes caches; consumers must not add them together.
    Python integers keep every recorded bit until the API's string export.
    """
    if not isinstance(value, dict) or set(value) != INPUT_USAGE_FIELDS:
        return None
    if any(item is not None and (type(item) is not int or item < 0)
           for item in value.values()):
        return None
    return dict(value)


def _project_row(raw):
    row = {key: _text(raw.get(key)) for key in TEXT_FIELDS}
    row["input_token_usage"] = _normalized_input_token_usage(raw.get("input_token_usage"))
    for key in TOKEN_FIELDS:
        value = raw.get(key)
        row[key] = value if type(value) is int and value >= 0 else None
    for key in MONEY_FIELDS:
        value = raw.get(key)
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        row[key] = value if valid else None
    for key in ("cost_final", "pricing_known"):
        row[key] = raw.get(key) if type(raw.get(key)) is bool else None
    return row


class _NarrowJSON:
    """Streaming metadata parser: ignored values are skipped without decoding.

    In particular source_text is never materialized as a Python string/object.
    The operating system's buffered byte reads are unavoidable for JSON files.
    """
    def __init__(self, stream, check_cancel=None):
        self.stream = stream
        self.check_cancel = check_cancel
        self._characters = 0
        self.char = stream.read(1)

    def advance(self):
        self._characters += 1
        if self.check_cancel and self._characters % 4096 == 0:
            self.check_cancel()
        previous = self.char
        self.char = self.stream.read(1)
        return previous

    def ws(self):
        while self.char and self.char.isspace():
            self.advance()

    def expect(self, char):
        self.ws()
        if self.char != char:
            raise ValueError("Malformed metadata JSON")
        self.advance()

    def string(self, keep=True):
        self.expect('"')
        value = ['"'] if keep else None
        escaped = False
        while self.char:
            c = self.advance()
            if keep:
                value.append(c)
                if len(value) > 16384:
                    raise ValueError("Oversize metadata identifier")
            if ord(c) < 32:
                raise ValueError("Malformed metadata string")
            if escaped:
                if c == "u":
                    for _ in range(4):
                        digit = self.advance()
                        if not digit or digit not in "0123456789abcdefABCDEF":
                            raise ValueError("Malformed metadata unicode escape")
                        if keep:
                            value.append(digit)
                elif c not in '"\\/bfnrt':
                    raise ValueError("Malformed metadata escape")
                escaped = False
            elif c == '"':
                return json.loads("".join(value)) if keep else None
            else:
                escaped = c == "\\"
        raise ValueError("Truncated metadata string")

    def skip(self, depth=0):
        if depth > 64:
            raise ValueError("Metadata nesting limit exceeded")
        self.ws()
        if self.char == '"':
            self.string(False)
        elif self.char == "{":
            self.object(lambda key: self.skip(depth + 1), keep_keys=False)
        elif self.char == "[":
            self.array(lambda: self.skip(depth + 1))
        else:
            token = []
            while self.char and self.char not in ",]} \t\r\n":
                token.append(self.advance())
                if len(token) > 256:
                    raise ValueError("Malformed metadata scalar")
            json.loads("".join(token))

    def object(self, consume, keep_keys=True):
        self.expect("{")
        self.ws()
        if self.char == "}":
            self.advance()
            return
        while True:
            key = self.string(keep_keys)
            self.expect(":")
            consume(key)
            self.ws()
            if self.char == "}":
                self.advance()
                return
            self.expect(",")

    def array(self, consume):
        self.expect("[")
        self.ws()
        if self.char == "]":
            self.advance()
            return
        while True:
            consume()
            self.ws()
            if self.char == "]":
                self.advance()
                return
            self.expect(",")

    def selected(self, keys):
        result = {}
        def field(key):
            self.ws()
            if key in keys and self.char == '"':
                result[key] = self.string()
            else:
                self.skip()
        self.object(field)
        return result


class UsageIndex:
    def __init__(self, data_dir: Path, stop_event=None):
        self.data_dir = Path(data_dir).absolute()
        self.source = self.data_dir / "state" / "usage_attempts.jsonl"
        self._lock = threading.RLock()
        self._stop_event = stop_event
        self._rows = {}
        self._signature = None
        self._offset = 0
        self._anchor = b""
        self._tail = b""
        self._revision = 0
        self._instance_id = uuid.uuid4().hex
        self._archive_signatures = {}
        self._metadata_signature = None
        self._bindings, self._projects = {}, {}
        self._metadata_issues = []
        self._issues = []
        self._counts = self._new_counts()
        self._bytes_read = 0
        self._coverage = {"status": "loading", "history_complete": False,
                          "issues": ["Initial history catch-up has not run."]}

    def _check_cancel(self):
        if self._stop_event is not None and self._stop_event.is_set():
            raise RefreshCancelled("Usage history catch-up stopped")

    @staticmethod
    def _new_counts():
        return {"source_rows": 0, "malformed_lines": 0, "oversize_lines": 0,
                "pending_partial_bytes": 0, "archive_segments": 0, "ignored_rows": 0}

    def _safe_open(self, path):
        relative = path.relative_to(self.data_dir)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        # Walk with directory descriptors so a concurrent parent-directory
        # symlink replacement cannot redirect a validated source outside root.
        parent_fd = os.open(self.data_dir, os.O_RDONLY | directory | nofollow)
        try:
            for part in relative.parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            fd = os.open(relative.parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("Source must be a regular file")
        return os.fdopen(fd, "rb")

    @staticmethod
    def _sig(st):
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def _file_signature(self, path):
        # Even stat must use the validated directory walk: lstat(path) alone
        # follows symlinked parent directories before reaching its final part.
        with self._safe_open(path) as stream:
            return self._sig(os.fstat(stream.fileno()))

    def _read(self, stream, size):
        content = stream.read(size)
        self._bytes_read += len(content)
        return content

    def _lines(self, stream, end, start=0, digest=None):
        """Bounded EOF reader; incomplete final line is retried on a later append."""
        stream.seek(start)
        while stream.tell() < end:
            self._check_cancel()
            position = stream.tell()
            line = stream.readline(min(MAX_LINE + 1, end - position))
            self._bytes_read += len(line)
            if digest is not None:
                digest.update(line)
            oversize = len(line) > MAX_LINE
            complete = line.endswith(b"\n")
            while not complete and stream.tell() < end and oversize:
                self._check_cancel()
                chunk = self._read(stream, min(65536, end - stream.tell()))
                # Do not consume subsequent records while dropping a long line.
                newline = chunk.find(b"\n")
                if newline >= 0:
                    consumed = chunk[:newline + 1]
                    stream.seek(-(len(chunk) - len(consumed)), os.SEEK_CUR)
                    if digest is not None:
                        digest.update(consumed)
                    complete = True
                elif digest is not None:
                    digest.update(chunk)
            if not complete:
                yield position, None, "partial", end - position
                return
            yield stream.tell(), None if oversize else line, "oversize" if oversize else "line", 0

    def _parse_segment(self, stream, end, start=0, digest=None):
        rows, header = {}, None
        last = start
        counts = self._new_counts()
        for next_offset, line, state, pending in self._lines(stream, end, start, digest):
            if state == "partial":
                counts["pending_partial_bytes"] = pending
                break
            last = next_offset
            if state == "oversize":
                counts["oversize_lines"] += 1
                continue
            if not line.strip():
                continue
            counts["source_rows"] += 1
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("Expected object")
            except (ValueError, UnicodeDecodeError, RecursionError):
                counts["malformed_lines"] += 1
                continue
            if start == 0 and next_offset == len(line) and raw.get("kind") == "usage_baseline":
                header = {k: raw.get(k) for k in ("archive_rel", "source_sha256", "source_size_bytes", "source_row_count", "compaction_epoch")}
            if raw.get("kind") in ("usage_baseline", "usage_baseline_group"):
                counts["ignored_rows"] += 1
                continue
            row = _project_row(raw)
            if row["attempt_id"]:
                rows[row["attempt_id"]] = row
            else:
                counts["ignored_rows"] += 1
        return rows, header, last, counts

    def _archive(self, header, seen, depth=0):
        self._check_cancel()
        if depth >= 128:
            raise ValueError("Archive chain exceeds 128 generations")
        rel = header.get("archive_rel")
        if not isinstance(rel, str) or "\\" in rel:
            raise ValueError("Invalid linked archive path")
        parts = PurePosixPath(rel).parts
        if len(parts) != 3 or parts[:2] != ("archive", "usage_ledger") or parts[2] in (".", "..") or str(PurePosixPath(rel)) != rel:
            raise ValueError("Invalid linked archive path")
        if rel in seen:
            raise ValueError("Cyclic linked archive")
        seen.add(rel)
        path = self.data_dir / rel
        try:
            self._archive_signatures[rel] = self._file_signature(path)
        except (OSError, ValueError):
            self._archive_signatures[rel] = None
        expected_hash, expected_size = header.get("source_sha256"), header.get("source_size_bytes")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64 or type(expected_size) is not int or expected_size < 0:
            raise ValueError("Invalid archive integrity metadata")
        with self._safe_open(path) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size != expected_size:
                raise ValueError("Linked archive size mismatch")
            digest = hashlib.sha256()
            rows, previous, offset, counts = self._parse_segment(stream, before.st_size, digest=digest)
            if digest.hexdigest() != expected_hash or self._sig(os.fstat(stream.fileno())) != self._sig(before):
                raise ValueError("Linked archive hash mismatch or concurrent change")
            if counts["pending_partial_bytes"]:
                raise ValueError("Linked archive has an incomplete final line")
        expected_rows = header.get("source_row_count")
        if type(expected_rows) is not int or expected_rows != counts["source_rows"] + counts["oversize_lines"]:
            raise ValueError("Linked archive row count mismatch")
        older = {}
        if previous:
            try:
                older = self._archive(previous, seen, depth + 1)
            except (OSError, ValueError) as exc:
                self._issues.append("Earlier linked archive unavailable: " + self._safe_error(exc))
        older.update(rows)
        self._add_counts(counts)
        self._counts["archive_segments"] += 1
        return older

    def _add_counts(self, counts):
        for key, value in counts.items():
            if key == "pending_partial_bytes":
                self._counts[key] = value
            else:
                self._counts[key] += value

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, FileNotFoundError):
            return "file missing"
        if isinstance(exc, OSError):
            return "file unreadable"
        if isinstance(exc, UnicodeError):
            return "invalid UTF-8 metadata"
        if isinstance(exc, json.JSONDecodeError):
            return "malformed metadata JSON"
        return str(exc)

    def _load_metadata(self):
        paths = [self.data_dir / "state" / n for n in ("project_task_bindings.json", "projects.json")]
        signatures = []
        for path in paths:
            self._check_cancel()
            try:
                signatures.append(self._file_signature(path))
            except (OSError, ValueError):
                signatures.append(None)
        if tuple(signatures) == self._metadata_signature:
            return False
        self._metadata_signature = tuple(signatures)
        self._bindings, self._projects, self._metadata_issues = {}, {}, []
        for path in paths:
            try:
                with self._safe_open(path) as binary:
                    import io
                    stream = io.TextIOWrapper(binary, encoding="utf-8")
                    parser = _NarrowJSON(stream, self._check_cancel)
                    found = []
                    if path.name == "project_task_bindings.json":
                        def binding(key):
                            item = parser.selected({"task_id", "project_id"})
                            if item.get("task_id", key) != key:
                                self._metadata_issues.append("Conflicting task binding identity")
                            elif _text(key) and _text(item.get("project_id")):
                                self._bindings[key] = item["project_id"]
                        def top(key):
                            if key == "bindings":
                                found.append(key)
                                parser.object(binding)
                            else:
                                parser.skip()
                        parser.object(top)
                    else:
                        def project():
                            item = parser.selected({"id", "name"})
                            if _text(item.get("id")):
                                self._projects[item["id"]] = _text(item.get("name"))
                        def top(key):
                            if key == "projects":
                                found.append(key)
                                parser.array(project)
                            else:
                                parser.skip()
                        parser.object(top)
                    if len(found) != 1:
                        raise ValueError("Metadata collection missing or duplicated")
                    parser.ws()
                    if parser.char:
                        raise ValueError("Trailing metadata content")
            except (OSError, ValueError, UnicodeError, RecursionError) as exc:
                # A malformed file must not leave a partially parsed join active.
                if path.name == "projects.json":
                    self._projects = {}
                else:
                    self._bindings = {}
                self._metadata_issues.append(path.name + ": " + self._safe_error(exc))
        return True

    def refresh(self):
        # Refresh mutates its disposable index while scanning. Roll back every
        # field on cancellation so no partial generation is published as ready.
        with self._lock:
            self._check_cancel()
            checkpoint = {key: value.copy() if key != "_rows" and isinstance(value, (dict, list)) else value
                          for key, value in self.__dict__.items()
                          if key not in ("_lock", "_stop_event")}
            try:
                return self._refresh()
            except RefreshCancelled:
                self.__dict__.update(checkpoint)
                raise

    def _refresh(self):
        with self._lock:
            self._bytes_read = 0
            changed = self._load_metadata()
            fatal = False
            try:
                current = self._file_signature(self.source)
                for rel, old_signature in self._archive_signatures.items():
                    try:
                        archive_signature = self._file_signature(self.data_dir / rel)
                    except (OSError, ValueError):
                        archive_signature = None
                    if archive_signature != old_signature:
                        self._signature = None
                        break
                if current != self._signature:
                    changed = True
                    with self._safe_open(self.source) as stream:
                        before = os.fstat(stream.fileno())
                        current = self._sig(before)
                        append = (self._signature is not None and current[:2] == self._signature[:2]
                                  and current[2] > self._signature[2])
                        if append:
                            stream.seek(0)
                            append = self._read(stream, len(self._anchor)) == self._anchor
                            stream.seek(max(0, self._offset - len(self._tail)))
                            append = append and self._read(stream, len(self._tail)) == self._tail
                        if not append:
                            self._rows, self._issues, self._counts = {}, [], self._new_counts()
                            self._archive_signatures = {}
                            self._offset = 0
                        else:
                            # Copy only when appending, not on unchanged polls;
                            # cancellation must retain the prior generation.
                            self._rows = dict(self._rows)
                        rows, header, offset, counts = self._parse_segment(stream, before.st_size, self._offset)
                        if header:
                            try:
                                self._rows.update(self._archive(header, set()))
                            except (OSError, ValueError) as exc:
                                self._issues.append("Linked history unavailable: " + self._safe_error(exc))
                        self._rows.update(rows)
                        self._add_counts(counts)
                        self._offset = offset
                        self._signature = current
                        stream.seek(0)
                        self._anchor = self._read(stream, min(4096, offset))
                        stream.seek(max(0, offset - 256))
                        self._tail = self._read(stream, min(256, offset))
            except (OSError, ValueError) as exc:
                changed = self._signature is not None or changed
                self._rows, self._signature, self._offset = {}, None, 0
                self._counts = self._new_counts()
                self._issues = ["Usage source unavailable: " + self._safe_error(exc)]
                fatal = True
            if changed:
                self._revision += 1
            issues = list(dict.fromkeys(self._issues + self._metadata_issues))
            if self._counts["malformed_lines"]:
                issues.append("Malformed journal lines were skipped.")
            if self._counts["oversize_lines"]:
                issues.append("Oversize journal lines were skipped.")
            if self._counts["pending_partial_bytes"]:
                issues.append("An incomplete final journal line is awaiting completion.")
            self._coverage = dict(self._counts, issues=issues, status="error" if fatal else "partial" if issues else "ready",
                                  history_complete=not bool(self._issues or self._counts["malformed_lines"] or self._counts["oversize_lines"] or self._counts["pending_partial_bytes"]),
                                  source_bytes_read=self._bytes_read, canonical_source="state/usage_attempts.jsonl",
                                  retained_attempts=len(self._rows), metadata={"binding_count": len(self._bindings), "project_count": len(self._projects), "issues": list(self._metadata_issues)},
                                  physical_calls=sum(r["kind"] == "attempt" and r["state"] in ("dispatched", "settled", "unresolved") for r in self._rows.values()),
                                  session_aggregates=sum(r["kind"] == "subscription_session" for r in self._rows.values()))
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            rows, gaps, conflicts = [], 0, 0
            for source in self._rows.values():
                row = dict(source)
                if row["input_token_usage"] is not None:
                    row["input_token_usage"] = dict(row["input_token_usage"])
                direct = self._bindings.get(row["task_id"])
                root = self._bindings.get(row["root_task_id"]) if row["root_task_id"] else None
                project_id = direct or root
                assignment = "direct" if direct else "root" if root else "unassigned"
                if direct and root and direct != root:
                    assignment = "conflict"
                    conflicts += 1
                if not project_id or project_id not in self._projects:
                    gaps += 1
                    if project_id:
                        assignment += "_missing_project"
                row.update(project_id=project_id, project_name=self._projects.get(project_id), project_assignment=assignment)
                rows.append(row)
            coverage = dict(self._coverage, project_gaps=gaps, project_conflicts=conflicts)
            # Consumers cannot mutate the index through a returned snapshot.
            coverage["issues"] = list(coverage.get("issues", []))
            if "metadata" in coverage:
                coverage["metadata"] = dict(coverage["metadata"], issues=list(coverage["metadata"]["issues"]))
            return {"rows": rows, "coverage": coverage, "revision": self._revision,
                    "snapshot_id": self._instance_id + ":" + str(self._revision)}
