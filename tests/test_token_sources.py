import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import token_sources  # noqa: E402


def _assistant_line(request_id, output_tokens, uuid=None, request_ref=None, is_sidechain=False,
                     model="claude-sonnet-5", cache_read=0, cache_write=0):
    return json.dumps({
        "type": "assistant", "uuid": uuid or request_id, "requestId": request_id,
        "requestRef": request_ref, "isSidechain": is_sidechain,
        "message": {
            "model": model, "id": "msg_" + request_id,
            "usage": {
                "input_tokens": 100, "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
            },
        },
    })


class ClassifyClaudeQuerySourceTests(unittest.TestCase):
    def test_main_thread_values(self):
        self.assertEqual(token_sources.classify_claude_query_source("repl_main_thread"), token_sources.SOURCE_MAIN)
        self.assertEqual(token_sources.classify_claude_query_source("sdk"), token_sources.SOURCE_MAIN)

    def test_agent_values(self):
        self.assertEqual(token_sources.classify_claude_query_source("agent:builtin:general-purpose"), token_sources.SOURCE_SUBAGENT)
        self.assertEqual(token_sources.classify_claude_query_source("agent:custom:docs"), token_sources.SOURCE_SUBAGENT)

    def test_unrecognised_value_goes_to_other_not_background(self):
        self.assertEqual(token_sources.classify_claude_query_source("something_new"), token_sources.SOURCE_OTHER)

    def test_empty_is_unclassified(self):
        self.assertIsNone(token_sources.classify_claude_query_source(""))
        self.assertIsNone(token_sources.classify_claude_query_source(None))


class ParseClaudeTranscriptTests(unittest.TestCase):
    """The single most important correctness property here: one API response
    is spread over several lines sharing a requestId, and summing all of
    them inflates tokens several times over."""

    def test_duplicate_requestId_lines_count_once_keeping_largest_output(self):
        lines = [
            _assistant_line("req-1", output_tokens=5),
            _assistant_line("req-1", output_tokens=5),
            _assistant_line("req-1", output_tokens=40),  # the final, complete count
            _assistant_line("req-1", output_tokens=5),
        ]
        results = token_sources.parse_claude_transcript(lines, in_subagent_file=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["output"], 40)

    def test_main_thread_classified_via_requestRef_join(self):
        lines = [
            json.dumps({"type": "api-request", "id": "api-1", "querySource": "repl_main_thread"}),
            _assistant_line("req-1", output_tokens=10, request_ref="api-1"),
        ]
        results = token_sources.parse_claude_transcript(lines, in_subagent_file=False)
        self.assertEqual(results[0]["source"], token_sources.SOURCE_MAIN)

    def test_subagent_classified_via_requestRef_join(self):
        lines = [
            json.dumps({"type": "api-request", "id": "api-1", "querySource": "agent:custom:docs"}),
            _assistant_line("req-1", output_tokens=10, request_ref="api-1"),
        ]
        results = token_sources.parse_claude_transcript(lines, in_subagent_file=False)
        self.assertEqual(results[0]["source"], token_sources.SOURCE_SUBAGENT)

    def test_no_requestRef_join_falls_back_to_isSidechain(self):
        lines = [_assistant_line("req-1", output_tokens=10, is_sidechain=True)]
        results = token_sources.parse_claude_transcript(lines, in_subagent_file=False)
        self.assertEqual(results[0]["source"], token_sources.SOURCE_SUBAGENT)
        self.assertEqual(results[0]["sourceDetail"], "sidechain")

    def test_no_signal_at_all_defaults_to_main(self):
        lines = [_assistant_line("req-1", output_tokens=10)]
        results = token_sources.parse_claude_transcript(lines, in_subagent_file=False)
        self.assertEqual(results[0]["source"], token_sources.SOURCE_MAIN)

    def test_a_subagent_file_is_subagent_regardless_of_its_own_querySource(self):
        lines = [
            json.dumps({"type": "api-request", "id": "api-1", "querySource": "repl_main_thread"}),
            _assistant_line("req-1", output_tokens=10, request_ref="api-1"),
        ]
        results = token_sources.parse_claude_transcript(lines, in_subagent_file=True, subagent_type="docs")
        self.assertEqual(results[0]["source"], token_sources.SOURCE_SUBAGENT)
        self.assertEqual(results[0]["sourceDetail"], "agent:docs")

    def test_synthetic_model_lines_are_skipped(self):
        lines = [_assistant_line("req-1", output_tokens=10, model="<synthetic>")]
        results = token_sources.parse_claude_transcript(lines, in_subagent_file=False)
        self.assertEqual(results, [])

    def test_missing_requestId_falls_back_to_message_id_then_uuid(self):
        line = json.dumps({
            "type": "assistant", "uuid": "line-uuid-1",
            "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 1, "output_tokens": 1}},
        })
        results = token_sources.parse_claude_transcript([line], in_subagent_file=False)
        self.assertEqual(len(results), 1)


