"""Cross-model AI security review for pull requests, backed by the LiteLLM gateway.

A professional-grade, advisory security auditor that layers LLM reasoning on top of
the deterministic scanners the repo already runs (CodeQL, Semgrep, OSV-Scanner,
Grype, Scorecard, Zizmor, GitGuardian). It does not replace them; it catches the
business-logic / authz / exploitability issues pattern-matchers miss and enumerates
them as an actionable risk report.

Pipeline (the diff is untrusted input; the LLM is a pure text-in / JSON-out judge with
no tools, so an instruction injected into a diff can at worst corrupt a finding's text,
never trigger an action):

  1. Context   - `gh pr view/diff` plus each changed file's body fetched at the PR
                 head via the contents API as DATA; PR-authored code is never run.
  2. Propose   - a proposer model (default claude-opus-5) surfaces HIGH/MEDIUM
                 vulnerabilities using the methodology of Claude Code's built-in
                 `security-review` skill (5 categories, 3-phase, data-flow tracing).
  3. Refute    - a different-vendor verifier (default gpt-5.6-sol) adversarially
                 re-checks each finding against the HARD EXCLUSIONS / PRECEDENTS list
                 and scores 1-10; only findings it keeps at >= threshold survive.
                 Cross-vendor heterogeneity is the lever the literature credits for
                 large false-positive reductions.
  4. Publish   - an idempotent PR comment + a SARIF 2.1.0 file for the Security tab.

Advisory: it never fails the check / blocks a merge in this version.

Environment:
    OPENAI_API_KEY            - gateway virtual key. When absent the run is a no-op
                                dry-run (no LLM call, no comment) so external-fork PRs
                                can't force paid calls or inject via a hostile diff.
    OPENAI_BASE_URL           - gateway base URL (OpenAI-compatible surface).
    SECREVIEW_PROPOSER_MODEL  - proposer model override (default claude-opus-5).
    SECREVIEW_VERIFIER_MODEL  - verifier model override (default gpt-5.6-sol).

Usage:
    security_review_with_llm.py --repo owner/repo --pr 1234
    security_review_with_llm.py --repo owner/repo --pr 1234 --dry-run
    security_review_with_llm.py --repo owner/repo --pr 1234 --print-prompt
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

MARKER = "<!-- ai-security-review -->"

DEFAULT_PROPOSER_MODEL = "claude-opus-5"
DEFAULT_VERIFIER_MODEL = "gpt-5.6-sol"
GPT5_FAMILY_PREFIX = "gpt-5"

PROPOSER_MIN_CONFIDENCE = 0.7
VERIFIER_KEEP_THRESHOLD = 8

MAX_DIFF_CHARS = 200_000
MAX_FILE_CHARS = 40_000
MAX_TOTAL_FILE_CHARS = 300_000
MAX_FINDINGS_TO_VERIFY = 25
MAX_VERIFY_WORKERS = 8

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
TOOL_NAME = "ai-security-review"
TOOL_URI = "https://github.com/BerriAI/litellm/blob/main/.github/scripts/security_review_with_llm.py"


class ReviewError(Exception):
    """Raised for unrecoverable pipeline errors surfaced to the operator."""


# ---------------------------------------------------------------------------
# Data model — LLM output is validated here so the rest of the code is typed


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


_SEVERITY_RANK: dict[Severity, int] = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
_SARIF_LEVEL: dict[Severity, str] = {Severity.HIGH: "error", Severity.MEDIUM: "warning", Severity.LOW: "note"}


class RawFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: str
    line: int | None = None
    severity: Severity
    category: str
    description: str
    exploit_scenario: str = ""
    recommendation: str = ""
    confidence: float = 0.0

    @field_validator("severity", mode="before")
    @classmethod
    def _norm_severity(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("category", mode="before")
    @classmethod
    def _norm_category(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        return slug or "uncategorized"

    @field_validator("line", mode="before")
    @classmethod
    def _norm_line(cls, value: object) -> object:
        if isinstance(value, int) and value >= 1:
            return value
        if isinstance(value, str) and value.strip().isdigit() and int(value) >= 1:
            return int(value)
        return None


class ProposerResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    findings: list[RawFinding] = []


class VerifierVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    keep: bool
    confidence: int = 0
    reasoning: str = ""


@dataclass(frozen=True, slots=True)
class FileContent:
    path: str
    content: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class ReviewContext:
    repo: str
    number: int
    head_sha: str
    title: str
    body: str
    diff: str
    diff_truncated: bool
    files: tuple[FileContent, ...]
    files_truncated: bool


@dataclass(frozen=True, slots=True)
class VerifiedFinding:
    finding: RawFinding
    verdict: VerifierVerdict


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    context: ReviewContext
    proposed: tuple[RawFinding, ...]
    verified: tuple[VerifiedFinding, ...]
    findings_capped: bool


# ---------------------------------------------------------------------------
# Injected side effects (default impls; tests pass fakes)


GhFn = Callable[..., str]
ReadFileFn = Callable[[str], str | None]


class LLMFn(Protocol):
    def __call__(self, *, system: str, user: str, model: str) -> str: ...


def default_gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return result.stdout


def make_api_reader(repo: str, head_sha: str, gh_fn: GhFn) -> ReadFileFn:
    """Fetch a changed file's content at the PR head as DATA via the contents API.

    The PR head is untrusted (a fork PR is attacker-controlled), so we never check
    it out or execute anything from it; we read file bodies through the API and
    treat them as text to analyze. Returns None for missing, binary, or too-large
    (>1 MB, no inline content) files.
    """

    def _read(path: str) -> str | None:
        encoded = urllib.parse.quote(path, safe="/")
        try:
            raw = gh_fn("api", f"repos/{repo}/contents/{encoded}?ref={head_sha}", "-q", ".content")
        except subprocess.CalledProcessError:
            return None
        raw = raw.strip().strip('"').strip()
        if not raw:
            return None
        try:
            return base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    return _read


def make_llm(api_key: str, base_url: str | None) -> LLMFn:
    """Build an OpenAI-compatible caller for the gateway.

    gpt-5.x reasoning models reject `temperature != 1`, so for that family we keep
    reasoning on (better refutation) and omit temperature; everything else runs at
    temperature 0 for determinism. JSON is always requested via response_format.
    """

    def _call(*, system: str, user: str, model: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if model.lower().startswith(GPT5_FAMILY_PREFIX):
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                extra_body={"reasoning_effort": "medium"},
            )
        else:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0,
            )
        return response.choices[0].message.content or ""

    return _call


# ---------------------------------------------------------------------------
# Context assembly


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _collect_files(paths: tuple[str, ...], read_file: ReadFileFn) -> tuple[tuple[FileContent, ...], bool]:
    collected: list[FileContent] = []
    total = 0
    truncated_any = False
    for path in paths:
        raw = read_file(path)
        if raw is None:
            continue
        if total >= MAX_TOTAL_FILE_CHARS:
            truncated_any = True
            break
        room = MAX_TOTAL_FILE_CHARS - total
        body, file_trunc = _cap(raw, min(MAX_FILE_CHARS, room))
        truncated_any = truncated_any or file_trunc
        total += len(body)
        collected.append(FileContent(path=path, content=body, truncated=file_trunc))
    return tuple(collected), truncated_any


def build_context(repo: str, number: int, *, gh_fn: GhFn, read_file: ReadFileFn | None = None) -> ReviewContext:
    meta_raw = gh_fn("pr", "view", str(number), "--repo", repo, "--json", "title,body,headRefOid,files")
    meta = json.loads(meta_raw)
    head_sha = meta.get("headRefOid") or ""
    if not head_sha:
        raise ReviewError(f"could not resolve head SHA for {repo}#{number}")
    reader = read_file if read_file is not None else make_api_reader(repo, head_sha, gh_fn)
    diff_raw = gh_fn("pr", "diff", str(number), "--repo", repo)
    diff, diff_truncated = _cap(diff_raw, MAX_DIFF_CHARS)
    changed = tuple(entry["path"] for entry in meta.get("files", []) if entry.get("path"))
    files, files_truncated = _collect_files(changed, reader)
    return ReviewContext(
        repo=repo,
        number=number,
        head_sha=head_sha,
        title=meta.get("title") or "",
        body=meta.get("body") or "",
        diff=diff,
        diff_truncated=diff_truncated,
        files=files,
        files_truncated=files_truncated,
    )


def _render_files_block(files: tuple[FileContent, ...]) -> str:
    if not files:
        return "(no readable changed files; rely on the diff below)"
    parts: list[str] = []
    for item in files:
        suffix = "\n... [truncated] ..." if item.truncated else ""
        parts.append(f"--- FILE: {item.path} ---\n{item.content}{suffix}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Prompts — adapted from Claude Code's built-in `security-review` skill


PROPOSER_SYSTEM = """\
You are a senior security engineer performing a focused, high-signal security review of the \
changes in a single pull request. Identify HIGH-CONFIDENCE security vulnerabilities NEWLY \
INTRODUCED by this PR that have real exploitation potential. This is not a general code \
review: focus ONLY on security. Do not comment on pre-existing issues or on lines the PR did \
not change.

