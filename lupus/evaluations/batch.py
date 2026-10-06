"""Does batching pay on a driver with a large fixed input per call? The four-step project from
compare.py on Codex, one call per task vs. batched calls. Usage: PYTHONPATH=src python3 evaluations/batch.py out.json"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from compare import PROJECT_CRITERIA, PROJECT_FILES, STEPS, Codex, all_pass, lupus_goal, totals   # noqa: E402
from pilot import fresh   # noqa: E402

from lupus import probe, supervisor   # noqa: E402
from lupus.kernel import Kernel   # noqa: E402


def main(out_path: str) -> None:
    rows = []
    with tempfile.TemporaryDirectory(prefix="lupus-batch-") as tmp:
        base = Path(tmp).resolve()
        k = Kernel.init(base / "home")
        probe.run(k, live=True)
        for name, cap in (("one_call_per_task", 1), ("batched", 3)):
            k.conn.execute("UPDATE meta SET value = json_set(value, '$.batch_max_tasks', ?) WHERE key = 'policy'", (cap,))
            root = fresh(base, name, PROJECT_FILES)
            log: list = []
            adapter = Codex()
            adapter.log = log
            started = time.monotonic()
            report = supervisor.run_goal(k, lupus_goal(k, root, "문의 접수 도구 완성", STEPS, PROJECT_CRITERIA), adapter, timeout_s=420)
            row = totals(log, started, report["done"], attempts=report["budget"]["attempts"]["used"],
                         outcomes=[s.get("outcome") for s in report["steps"]], all_criteria_pass=all_pass(PROJECT_CRITERIA, root))
            rows.append({"condition": name, **row})
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
        k.close()
    Path(out_path).write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "driver": "native_codex", "rows": rows},
                                         ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