class ClassifyCodexSourceTests(unittest.TestCase):
    def test_plain_string_is_main(self):
        self.assertEqual(token_sources.classify_codex_source("exec"), (token_sources.SOURCE_MAIN, "exec"))

    def test_none_is_main(self):
        self.assertEqual(token_sources.classify_codex_source(None), (token_sources.SOURCE_MAIN, "main"))

    def test_guardian_subagent_is_review(self):
        source, detail = token_sources.classify_codex_source({"subagent": {"other": "guardian"}})
        self.assertEqual(source, token_sources.SOURCE_REVIEW)

    def test_named_subagent_kind(self):
        source, detail = token_sources.classify_codex_source({"subagent": {"thread_spawn": None}})
        self.assertEqual(source, token_sources.SOURCE_SUBAGENT)
        self.assertEqual(detail, "thread_spawn")


class ParseCodexRolloutTests(unittest.TestCase):
    def test_input_tokens_are_corrected_for_the_cached_portion(self):
        """Confirmed against a real rollout + its ccusage session row: Codex's
        raw input_tokens INCLUDES the cached tokens (unlike Claude's), so the
        cached amount has to come back out to match what ccusage reports."""
        lines = [
            json.dumps({"type": "session_meta", "payload": {"type": "session_meta", "source": "exec"}}),
            json.dumps({"type": "event_msg", "timestamp": "2026-11-09T19:58:00Z", "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 223092, "cached_input_tokens": 193152,
                    "cache_write_input_tokens": 0, "output_tokens": 1042, "reasoning_output_tokens": 34,
                }},
            }}),
        ]
        result = token_sources.parse_codex_rollout(lines)
        self.assertEqual(result["uncached"], 223092 - 193152)
        self.assertEqual(result["cacheRead"], 193152)
        self.assertEqual(result["output"], 1042)
        self.assertEqual(result["source"], token_sources.SOURCE_MAIN)

    def test_only_the_last_token_count_reading_is_used_not_a_sum(self):
        """Several token_count events can fire within seconds of each other,
        each already carrying the running cumulative total — summing them
        would multiply the real figure several times over."""
        def tc(total):
            return json.dumps({"type": "event_msg", "timestamp": "t", "payload": {
                "type": "token_count", "info": {"total_token_usage": {"input_tokens": total, "output_tokens": 0}},
            }})
        lines = [tc(100), tc(150), tc(223092)]
        result = token_sources.parse_codex_rollout(lines)
        self.assertEqual(result["uncached"], 223092)

    def test_no_token_count_line_returns_none(self):
        lines = [json.dumps({"type": "session_meta", "payload": {"type": "session_meta", "source": "exec"}})]
        self.assertIsNone(token_sources.parse_codex_rollout(lines))

    def test_guardian_review_thread_classified_correctly(self):
        lines = [
            json.dumps({"type": "session_meta", "payload": {"type": "session_meta", "source": {"subagent": {"other": "guardian"}}}}),
            json.dumps({"type": "event_msg", "timestamp": "t", "payload": {
                "type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 2}},
            }}),
        ]
        result = token_sources.parse_codex_rollout(lines)
        self.assertEqual(result["source"], token_sources.SOURCE_REVIEW)


class SummarizeBySourceTests(unittest.TestCase):
    def test_sums_per_tool_and_source(self):
        claude_requests = [
            {"source": "main", "uncached": 10, "cacheWrite": 0, "cacheRead": 0, "output": 5, "reasoning": 0},
            {"source": "main", "uncached": 20, "cacheWrite": 0, "cacheRead": 0, "output": 5, "reasoning": 0},
            {"source": "subagent", "uncached": 1, "cacheWrite": 0, "cacheRead": 0, "output": 1, "reasoning": 0},
        ]
        codex_threads = [{"source": "main", "uncached": 7, "cacheWrite": 0, "cacheRead": 0, "output": 1, "reasoning": 0}]
        summary = token_sources.summarize_by_source(claude_requests, codex_threads)
        self.assertEqual(summary["claude"]["main"]["uncached"], 30)
        self.assertEqual(summary["claude"]["main"]["requests"], 2)
        self.assertEqual(summary["claude"]["subagent"]["uncached"], 1)
        self.assertEqual(summary["codex"]["main"]["uncached"], 7)


if __name__ == "__main__":
    unittest.main()
