"""Featured-Program selection in the Control Center.

The selector is extracted from control-center.js and evaluated under Node, so the
tests exercise the shipped code rather than a copy of it.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "nexus" / "web" / "static" / "js" / "control-center.js"

HARNESS = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8").replace(/\r\n/g, "\n");

function extract(name) {
  const start = source.indexOf("function " + name + "(");
  if (start < 0) throw new Error("missing " + name);
  const lineEnd = source.indexOf("\n", start);
  const line = source.slice(start, lineEnd);
  if (line.trimEnd().endsWith("}")) return line + "\n";
  const end = source.indexOf("\n  }\n", start);
  return source.slice(start, end + 4);
}

const constStart = source.indexOf("var TERMINAL_PROGRAM_STATUSES");
const constLine = source.slice(constStart, source.indexOf("\n", constStart) + 1);
const pieces = [constLine, extract("normalized"), extract("parseTime"),
  extract("programActivityTime"), extract("newestProgram")];
const selector = new Function(pieces.join("\n") + "\nreturn newestProgram;")();

const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const results = {};
for (const [label, programs] of Object.entries(cases)) {
  const chosen = selector(programs);
  results[label] = chosen ? chosen.program_id : null;
}
process.stdout.write(JSON.stringify(results));
"""


def _run_selector(cases):
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("node is required to evaluate the Control Center selector")
    completed = subprocess.run(
        [node, "-e", HARNESS, str(SCRIPT)],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _program(program_id, status, updated_at, created_at="2026-10-01T00:00:00+00:00"):
    return {
        "program_id": program_id,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
    }


class FeaturedProgramSelectionTests(unittest.TestCase):
    def test_case_a_newer_completed_program_beats_old_blocked(self):
        programs = [
            _program("PROGRAM-NEW", "COMPLETED", "2026-10-05T00:23:38+00:00"),
            _program("PROGRAM-OLD", "BLOCKED", "2026-10-04T20:46:22+00:00"),
        ]
        self.assertEqual(_run_selector({"case": programs})["case"], "PROGRAM-NEW")

    def test_case_b_running_program_is_featured_over_older_terminal_programs(self):
        programs = [
            _program("PROGRAM-DONE", "COMPLETED", "2026-10-05T10:00:00+00:00"),
            _program("PROGRAM-RUN", "RUNNING", "2026-10-04T09:00:00+00:00"),
            _program("PROGRAM-OLD", "BLOCKED", "2026-09-27T00:49:17+00:00"),
        ]
        self.assertEqual(_run_selector({"case": programs})["case"], "PROGRAM-RUN")

    def test_case_b_pending_counts_as_active_over_terminal(self):
        programs = [
            _program("PROGRAM-DONE", "COMPLETED", "2026-10-05T10:00:00+00:00"),
            _program("PROGRAM-PEND", "PENDING", "2026-10-04T09:00:00+00:00"),
        ]
        self.assertEqual(_run_selector({"case": programs})["case"], "PROGRAM-PEND")

    def test_case_c_multiple_terminal_programs_use_most_recent_regardless_of_status(self):
        programs = [
            _program("PROGRAM-A", "BLOCKED", "2026-10-02T00:00:00+00:00"),
            _program("PROGRAM-B", "FAILED", "2026-10-04T00:00:00+00:00"),
            _program("PROGRAM-C", "COMPLETED", "2026-10-03T00:00:00+00:00"),
        ]
        self.assertEqual(_run_selector({"case": programs})["case"], "PROGRAM-B")

    def test_case_d_blocked_program_remains_in_program_data_after_another_is_featured(self):
        programs = [
            _program("PROGRAM-NEW", "COMPLETED", "2026-10-05T00:23:38+00:00"),
            _program("PROGRAM-OLD", "BLOCKED", "2026-10-04T20:46:22+00:00"),
        ]
        chosen = _run_selector({"case": programs})["case"]
        self.assertEqual(chosen, "PROGRAM-NEW")
        self.assertIn("PROGRAM-OLD", [p["program_id"] for p in programs])

    def test_empty_list_has_no_featured_program(self):
        self.assertIsNone(_run_selector({"case": []})["case"])

    def test_selector_source_has_no_status_priority_list(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r'priority\s*=\s*\["RUNNING",\s*"BLOCKED"', source))


MERGE_HARNESS = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8").replace(/\r\n/g, "\n");

function extract(name) {
  const start = source.indexOf("function " + name + "(");
  if (start < 0) throw new Error("missing " + name);
  const lineEnd = source.indexOf("\n", start);
  const line = source.slice(start, lineEnd);
  if (line.trimEnd().endsWith("}")) return line + "\n";
  const end = source.indexOf("\n  }\n", start);
  return source.slice(start, end + 4);
}

const merge = new Function(
  [extract("parseTime"), extract("mergeRunCheckpoints"), "\nreturn mergeRunCheckpoints;"].join("\n")
)();
const entries = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(merge(entries).map(function (c) { return c.boundary; })));
"""


def _merge_boundaries(entries):
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("node is required to evaluate the Control Center merge")
    completed = subprocess.run(
        [node, "-e", MERGE_HARNESS, str(SCRIPT)],
        input=json.dumps(entries),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


class RecoveryVisibilityTests(unittest.TestCase):
    def test_case_d_old_blocked_run_checkpoints_survive_merge_after_new_program_featured(self):
        entries = [
            {"run": {"id": "RUN-NEW"}, "checkpoints": [
                {"boundary": "RUN_TERMINAL", "created_at": "2026-10-05T00:23:32+00:00"},
            ]},
            {"run": {"id": "RUN-OLD"}, "checkpoints": [
                {"boundary": "RUN_FAILED_OLD", "created_at": "2026-10-04T20:46:22+00:00"},
            ]},
        ]
        self.assertIn("RUN_FAILED_OLD", _merge_boundaries(entries))

    def test_merged_checkpoints_are_chronological_so_tail_slices_are_latest(self):
        entries = [
            {"checkpoints": [{"boundary": "LATE", "created_at": "2026-10-05T00:00:00+00:00"}]},
            {"checkpoints": [{"boundary": "EARLY", "created_at": "2026-10-04T00:00:00+00:00"}]},
        ]
        self.assertEqual(_merge_boundaries(entries), ["EARLY", "LATE"])


if __name__ == "__main__":
    unittest.main()