CRITICAL INSTRUCTIONS:
1. MINIMIZE FALSE POSITIVES: only flag issues where you are >80% confident of actual exploitability.
2. AVOID NOISE: skip theoretical issues, style concerns, and low-impact findings.
3. FOCUS ON IMPACT: prioritize vulnerabilities leading to unauthorized access, data breach, or system compromise.
4. EXCLUSIONS: do NOT report denial-of-service / resource exhaustion, secrets-at-rest handled \
elsewhere, or rate-limiting.

SECURITY CATEGORIES TO EXAMINE:
- Input validation: SQL injection, command injection, XXE, template injection, NoSQL injection, path traversal.
- Authentication & authorization: auth bypass, privilege escalation, session/JWT flaws, authorization-logic bypass.
- Crypto & secrets: hardcoded keys/passwords/tokens, weak crypto, improper key storage, bad \
randomness, cert-validation bypass.
- Injection & code execution: RCE via deserialization, Python pickle, YAML deserialization, eval injection, XSS.
- Data exposure: sensitive-data logging/storage, PII handling, API data leakage, debug-info exposure.
Local-network-only exploitability can still be HIGH severity.

METHODOLOGY:
- Phase 1 (context): note the security frameworks, sanitization/validation patterns, and trust model already present.
- Phase 2 (comparative): compare the new code against those patterns; flag deviations and new attack surface.
- Phase 3 (assessment): for each changed file, trace data flow from user input to sensitive sinks; find injection \
points, unsafe deserialization, and privilege boundaries crossed unsafely.

