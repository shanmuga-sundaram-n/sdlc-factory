"""Unit tests for Phase 2 tool modules.

All tests are isolated:
  - File tools operate on pytest tmp_path directories.
  - Terminal tool uses simple echo/true commands.
  - Kanban tools use an in-memory PipelineDB.
  - Web tools mock httpx to avoid real network calls.

Coverage goals:
  TestFileTools         — read, write, patch, search + security (traversal)
  TestTerminalTool      — success, non-zero exit, timeout, bad command
  TestKanbanTools       — create, complete, block, comment
  TestWebTools          — brave path, ddg fallback, empty query guard
  TestToolDiscovery     — discover_tools() loads all 4 modules + registers tools
  TestToolContextInjection — set_tool_context / safe_resolve threading isolation
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_ws(path: Path):
    from tools._context import set_tool_context
    set_tool_context(workspace=path, db=None, run_id="test-run")


# ---------------------------------------------------------------------------
# TestFileTools
# ---------------------------------------------------------------------------

class TestFileTools:
    def test_write_and_read(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file, _read_file

        result = json.loads(_write_file("hello.txt", "Hello World\n"))
        assert result["ok"] is True
        assert result["bytes_written"] > 0

        read = json.loads(_read_file("hello.txt"))
        assert read["content"] == "Hello World\n"
        assert read["total_lines"] == 1

    def test_write_creates_parent_dirs(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file, _read_file

        _write_file("src/api/users.py", "# users\n")
        assert (tmp_path / "src" / "api" / "users.py").exists()

    def test_read_with_offset_and_limit(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file, _read_file

        content = "\n".join(str(i) for i in range(10)) + "\n"
        _write_file("nums.txt", content)

        result = json.loads(_read_file("nums.txt", offset=2, limit=3))
        lines = result["content"].strip().split("\n")
        assert lines == ["2", "3", "4"]
        assert result["returned_lines"] == 3

    def test_read_missing_file_returns_error(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _read_file

        result = json.loads(_read_file("nonexistent.txt"))
        assert "error" in result

    def test_patch_replaces_string(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file, _patch, _read_file

        _write_file("app.py", "VERSION = '0.0.1'\n")
        result = json.loads(_patch("app.py", "0.0.1", "1.0.0"))
        assert result["ok"] is True
        read = json.loads(_read_file("app.py"))
        assert "1.0.0" in read["content"]
        assert "0.0.1" not in read["content"]

    def test_patch_errors_on_missing_string(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file, _patch

        _write_file("app.py", "nothing here\n")
        result = json.loads(_patch("app.py", "MISSING", "replaced"))
        assert "error" in result

    def test_patch_errors_on_ambiguous_string(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file, _patch

        _write_file("app.py", "x = 1\nx = 1\n")
        result = json.loads(_patch("app.py", "x = 1", "x = 2"))
        assert "error" in result
        assert "2 times" in result["error"]

    def test_search_files_glob(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file, _search_files

        _write_file("src/a.py", "")
        _write_file("src/b.py", "")
        _write_file("src/c.txt", "")

        result = json.loads(_search_files("**/*.py"))
        assert result["total"] == 2
        assert all(m.endswith(".py") for m in result["matches"])

    def test_search_files_grep(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file, _search_files

        _write_file("a.py", "import fastapi\n")
        _write_file("b.py", "import flask\n")
        _write_file("c.py", "import django\n")

        result = json.loads(_search_files("*.py", grep="fastapi"))
        assert result["total"] == 1
        assert result["matches"][0] == "a.py"

    def test_directory_traversal_blocked(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _read_file

        result = json.loads(_read_file("../../etc/passwd"))
        assert "error" in result
        assert "outside" in result["error"].lower() or "escape" in result["error"].lower()

    def test_write_traversal_blocked(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file

        result = json.loads(_write_file("../evil.txt", "pwned"))
        assert "error" in result

    def test_search_in_subdirectory(self, tmp_path):
        _set_ws(tmp_path)
        from tools.file_tools import _write_file, _search_files

        _write_file("tests/test_a.py", "")
        _write_file("tests/test_b.py", "")

        result = json.loads(_search_files("*.py", directory="tests"))
        assert result["total"] == 2


# ---------------------------------------------------------------------------
# TestTerminalTool
# ---------------------------------------------------------------------------

class TestTerminalTool:
    def test_successful_command(self, tmp_path):
        _set_ws(tmp_path)
        from tools.terminal_tool import _terminal

        result = json.loads(_terminal("echo hello"))
        assert result["ok"] is True
        assert "hello" in result["stdout"]
        assert result["returncode"] == 0

    def test_nonzero_exit_code(self, tmp_path):
        _set_ws(tmp_path)
        from tools.terminal_tool import _terminal

        result = json.loads(_terminal("false"))
        assert result["ok"] is False
        assert result["returncode"] != 0

    def test_stderr_captured(self, tmp_path):
        _set_ws(tmp_path)
        from tools.terminal_tool import _terminal

        result = json.loads(_terminal("sh -c 'echo err >&2; exit 1'"))
        assert "err" in result["stderr"]
        assert result["returncode"] == 1

    def test_custom_cwd(self, tmp_path):
        _set_ws(tmp_path)
        from tools.terminal_tool import _terminal
        from tools.file_tools import _write_file

        _write_file("subdir/marker.txt", "")
        result = json.loads(_terminal("ls marker.txt", cwd="subdir"))
        assert result["ok"] is True
        assert "marker.txt" in result["stdout"]

    def test_cwd_traversal_blocked(self, tmp_path):
        _set_ws(tmp_path)
        from tools.terminal_tool import _terminal

        result = json.loads(_terminal("ls", cwd="../../"))
        assert "error" in result

    def test_unknown_command_returns_error(self, tmp_path):
        _set_ws(tmp_path)
        from tools.terminal_tool import _terminal

        result = json.loads(_terminal("__this_command_does_not_exist__"))
        assert "error" in result

    def test_timeout(self, tmp_path):
        _set_ws(tmp_path)
        from tools.terminal_tool import _terminal

        result = json.loads(_terminal("sleep 10", timeout=1))
        assert "error" in result
        assert "timed out" in result["error"].lower()


# ---------------------------------------------------------------------------
# TestKanbanTools
# ---------------------------------------------------------------------------

class TestKanbanTools:
    @pytest.fixture
    def db_and_ctx(self, tmp_path):
        from core.session import PipelineDB
        from tools._context import set_tool_context

        db = PipelineDB(db_path=tmp_path / "kanban_test.db")
        db.create_run("run-kt", "Kanban test run", {})
        set_tool_context(workspace=tmp_path, db=db, run_id="run-kt")
        return db

    def test_create_task(self, db_and_ctx):
        from tools.kanban_tools import _kanban_create

        result = json.loads(_kanban_create("t-001", "Build API", "developer_backend"))
        assert result["ok"] is True
        assert result["status"] == "ready"

    def test_complete_task(self, db_and_ctx):
        db = db_and_ctx
        from tools.kanban_tools import _kanban_create, _kanban_complete

        _kanban_create("t-002", "Build UI", "developer_ui")
        result = json.loads(_kanban_complete("t-002", notes="All tests pass"))
        assert result["ok"] is True
        assert result["status"] == "done"

        task = db.get_task("t-002")
        assert task["status"] == "done"

    def test_block_task(self, db_and_ctx):
        db = db_and_ctx
        from tools.kanban_tools import _kanban_create, _kanban_block

        _kanban_create("t-003", "DB migration", "developer_backend")
        result = json.loads(_kanban_block("t-003", "Waiting for schema approval"))
        assert result["ok"] is True
        assert result["status"] == "blocked"

        task = db.get_task("t-003")
        assert task["status"] == "blocked"

    def test_comment_on_task(self, db_and_ctx):
        from tools.kanban_tools import _kanban_create, _kanban_comment

        _kanban_create("t-004", "Write tests", "developer_backend")
        result = json.loads(_kanban_comment("t-004", "Added 10 unit tests", author="dev_backend"))
        assert result["ok"] is True

    def test_create_with_metadata(self, db_and_ctx):
        db = db_and_ctx
        from tools.kanban_tools import _kanban_create

        _kanban_create(
            "t-005", "Implement auth", "developer_backend",
            description="JWT auth endpoint",
            metadata={"skills": ["python", "jwt"], "sprint": 1},
        )
        task = db.get_task("t-005")
        assert task is not None
        assert task["metadata"]["skills"] == ["python", "jwt"]

    def test_no_db_context_returns_error(self, tmp_path):
        from tools._context import set_tool_context
        from tools.kanban_tools import _kanban_create

        set_tool_context(workspace=tmp_path, db=None, run_id="")
        result = json.loads(_kanban_create("t-x", "Task", "developer_backend"))
        assert "error" in result


# ---------------------------------------------------------------------------
# TestWebTools
# ---------------------------------------------------------------------------

class TestWebTools:
    def test_brave_search_called_when_key_set(self, tmp_path):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "web": {
                "results": [
                    {"title": "FastAPI Docs", "url": "https://fastapi.tiangolo.com", "description": "FastAPI framework"},
                ]
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "test-key"}):
            with patch("httpx.get", return_value=mock_response) as mock_get:
                from tools.web_tools import _web_search
                result = json.loads(_web_search("FastAPI tutorial", num_results=3))

        assert result["source"] == "brave"
        assert len(result["results"]) == 1
        assert result["results"][0]["title"] == "FastAPI Docs"
        mock_get.assert_called_once()

    def test_ddg_fallback_when_no_key(self, tmp_path):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "AbstractText": "FastAPI is a modern web framework.",
            "AbstractURL": "https://fastapi.tiangolo.com",
            "Heading": "FastAPI",
            "RelatedTopics": [],
        }
        mock_response.raise_for_status = MagicMock()

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BRAVE_SEARCH_API_KEY", None)
            with patch("httpx.get", return_value=mock_response):
                from tools import web_tools  # ensure module is loaded
                import importlib
                importlib.reload(web_tools)
                result = json.loads(web_tools._web_search("FastAPI"))

        assert result["source"] == "duckduckgo"

    def test_empty_query_returns_error(self):
        from tools.web_tools import _web_search
        result = json.loads(_web_search(""))
        assert "error" in result

    def test_num_results_capped_at_20(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"web": {"results": []}}
        mock_response.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "k"}):
            with patch("httpx.get", return_value=mock_response) as mock_get:
                from tools.web_tools import _web_search
                _web_search("query", num_results=999)
                _, kwargs = mock_get.call_args
                params = kwargs.get("params", {})
                assert params.get("count", 0) <= 20


# ---------------------------------------------------------------------------
# TestToolDiscovery
# ---------------------------------------------------------------------------

class TestToolDiscovery:
    def test_discover_tools_imports_all_modules(self):
        from core.registry import discover_tools, ToolRegistry

        reg = ToolRegistry()
        modules = discover_tools()
        assert "tools.file_tools" in modules
        assert "tools.terminal_tool" in modules
        assert "tools.web_tools" in modules
        assert "tools.kanban_tools" in modules

    def test_pipeline_init_registers_tools(self, tmp_path):
        from core.pipeline import SDLCPipeline
        from core.session import PipelineDB
        from core.registry import registry

        db = PipelineDB(db_path=tmp_path / "disc.db")
        SDLCPipeline.from_dict({"workspace": str(tmp_path / "out")}, db=db)

        # After pipeline init, these tools should be in the global registry
        for tool_name in ("read_file", "write_file", "patch", "search_files",
                          "terminal", "web_search",
                          "kanban_create", "kanban_complete", "kanban_block", "kanban_comment"):
            entry = registry.get_entry(tool_name)
            assert entry is not None, f"Tool {tool_name!r} not registered after pipeline init"

    def test_file_toolset_definitions(self):
        from core.registry import registry

        defs = registry.get_definitions(enabled_toolsets=["file"])
        names = {d["function"]["name"] for d in defs}
        assert {"read_file", "write_file", "patch", "search_files"} <= names

    def test_terminal_toolset_definition(self):
        from core.registry import registry

        defs = registry.get_definitions(enabled_toolsets=["terminal"])
        names = {d["function"]["name"] for d in defs}
        assert "terminal" in names

    def test_kanban_toolset_definitions(self):
        from core.registry import registry

        defs = registry.get_definitions(enabled_toolsets=["kanban"])
        names = {d["function"]["name"] for d in defs}
        assert {"kanban_create", "kanban_complete", "kanban_block", "kanban_comment"} <= names


# ---------------------------------------------------------------------------
# TestToolContextInjection
# ---------------------------------------------------------------------------

class TestToolContextInjection:
    def test_set_and_get_workspace(self, tmp_path):
        from tools._context import set_tool_context, get_workspace

        set_tool_context(workspace=tmp_path)
        assert get_workspace() == tmp_path

    def test_set_and_get_run_id(self, tmp_path):
        from tools._context import set_tool_context, get_run_id

        set_tool_context(workspace=tmp_path, run_id="run-xyz")
        assert get_run_id() == "run-xyz"

    def test_thread_isolation(self, tmp_path):
        from tools._context import set_tool_context, get_run_id

        results = {}
        barrier = threading.Barrier(2)

        def worker(run_id: str):
            set_tool_context(workspace=tmp_path, run_id=run_id)
            barrier.wait()  # both threads set their context before either reads
            results[run_id] = get_run_id()

        t1 = threading.Thread(target=worker, args=("run-A",))
        t2 = threading.Thread(target=worker, args=("run-B",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert results["run-A"] == "run-A"
        assert results["run-B"] == "run-B"

    def test_safe_resolve_within_workspace(self, tmp_path):
        from tools._context import set_tool_context, safe_resolve

        set_tool_context(workspace=tmp_path)
        resolved = safe_resolve("src/main.py")
        assert resolved == (tmp_path / "src" / "main.py").resolve()

    def test_safe_resolve_traversal_raises(self, tmp_path):
        from tools._context import set_tool_context, safe_resolve

        set_tool_context(workspace=tmp_path)
        with pytest.raises(PermissionError, match="outside"):
            safe_resolve("../../etc/passwd")


# ---------------------------------------------------------------------------
# TestGitWorkspace
# ---------------------------------------------------------------------------

class TestGitWorkspace:
    def test_git_init_creates_dotgit(self, tmp_path):
        from core.pipeline import SDLCPipeline
        from core.session import PipelineDB

        db = PipelineDB(db_path=tmp_path / "git_test.db")
        ws = tmp_path / "output"
        pipeline = SDLCPipeline.from_dict({"workspace": str(ws)}, db=db)
        run_workspace = ws / "test-run"
        run_workspace.mkdir(parents=True, exist_ok=True)
        pipeline._git_init(run_workspace, "Test idea")

        assert (run_workspace / ".git").exists()

    def test_git_init_idempotent(self, tmp_path):
        from core.pipeline import SDLCPipeline
        from core.session import PipelineDB

        db = PipelineDB(db_path=tmp_path / "git2.db")
        ws = tmp_path / "output"
        pipeline = SDLCPipeline.from_dict({"workspace": str(ws)}, db=db)
        run_ws = ws / "run-idem"
        run_ws.mkdir(parents=True, exist_ok=True)

        pipeline._git_init(run_ws, "idea")
        pipeline._git_init(run_ws, "idea")  # second call should be a no-op
        assert (run_ws / ".git").exists()

    def test_git_commit_stage_with_changes(self, tmp_path):
        import subprocess
        from core.pipeline import SDLCPipeline
        from core.session import PipelineDB

        db = PipelineDB(db_path=tmp_path / "git3.db")
        ws = tmp_path / "output"
        pipeline = SDLCPipeline.from_dict({"workspace": str(ws)}, db=db)
        run_ws = ws / "run-commit"
        run_ws.mkdir(parents=True, exist_ok=True)
        pipeline._git_init(run_ws, "idea")

        # Write a file and commit the discovery stage
        (run_ws / "glossary.json").write_text('{"User": "A person"}')
        pipeline._git_commit_stage(run_ws, "discovery")

        log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(run_ws), capture_output=True, text=True,
        )
        assert "discovery" in log.stdout

    def test_git_commit_stage_no_changes_is_silent(self, tmp_path):
        from core.pipeline import SDLCPipeline
        from core.session import PipelineDB

        db = PipelineDB(db_path=tmp_path / "git4.db")
        ws = tmp_path / "output"
        pipeline = SDLCPipeline.from_dict({"workspace": str(ws)}, db=db)
        run_ws = ws / "run-nochange"
        run_ws.mkdir(parents=True, exist_ok=True)
        pipeline._git_init(run_ws, "idea")

        # Commit with nothing staged — should not raise
        pipeline._git_commit_stage(run_ws, "planning")
