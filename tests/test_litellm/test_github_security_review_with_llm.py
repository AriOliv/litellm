"""Unit tests for `.github/scripts/security_review_with_llm.py`.

Each test targets a specific behavior so a mutation that breaks it fails here:
the proposer severity/confidence filter, the cross-model verifier's keep flag and
>= threshold, severity-then-confidence sorting, SARIF level mapping, the idempotent
comment upsert (POST vs PATCH), context assembly + truncation, and the no-key
dry-run guard that must never reach the LLM.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "security_review_with_llm.py"


@pytest.fixture(scope="module")
def sr():
    spec = importlib.util.spec_from_file_location("security_review_with_llm", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["security_review_with_llm"] = module
    spec.loader.exec_module(module)
    return module


def _ctx(sr, **overrides):
    defaults = dict(
        repo="o/r",
        number=7,
        head_sha="deadbeef",
        title="t",
        body="b",
        diff="--- a/x.py\n+++ b/x.py\n",
        diff_truncated=False,
        files=(sr.FileContent(path="x.py", content="code", truncated=False),),
        files_truncated=False,
    )
    defaults.update(overrides)
    return sr.ReviewContext(**defaults)


def _finding(sr, *, severity="HIGH", category="cat", description="desc", line=2, confidence=0.9):
    return sr.RawFinding(
        file="x.py",
        line=line,
        severity=severity,
        category=category,
        description=description,
        exploit_scenario="exp",
        recommendation="fix",
        confidence=confidence,
    )


class TestFieldValidators:
    def test_severity_is_case_insensitive(self, sr):
        assert _finding(sr, severity="high").severity is sr.Severity.HIGH

    def test_category_is_slugified(self, sr):
        assert _finding(sr, category="Command Injection!").category == "command_injection"

    @pytest.mark.parametrize("raw,expected", [(42, 42), ("42", 42), (0, None), ("x", None), (-3, None)])
    def test_line_is_normalized(self, sr, raw, expected):
        assert _finding(sr, line=raw).line == expected

    def test_extra_fields_ignored(self, sr):
        model = sr.RawFinding.model_validate(
            {"file": "a", "severity": "LOW", "category": "c", "description": "d", "bogus": 1}
        )
        assert model.file == "a"


class TestPropose:
    def test_drops_low_severity_and_low_confidence(self, sr):
        payload = json.dumps(
            {
                "findings": [
                    {
                        "file": "x.py",
                        "line": 1,
                        "severity": "HIGH",
                        "category": "a",
                        "description": "d",
                        "confidence": 0.9,
                    },
                    {
                        "file": "x.py",
                        "line": 2,
                        "severity": "MEDIUM",
                        "category": "b",
                        "description": "d",
                        "confidence": 0.75,
                    },
                    {
                        "file": "x.py",
                        "line": 3,
                        "severity": "LOW",
                        "category": "c",
                        "description": "d",
                        "confidence": 0.99,
                    },
                    {
                        "file": "x.py",
                        "line": 4,
                        "severity": "HIGH",
                        "category": "d",
                        "description": "d",
                        "confidence": 0.6,
                    },
                ]
            }
        )
        kept = sr.propose(_ctx(sr), llm=lambda **_: payload, model="m")
        cats = {f.category for f in kept}
        assert cats == {"a", "b"}


class TestVerifyAll:
    def _verifier(self, sr):
        table = {
            "alpha": {"keep": True, "confidence": 8},
            "bravo": {"keep": True, "confidence": 9},
            "charlie": {"keep": True, "confidence": 9},
            "delta": {"keep": True, "confidence": 7},
            "echo": {"keep": False, "confidence": 10},
        }

        def _llm(*, system, user, model):
            for name, verdict in table.items():
                if f"description: {name}" in user:
                    return json.dumps({**verdict, "reasoning": name})
            raise AssertionError(f"unexpected verify prompt: {user[:80]}")

        return _llm

    def test_keeps_only_surviving_findings_sorted(self, sr):
        proposed = (
            _finding(sr, severity="HIGH", description="alpha"),
            _finding(sr, severity="MEDIUM", description="bravo"),
            _finding(sr, severity="HIGH", description="charlie"),
            _finding(sr, severity="HIGH", description="delta"),
            _finding(sr, severity="HIGH", description="echo"),
        )
        verified = sr.verify_all(proposed, _ctx(sr), llm=self._verifier(sr), model="v")
        order = [(v.finding.severity.value, v.finding.description, v.verdict.confidence) for v in verified]
        assert order == [
            ("HIGH", "charlie", 9),
            ("HIGH", "alpha", 8),
            ("MEDIUM", "bravo", 9),
        ]

    def test_threshold_is_inclusive_of_eight(self, sr):
        proposed = (_finding(sr, description="alpha"),)  # verifier returns conf 8
        verified = sr.verify_all(proposed, _ctx(sr), llm=self._verifier(sr), model="v")
        assert len(verified) == 1

    def test_refuted_finding_is_dropped_even_at_high_confidence(self, sr):
        proposed = (_finding(sr, description="echo"),)  # keep=false, conf 10
        assert sr.verify_all(proposed, _ctx(sr), llm=self._verifier(sr), model="v") == ()

    def test_empty_input_makes_no_calls(self, sr):
        def _boom(**_):
            raise AssertionError("verifier must not run on empty input")

        assert sr.verify_all((), _ctx(sr), llm=_boom, model="v") == ()


class TestParsing:
    def test_proposer_tolerates_json_fence(self, sr):
        result = sr.parse_proposer('```json\n{"findings": []}\n```')
        assert result.findings == []

    def test_proposer_extracts_object_from_prose(self, sr):
        raw = (
            'Here you go:\n{"findings": [{"file":"x","line":1,"severity":"HIGH",'
            '"category":"c","description":"d","confidence":0.9}]}'
        )
        assert len(sr.parse_proposer(raw).findings) == 1

    def test_verifier_malformed_defaults_to_reject(self, sr):
        verdict = sr.parse_verifier("not json at all")
        assert verdict.keep is False and verdict.confidence == 0


class TestRenderComment:
    def test_finding_comment_has_marker_permalink_and_details(self, sr):
        finding = _finding(sr, category="sqli", line=42)
        verified = (sr.VerifiedFinding(finding=finding, verdict=sr.VerifierVerdict(keep=True, confidence=9)),)
        outcome = sr.ReviewOutcome(context=_ctx(sr), proposed=(finding,), verified=verified, findings_capped=False)
        body = sr.render_comment(outcome, proposer_model="P", verifier_model="V")
        assert sr.MARKER in body
        assert "https://github.com/o/r/blob/deadbeef/x.py#L42" in body
        assert "HIGH" in body and "sqli" in body

    def test_no_findings_message(self, sr):
        outcome = sr.ReviewOutcome(context=_ctx(sr), proposed=(_finding(sr),), verified=(), findings_capped=False)
        body = sr.render_comment(outcome, proposer_model="P", verifier_model="V")
        assert "No high-confidence security findings" in body
        assert "1 candidate" in body

    def test_truncation_note_when_capped(self, sr):
        outcome = sr.ReviewOutcome(
            context=_ctx(sr, diff_truncated=True), proposed=(), verified=(), findings_capped=True
        )
        body = sr.render_comment(outcome, proposer_model="P", verifier_model="V")
        assert "Coverage note" in body


class TestBuildSarif:
    def _outcome(self, sr, findings):
        verified = tuple(
            sr.VerifiedFinding(finding=f, verdict=sr.VerifierVerdict(keep=True, confidence=9)) for f in findings
        )
        return sr.ReviewOutcome(context=_ctx(sr), proposed=findings, verified=verified, findings_capped=False)

    def test_level_mapping_and_location(self, sr):
        outcome = self._outcome(
            sr,
            (
                _finding(sr, severity="HIGH", category="a", line=5),
                _finding(sr, severity="MEDIUM", category="b", line=None),
            ),
        )
        sarif = sr.build_sarif(outcome, proposer_model="P", verifier_model="V")
        run = sarif["runs"][0]
        assert sarif["version"] == "2.1.0"
        results = run["results"]
        assert results[0]["level"] == "error"
        assert results[0]["locations"][0]["physicalLocation"]["region"]["startLine"] == 5
        assert results[1]["level"] == "warning"
        assert results[1]["locations"][0]["physicalLocation"]["region"]["startLine"] == 1

    def test_rules_deduped_by_category(self, sr):
        outcome = self._outcome(sr, (_finding(sr, category="dup"), _finding(sr, category="dup")))
        sarif = sr.build_sarif(outcome, proposer_model="P", verifier_model="V")
        rules = sarif["runs"][0]["tool"]["driver"]["rules"]
        assert [r["id"] for r in rules] == ["dup"]

    def test_fingerprint_stable_and_distinct(self, sr):
        a = _finding(sr, category="c", description="same")
        b = _finding(sr, category="c", description="same")
        c = sr.RawFinding(file="other.py", line=1, severity="HIGH", category="c", description="same", confidence=0.9)
        assert sr._fingerprint(a) == sr._fingerprint(b)
        assert sr._fingerprint(a) != sr._fingerprint(c)


class TestBuildContext:
    def _gh(self, sr, *, head="abc", files=("x.py",), diff="D"):
        meta = {"title": "T", "body": "B", "headRefOid": head, "files": [{"path": p} for p in files]}

        def _fn(*args):
            if "view" in args:
                return json.dumps(meta)
            if "diff" in args:
                return diff
            raise AssertionError(f"unexpected gh args: {args}")

        return _fn

    def test_assembles_context_and_reads_files(self, sr):
        ctx = sr.build_context(
            "o/r", 7, gh_fn=self._gh(sr, head="sha1", files=("x.py", "y.py")), read_file=lambda p: f"body-of-{p}"
        )
        assert ctx.head_sha == "sha1"
        assert {f.path for f in ctx.files} == {"x.py", "y.py"}
        assert ctx.files[0].content.startswith("body-of-")

    def test_missing_head_sha_raises(self, sr):
        with pytest.raises(sr.ReviewError):
            sr.build_context("o/r", 7, gh_fn=self._gh(sr, head=""), read_file=lambda p: "c")

    def test_diff_over_cap_is_truncated(self, sr, monkeypatch):
        monkeypatch.setattr(sr, "MAX_DIFF_CHARS", 10)
        ctx = sr.build_context("o/r", 7, gh_fn=self._gh(sr, diff="x" * 50), read_file=lambda p: "c")
        assert ctx.diff_truncated is True
        assert len(ctx.diff) == 10

    def test_deleted_files_are_skipped(self, sr):
        ctx = sr.build_context("o/r", 7, gh_fn=self._gh(sr, files=("gone.py",)), read_file=lambda p: None)
        assert ctx.files == ()


class _RecordingGh:
    def __init__(self, list_lines):
        self.list_lines = list_lines
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args):
        self.calls.append(args)
        if "--paginate" in args:
            return "\n".join(self.list_lines)
        return ""


class TestUpsertComment:
    def test_posts_when_no_existing_marker(self, sr):
        gh = _RecordingGh([json.dumps({"id": 1, "body": "unrelated comment"})])
        sr.upsert_comment("o/r", 7, "body", gh_fn=gh)
        mutating = [c for c in gh.calls if "-X" in c]
        assert len(mutating) == 1
        assert "POST" in mutating[0]
        assert "repos/o/r/issues/7/comments" in mutating[0]

    def test_patches_existing_marker_comment(self, sr):
        gh = _RecordingGh([json.dumps({"id": 99, "body": f"old {sr.MARKER}"})])
        sr.upsert_comment("o/r", 7, "new body", gh_fn=gh)
        mutating = [c for c in gh.calls if "-X" in c]
        assert len(mutating) == 1
        assert "PATCH" in mutating[0]
        assert "repos/o/r/issues/comments/99" in mutating[0]


class TestApiReader:
    def test_reads_and_decodes_content_at_head_sha(self, sr):
        b64 = base64.b64encode(b"hello world").decode()

        def gh(*args):
            assert args[0] == "api"
            assert "contents/app.py" in args[1] and "ref=sha1" in args[1]
            return b64

        reader = sr.make_api_reader("o/r", "sha1", gh)
        assert reader("app.py") == "hello world"

    def test_missing_file_returns_none(self, sr):
        def gh(*args):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="404")

        assert sr.make_api_reader("o/r", "sha1", gh)("gone.py") is None

    def test_build_context_defaults_to_api_reader_not_local_disk(self, sr):
        meta = {"title": "T", "body": "B", "headRefOid": "sha9", "files": [{"path": "a.py"}]}
        b64 = base64.b64encode(b"secret code").decode()

        def gh(*args):
            if "view" in args:
                return json.dumps(meta)
            if "diff" in args:
                return "DIFF"
            if args[0] == "api" and "contents/a.py" in args[1]:
                assert "ref=sha9" in args[1]
                return b64
            raise AssertionError(f"unexpected gh args: {args}")

        ctx = sr.build_context("o/r", 7, gh_fn=gh)
        assert ctx.head_sha == "sha9"
        assert ctx.files[0].content == "secret code"


class TestMainNoKeyDryRun:
    def test_no_key_writes_empty_sarif_and_never_calls_review(self, sr, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        sarif_out = tmp_path / "out.sarif"

        def _must_not_run(*a, **k):
            raise AssertionError("review_pr must not run without a key")

        monkeypatch.setattr(sr, "review_pr", _must_not_run)
        monkeypatch.setattr(sys, "argv", ["prog", "--repo", "o/r", "--pr", "1", "--sarif-out", str(sarif_out)])
        assert sr.main() == 0
        data = json.loads(sarif_out.read_text())
        assert data["runs"][0]["results"] == []