SEVERITY: HIGH = directly exploitable (RCE / data breach / auth bypass). MEDIUM = significant impact under specific \
conditions. Report ONLY HIGH and MEDIUM. Confidence is a float 0-1; do not report anything below 0.7.

OUTPUT: return ONLY a JSON object of the exact shape:
{"findings": [{"file": "path/to/file.py", "line": 42, "severity": "HIGH", "category": "sql_injection", \
"description": "...", "exploit_scenario": "...", "recommendation": "...", "confidence": 0.9}]}
`line` is the 1-based line in the changed file. If there are no qualifying findings, return {"findings": []}."""


VERIFIER_SYSTEM = """\
You are an adversarial security reviewer. Another model proposed a security finding on a pull request. \
Your job is to REFUTE it: default to rejecting unless it is a concrete, exploitable vulnerability with a \
clear attack path in the changed code. Read the code; do not assume.

HARD EXCLUSIONS - reject (keep=false) if the finding matches any of these:
1. Denial of Service (DoS) or resource exhaustion.
2. Secrets/credentials stored on disk if otherwise secured.
3. Rate limiting or service-overload scenarios.
4. Memory or CPU exhaustion.
5. Missing input validation on non-security-critical fields without proven security impact.
6. Input sanitization in GitHub Action workflows unless clearly triggerable via untrusted input.
7. A mere lack of hardening; only concrete vulnerabilities count.
8. Theoretical race conditions or timing attacks that are not concretely problematic.
9. Outdated third-party libraries (managed separately).
10. Memory-safety issues in memory-safe languages.
11. Files that are only unit tests or test scaffolding.
12. Log spoofing / logging unsanitized user input.
13. SSRF that only controls the path (SSRF matters only if it controls host or protocol).
14. User-controlled content placed into AI system prompts.
15. Regex injection.
16. Regex DoS.
17. Findings in documentation files (e.g. markdown).
18. Lack of audit logs.

PRECEDENTS:
1. Logging high-value secrets in plaintext IS a vuln; logging URLs is safe.
2. UUIDs are unguessable and need no validation.
3. Environment variables and CLI flags are trusted inputs; attacks relying on controlling them are invalid.
4. Resource/file-descriptor leaks are not valid findings.
5. Tabnabbing, XS-Leaks, prototype pollution, open redirects: only at extremely high confidence.
6. React/Angular are XSS-safe unless using dangerouslySetInnerHTML / bypassSecurityTrustHtml or similar.
7. Most GitHub Action workflow vulns are not exploitable; require a concrete, specific attack path.
8. Missing authn/authz in client-side JS/TS is not a vuln; the backend is responsible.
9. Only keep MEDIUM findings that are obvious and concrete.
10. Most notebook (*.ipynb) vulns are not exploitable; require a concrete untrusted-input path.
11. Logging non-PII data is not a vuln; only secrets/passwords/PII count.
12. Command injection in shell scripts is usually not exploitable unless a concrete untrusted-input path exists.

