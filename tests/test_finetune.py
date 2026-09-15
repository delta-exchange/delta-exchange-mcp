import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from delta_exchange_mcp.server import build_server
from finetune.generate_qna import TOOL_COUNT, TOOL_SCHEMA_SHA256
from tests.connection_support import service, verified


ROOT = Path(__file__).parents[1]
ARTIFACTS = ("delta-exchange-mcp-qna.md", "delta-exchange-mcp-qna.jsonl")
CREDENTIAL_ASSIGNMENT = re.compile(
    r"[\"']?DELTA_API_(?:KEY|SECRET)[\"']?\s*(?:=|:)"
)
CONTROL_TOOLS = {
    "get_connection_status",
    "get_debug_status",
    "get_trading_status",
    "get_skill",
    "list_skills",
    "setup_credentials",
}


def test_generated_finetune_artifacts_match_source(tmp_path: Path) -> None:
    source = ROOT / "finetune"
    shutil.copyfile(source / "generate_qna.py", tmp_path / "generate_qna.py")

    subprocess.run(
        [sys.executable, str(tmp_path / "generate_qna.py")],
        check=True,
        capture_output=True,
        text=True,
    )

    for name in ARTIFACTS:
        assert (tmp_path / name).read_bytes() == (source / name).read_bytes()


def test_install_answers_do_not_embed_credentials() -> None:
    path = ROOT / "finetune" / "delta-exchange-mcp-qna.jsonl"

    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["metadata"]["category"] != "Install and setup":
            continue

        answer = record["messages"][-1]["content"]
        assert not CREDENTIAL_ASSIGNMENT.search(answer)
        assert "your-api-" not in answer.lower()


def test_control_tools_have_training_coverage() -> None:
    path = ROOT / "finetune" / "delta-exchange-mcp-qna.jsonl"
    questions = {
        json.loads(line)["messages"][-2]["content"]
        for line in path.read_text(encoding="utf-8").splitlines()
    }

    for tool in CONTROL_TOOLS:
        assert any(tool in question for question in questions)


async def test_dataset_contract_matches_the_registered_tool_arguments():
    app = build_server(connection_service=service(verified))
    try:
        tools = await app.list_tools()
    finally:
        await app.close_live_client()
    schemas = {tool.name: tool.input_schema for tool in tools}
    encoded = json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == TOOL_SCHEMA_SHA256
    assert len(tools) == TOOL_COUNT

    path = ROOT / "finetune" / "delta-exchange-mcp-qna.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for record in records:
        assert record["metadata"]["release_status"] == "unreleased"
        assert record["metadata"]["tool_schema_sha256"] == TOOL_SCHEMA_SHA256
        assert re.fullmatch(r"[0-9a-f]{40}", record["metadata"]["source_commit"])
    questions = " ".join(record["messages"][-2]["content"] for record in records)
    assert all(re.search(rf"\b{re.escape(name)}\b", questions) for name in schemas)
