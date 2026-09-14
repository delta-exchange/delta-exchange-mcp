import re
import shutil
import subprocess
from pathlib import Path

import pytest

from delta_exchange_mcp.connection_app import VIEW_HTML


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node is required for the view runtime contract",
)
def test_connection_view_negotiates_apps_and_handles_link_results():
    script = re.search(r"<script>(.*?)</script>", VIEW_HTML, re.DOTALL)
    assert script is not None
    subprocess.run(
        ["node", str(Path(__file__).with_name("connection_app.mjs"))],
        input=script.group(1),
        text=True,
        check=True,
        capture_output=True,
    )