SIGNAL QUALITY: is there a concrete exploitable vuln with a clear attack path? A real risk vs a theoretical best \
practice? A specific code location? Something a security team would act on?

Assign a confidence 1-10 (1-3 likely false positive; 4-6 needs investigation; 7-10 likely a true vulnerability).

OUTPUT: return ONLY a JSON object: {"keep": true, "confidence": 9, "reasoning": "..."} where keep is true only if \
this is a real, actionable vulnerability that survives every exclusion and precedent above."""


def build_proposer_prompt(context: ReviewContext) -> tuple[str, str]:
    user = (
        f"PULL REQUEST: {context.repo}#{context.number}\n"
        f"TITLE: {context.title}\n\n"
        f"DESCRIPTION:\n{context.body or '(none)'}\n\n"
        f"UNIFIED DIFF (the changes to review):\n{context.diff}\n\n"
        f"FULL CONTENT OF CHANGED FILES (for context; review only what the diff changed):\n"
        f"{_render_files_block(context.files)}\n"
    )
    return PROPOSER_SYSTEM, user


def build_verifier_prompt(finding: RawFinding, context: ReviewContext) -> tuple[str, str]:
    location = f"{finding.file}:{finding.line}" if finding.line else finding.file
    user = (
        f"PROPOSED FINDING to refute or confirm:\n"
        f"- location: {location}\n"
        f"- severity: {finding.severity.value}\n"
        f"- category: {finding.category}\n"
        f"- description: {finding.description}\n"
        f"- exploit scenario: {finding.exploit_scenario}\n\n"
        f"PULL REQUEST: {context.repo}#{context.number}\n\n"
        f"UNIFIED DIFF:\n{context.diff}\n\n"
        f"CHANGED FILE CONTENTS:\n{_render_files_block(context.files)}\n"
    )
    return VERIFIER_SYSTEM, user


# ---------------------------------------------------------------------------
# Parsing


def _extract_json(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_proposer(raw: str) -> ProposerResult:
    text = _extract_json(raw)
    if not text:
        return ProposerResult()
    try:
        return ProposerResult.model_validate_json(text)
    except ValidationError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return ProposerResult()
        try:
            return ProposerResult.model_validate_json(match.group(0))
        except ValidationError as exc:
            raise ReviewError(f"proposer returned unparseable JSON: {raw[:200]}") from exc


def parse_verifier(raw: str) -> VerifierVerdict:
    text = _extract_json(raw)
    try:
        return VerifierVerdict.model_validate_json(text)
    except ValidationError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return VerifierVerdict(keep=False, confidence=0, reasoning="unparseable verifier response")
        try:
            return VerifierVerdict.model_validate_json(match.group(0))
        except ValidationError:
            return VerifierVerdict(keep=False, confidence=0, reasoning="unparseable verifier response")


# ---------------------------------------------------------------------------
# Pipeline stages (pure over injected llm)


def propose(context: ReviewContext, *, llm: LLMFn, model: str) -> tuple[RawFinding, ...]:
    system, user = build_proposer_prompt(context)
    raw = llm(system=system, user=user, model=model)
    result = parse_proposer(raw)
    return tuple(
        finding
        for finding in result.findings
        if finding.confidence >= PROPOSER_MIN_CONFIDENCE and finding.severity in (Severity.HIGH, Severity.MEDIUM)
    )


def verify_all(
    findings: tuple[RawFinding, ...], context: ReviewContext, *, llm: LLMFn, model: str
) -> tuple[VerifiedFinding, ...]:
    if not findings:
        return ()

    def _verify(finding: RawFinding) -> VerifiedFinding:
        system, user = build_verifier_prompt(finding, context)
        verdict = parse_verifier(llm(system=system, user=user, model=model))
        return VerifiedFinding(finding=finding, verdict=verdict)

    workers = min(MAX_VERIFY_WORKERS, len(findings))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        verified = tuple(executor.map(_verify, findings))
    kept = tuple(item for item in verified if item.verdict.keep and item.verdict.confidence >= VERIFIER_KEEP_THRESHOLD)
    return _sort_findings(kept)


def _sort_findings(items: tuple[VerifiedFinding, ...]) -> tuple[VerifiedFinding, ...]:
    return tuple(
        sorted(
            items,
            key=lambda item: (_SEVERITY_RANK[item.finding.severity], -item.verdict.confidence),
        )
    )


def review_pr(
    repo: str,
    number: int,
    *,
    llm: LLMFn,
    gh_fn: GhFn,
    proposer_model: str,
    verifier_model: str,
    read_file: ReadFileFn | None = None,
) -> ReviewOutcome:
    context = build_context(repo, number, gh_fn=gh_fn, read_file=read_file)
    proposed = propose(context, llm=llm, model=proposer_model)
    capped = len(proposed) > MAX_FINDINGS_TO_VERIFY
    to_verify = tuple(sorted(proposed, key=lambda f: -f.confidence))[:MAX_FINDINGS_TO_VERIFY] if capped else proposed
    verified = verify_all(to_verify, context, llm=llm, model=verifier_model)
    return ReviewOutcome(context=context, proposed=proposed, verified=verified, findings_capped=capped)


# ---------------------------------------------------------------------------
# Rendering


def _permalink(context: ReviewContext, finding: RawFinding) -> str:
    anchor = f"#L{finding.line}" if finding.line else ""
    return f"https://github.com/{context.repo}/blob/{context.head_sha}/{finding.file}{anchor}"


def _truncation_note(outcome: ReviewOutcome) -> str:
    reasons: list[str] = []
    if outcome.context.diff_truncated:
        reasons.append("the diff was truncated")
    if outcome.context.files_truncated:
        reasons.append("some changed-file contents were truncated")
    if outcome.findings_capped:
        reasons.append(f"only the top {MAX_FINDINGS_TO_VERIFY} proposed findings were verified")
    if not reasons:
        return ""
    return f"\n> Coverage note: {'; '.join(reasons)}. Large changes may hide additional issues.\n"


def render_comment(outcome: ReviewOutcome, *, proposer_model: str, verifier_model: str) -> str:
    context = outcome.context
    verified = outcome.verified
    header = (
        f"{MARKER}\n"
        f"## AI security review\n\n"
        f"Cross-model audit: **{proposer_model}** proposes, **{verifier_model}** adversarially "
        f"verifies; only findings that survive both are shown. Advisory only, not a merge gate.\n"
    )
    note = _truncation_note(outcome)
    if not verified:
        return (
            f"{header}{note}\n"
            f"No high-confidence security findings in this PR's changes. "
            f"({len(outcome.proposed)} candidate(s) proposed, 0 survived cross-model verification.)\n"
        )
    highs = sum(1 for item in verified if item.finding.severity is Severity.HIGH)
    mediums = sum(1 for item in verified if item.finding.severity is Severity.MEDIUM)
    lines = [
        header,
        note,
        f"\n**{len(verified)} finding(s):** {highs} high, {mediums} medium.\n",
    ]
    for index, item in enumerate(verified, start=1):
        finding = item.finding
        location = f"{finding.file}:{finding.line}" if finding.line else finding.file
        lines.append(
            f"\n### {index}. {finding.severity.value} — {finding.category}: "
            f"[`{location}`]({_permalink(context, finding)})\n"
            f"{finding.description}\n\n"
            f"- **Exploit scenario:** {finding.exploit_scenario or '(not provided)'}\n"
            f"- **Recommendation:** {finding.recommendation or '(not provided)'}\n"
            f"- <sub>proposed confidence {finding.confidence:.2f}; verifier confidence "
            f"{item.verdict.confidence}/10</sub>\n"
        )
    return "".join(lines)


def build_sarif(outcome: ReviewOutcome, *, proposer_model: str, verifier_model: str) -> dict[str, object]:
    verified = outcome.verified
    categories = tuple(dict.fromkeys(item.finding.category for item in verified))
    rules = [
        {
            "id": category,
            "name": category,
            "shortDescription": {"text": f"AI-identified {category.replace('_', ' ')} risk"},
        }
        for category in categories
    ]
    results = [
        {
            "ruleId": item.finding.category,
            "level": _SARIF_LEVEL[item.finding.severity],
            "message": {
                "text": (
                    f"[{item.finding.severity.value}] {item.finding.description} "
                    f"Exploit: {item.finding.exploit_scenario or 'n/a'} "
                    f"Fix: {item.finding.recommendation or 'n/a'} "
                    f"(proposed by {proposer_model}, confirmed by {verifier_model} "
                    f"at {item.verdict.confidence}/10)"
                )
            },
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": item.finding.file},
                        "region": {"startLine": item.finding.line or 1},
                    }
                }
            ],
            "partialFingerprints": {"primary": _fingerprint(item.finding)},
        }
        for item in verified
    ]
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "informationUri": TOOL_URI,
                        "version": "1.0.0",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def _fingerprint(finding: RawFinding) -> str:
    basis = f"{finding.file}|{finding.category}|{finding.description}".encode("utf-8")
    return hashlib.sha256(basis).hexdigest()


# ---------------------------------------------------------------------------
# GitHub comment upsert (idempotent by marker)


def find_marker_comment_id(repo: str, number: int, *, gh_fn: GhFn) -> int | None:
    raw = gh_fn(
        "api",
        "--paginate",
        f"repos/{repo}/issues/{number}/comments",
        "-q",
        ".[] | {id: .id, body: .body}",
    )
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if MARKER in (obj.get("body") or ""):
            return int(obj["id"])
    return None


def upsert_comment(repo: str, number: int, body: str, *, gh_fn: GhFn) -> None:
    comment_id = find_marker_comment_id(repo, number, gh_fn=gh_fn)
    if comment_id is None:
        gh_fn(
            "api",
            f"repos/{repo}/issues/{number}/comments",
            "-X",
            "POST",
            "-f",
            f"body={body}",
        )
        return
    gh_fn(
        "api",
        f"repos/{repo}/issues/comments/{comment_id}",
        "-X",
        "PATCH",
        "-f",
        f"body={body}",
    )


# ---------------------------------------------------------------------------
# Entrypoint


def _write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _empty_sarif() -> dict[str, object]:
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": TOOL_NAME, "informationUri": TOOL_URI, "version": "1.0.0", "rules": []}},
                "results": [],
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repository (owner/repo).")
    parser.add_argument("--pr", required=True, type=int, help="Pull request number.")
    parser.add_argument(
        "--proposer-model",
        default=os.environ.get("SECREVIEW_PROPOSER_MODEL") or DEFAULT_PROPOSER_MODEL,
    )
    parser.add_argument(
        "--verifier-model",
        default=os.environ.get("SECREVIEW_VERIFIER_MODEL") or DEFAULT_VERIFIER_MODEL,
    )
    parser.add_argument("--sarif-out", default="security-review.sarif")
    parser.add_argument("--dry-run", action="store_true", help="Run the LLM but do not post a comment.")
    parser.add_argument("--print-prompt", action="store_true", help="Print the proposer prompt and exit.")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY") or ""
    base_url = os.environ.get("OPENAI_BASE_URL") or None

    if args.print_prompt:
        context = build_context(args.repo, args.pr, gh_fn=default_gh)
        system, user = build_proposer_prompt(context)
        print(system)
        print("\n\n=== USER ===\n")
        print(user)
        return 0

    Path(args.sarif_out).write_text(json.dumps(_empty_sarif()), encoding="utf-8")

    if not api_key:
        print("::notice::security review skipped (no OPENAI_API_KEY; dry-run for untrusted/fork PRs).")
        return 0

    llm = make_llm(api_key, base_url)
    outcome = review_pr(
        args.repo,
        args.pr,
        llm=llm,
        gh_fn=default_gh,
        proposer_model=args.proposer_model,
        verifier_model=args.verifier_model,
    )

    sarif = build_sarif(outcome, proposer_model=args.proposer_model, verifier_model=args.verifier_model)
    Path(args.sarif_out).write_text(json.dumps(sarif, indent=2), encoding="utf-8")
    comment = render_comment(outcome, proposer_model=args.proposer_model, verifier_model=args.verifier_model)

    highs = sum(1 for item in outcome.verified if item.finding.severity is Severity.HIGH)
    mediums = sum(1 for item in outcome.verified if item.finding.severity is Severity.MEDIUM)
    _write_step_summary(
        f"### AI security review\n"
        f"- proposed: {len(outcome.proposed)}\n"
        f"- confirmed after cross-model verify: {len(outcome.verified)} ({highs} high, {mediums} medium)\n"
    )

    if args.dry_run:
        print(comment)
        return 0

    upsert_comment(args.repo, args.pr, comment, gh_fn=default_gh)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReviewError as exc:
        print(f"::error::security review failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
