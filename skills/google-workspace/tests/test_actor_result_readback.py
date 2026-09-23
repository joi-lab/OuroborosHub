"""Optional installed-host integration; no provider/model/network requests."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_real_child_context_and_actor_file_readers(tmp_path):
    if importlib.util.find_spec("ouroboros") is None:
        pytest.skip("Ouroboros checkout required on PYTHONPATH for host reader integration")
    script = r'''
import asyncio, hashlib, json, os
from pathlib import Path
from types import SimpleNamespace
from ouroboros.tools.tool_context import ToolContext
from ouroboros.extension_process_runner import _tool_context_payload, _call_tool, _handler_wants_ctx
from ouroboros import extension_loader
from ouroboros.artifacts import task_artifact_dir_path, read_actor_source_bytes
from ouroboros.headless import copy_child_task_result, remove_subagent_task_drive
from ouroboros.task_results import write_task_result
from ouroboros.tools.core_file_tools import _read_file
from ouroboros.tools.shell import _run_script
from results import result_handler, store_state_bytes
root=Path(os.environ["FIXTURE_ROOT"])
repo=root/"repo"; repo.mkdir()
data=root/"data"; data.mkdir()
project=root/"project"; project.mkdir()
api=SimpleNamespace(get_state_dir=lambda:str(data/"state/skills/google-workspace"))
text=json.dumps({"text":"完整 ответ 🧪"*60000,"end":"END-OF-FULL-SOURCE"},ensure_ascii=False)
handler=result_handler(api,"workspace_request",lambda **kw:text)
assert _handler_wants_ctx(handler)
extension_loader.get_tool=lambda name:{"handler":handler,"wants_ctx":True}
for mode in ["presence","project","subagent"]:
    task="google-"+mode
    drive=data if mode=="presence" else data/"state/headless_tasks"/task/"data"
    drive.mkdir(parents=True,exist_ok=True)
    ctx=ToolContext(repo_dir=repo,drive_root=drive,task_id=task,budget_drive_root=data,
        workspace_root=project if mode!="presence" else None,
        workspace_mode="external" if mode!="presence" else "",task_depth=1 if mode=="subagent" else 0)
    if mode=="subagent": ctx.task_constraint={"mode":"local_readonly_subagent"}
    if mode=="presence": ctx.task_metadata={"_presence_turn":True,"presence":{}}
    # Actual isolated-child calling convention reconstructs the serialized ctx.
    payload=json.loads(asyncio.run(_call_tool("fixture",{},data,repo,_tool_context_payload(ctx))))
    ref=payload["source_ref"]
    path=Path(payload["path"])
    assert path.read_text(encoding="utf-8")==text
    assert hashlib.sha256(path.read_bytes()).hexdigest()==payload["sha256"]
    head=_read_file(ctx,**payload["read"]["arguments"])
    assert "完整 ответ" in head,head
    tail=_read_file(ctx,root="artifact_store",path=str(path),start_char=len(text)-200)
    assert "END-OF-FULL-SOURCE" in tail,tail
    if mode!="subagent":
        code="import hashlib,pathlib;print(hashlib.sha256(pathlib.Path("+repr(str(path))+").read_bytes()).hexdigest())"
        output=_run_script(ctx,script=code,cwd="artifact_store")
        assert payload["sha256"] in output,output
    binary_paths=[]
    for mime,extension,content in [("application/pdf","pdf",b"%PDF-1.7\n\xfffixture"),
                                    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document","docx",b"PK\x03\x04\xfffixture")]:
        stored=store_state_bytes(api,content,name="fixture",extension=extension)
        export=json.dumps({**stored,"mime_type":mime,"name":"fixture","file_id":"fixture"})
        binary=json.loads(result_handler(api,"drive_export",lambda:export)(ctx))
        assert "read" not in binary and "source_ref" not in binary
        assert "Binary file" in binary["read_hint"] and binary["mime_type"]==mime
        assert Path(binary["path"]).read_bytes()==content
        assert binary["path"]!=stored["path"]
        binary_paths.append((Path(binary["path"]),content))
    if mode != "presence":
        write_task_result(drive, task, "completed", result="done", artifact_status="ready",
                          llm_trace={"tool_calls":[{"tool":"workspace_request","result":json.dumps(payload)}]})
        copied=copy_child_task_result(data,{"id":task,"drive_root":str(drive)})
        assert copied["status"]=="completed"
        assert remove_subagent_task_drive(data,task)
        assert not drive.exists()
        assert all(path.read_bytes()==raw for path,raw in binary_paths)
        assert read_actor_source_bytes(data,task,ref)==text.encode("utf-8")
        restored=ToolContext(repo_dir=repo,drive_root=data,task_id=task)
        assert "END-OF-FULL-SOURCE" in _read_file(restored,root="artifact_store",path=str(path),start_char=len(text)-200)
    print(mode,"PASS")
'''
    env = dict(os.environ, FIXTURE_ROOT=str(tmp_path), OUROBOROS_DATA_DIR=str(tmp_path / "data"),
               OUROBOROS_SETTINGS_PATH=str(tmp_path / "settings.json"),
               OUROBOROS_RUNTIME_MODE="advanced", OUROBOROS_SAFETY_MODE="off")
    skill = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = skill + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.splitlines() == ["presence PASS", "project PASS", "subagent PASS"]
