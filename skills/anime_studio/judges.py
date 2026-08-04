"""Two-VLM identity judge panel used as a HARD gate on scene clips.

This panel exists because the OWNER explicitly chose a hard VLM gate over
objective metrics (a DINOv2-style embedding threshold), against research
advice. Published VLM-judge biases are real, so each one gets a STRUCTURAL
mitigation here — not a comment, a requirement the code enforces:

1. Self-preference bias (a model family favours its own family's outputs):
   mitigated by DIFFERENT FAMILIES — `select_judges` never returns two judges
   of the same family, and the caller must exclude the generator models'
   families entirely (`exclude_families`). If exclusion leaves fewer judges
   than requested, the panel is DEGRADED and cannot produce a `pass`
   (`panel_verdict` treats a short panel as `indeterminate`); the caller must
   never silently substitute an excluded family.
2. Position bias (LLaVA repeated an example ordering 88.2% of the time):
   mitigated by ORDER-SWAP — every judge sees the images twice, forward and
   reversed, and `combine_order_swap` downgrades any attribute whose verdict
   flips with the ordering to `indeterminate`.
3. Verbosity / informativeness bias (longer, richer answers score higher):
   mitigated by ATOMIC ATTRIBUTE QUESTIONS — the prompt forbids any overall
   quality score and demands one `pass`/`fail`/`uncertain` verdict per
   concrete visual attribute, so there is no free-text "impression" for
   verbosity to inflate.

Verdicts are three-state everywhere: `pass`, `fail`, `indeterminate`.
"Could not judge" is never allowed to look like "judged and passed".

`identity_similarity_proxy` is NOT DINOv2 and is NOT calibrated. It is a weak
Pillow-only colour-histogram cosine kept as a diagnostic breadcrumb; it has no
threshold, gates nothing, and must never gate anything.

OWNER RULING (settled): no local models. Identity judging runs ONLY through the
OpenRouter VLM panel in this module. This payload carries no local ML
dependency — no torch, no local DINOv2, no local face/ReID embedding — and must
not acquire one. The proxy below is not a placeholder for a local embedding
that will arrive later; it is a diagnostic that stays diagnostic.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("anime_studio.judges")

# Ordered (model_id, family) candidates. Two entries, two families, on purpose:
# the panel's whole value is family diversity, not headcount.
JUDGE_CANDIDATES = (
    ("google/gemini-3.1-pro-preview", "google"),
    ("anthropic/claude-sonnet-4.6", "anthropic"),
)

VERDICTS = ("pass", "fail", "indeterminate")


def model_family(model_id: str) -> str:
    """Provider family of an OpenRouter-style id: the part before the first "/".

    A bare id with no "/" returns the whole id lowercased.
    """
    text = str(model_id or "").strip().lower()
    if "/" in text:
        return text.split("/", 1)[0]
    return text


def select_judges(count: int, *, exclude_families: set[str]) -> list[dict]:
    """Pick up to `count` judges, never from an excluded family, never two of
    the same family.

    If exclusion leaves fewer than requested, return what is available — the
    CALLER must degrade (panel_verdict already refuses `pass` on a short
    panel); it must never silently substitute a judge from an excluded family.
    """
    excluded = {model_family(f) for f in (exclude_families or set())}
    selected: list[dict] = []
    seen_families: set[str] = set()
    for model_id, family in JUDGE_CANDIDATES:
        if len(selected) >= max(0, int(count)):
            break
        fam = model_family(family)
        if fam in excluded or fam in seen_families:
            continue
        seen_families.add(fam)
        selected.append({"model": model_id, "family": fam})
    return selected


# Atomic per-attribute questions only. No overall score: an aggregate number is
# exactly the surface verbosity/informativeness bias inflates.
ATTRIBUTE_QUESTION_PROMPT = """You are checking CHARACTER IDENTITY consistency between a reference
character sheet and frames from a generated anime video clip.

Images, in the order given to you: {frame_labels}
Anime style: {style}

Answer EACH numbered attribute question below independently, by looking at the
images. Do NOT give any overall quality score, overall impression, or summary
verdict — atomic per-attribute answers only.

## Attribute questions:
{attribute_lines}

## Output JSON (only this object, nothing else):
{{"attributes": [{{"attribute": "the short id in [brackets] for that question", "verdict": "pass"|"fail"|"uncertain", "evidence_frame": 0, "note": "one short sentence of evidence"}}]}}

Rules:
- One entry per question. "attribute" must be the SHORT ID shown in [brackets]
  at the start of that question (for example char_01_hair), copied exactly and
  with nothing added. Do NOT copy the question text — the id is how your two
  passes are matched to each other and to the checklist.
- "verdict": "pass" = clearly consistent with the reference; "fail" = clearly
  contradicts the reference; "uncertain" = you cannot clearly tell.
- "evidence_frame" is the 0-based index of the image that best supports your
  verdict, in the order the images were given to you.
- If an attribute is genuinely unclear (occluded, too small, motion-blurred,
  character not visible), you MUST answer "uncertain". Guessing is WORSE than
  uncertainty: a guessed "pass" ships a broken video, a guessed "fail" burns a
  regeneration for nothing.
- NO overall score. NO extra keys. NO prose outside the JSON object."""


ATTRIBUTE_ASPECTS = (
    ("hair", "hair colour, length and parting/side"),
    ("eyes", "eye colour"),
    ("accessories", "accessories (present, absent, and correct)"),
    ("outfit", "outfit details and colours"),
)

CHARACTER_COUNT_ID = "character_count"


def build_attribute_checks(characters, *, anchored_names=None) -> list[dict]:
    """Atomic checks as [{id, question, character, anchored}].

    WHY IDS AND NOT THE QUESTION TEXT. The previous version made the whole
    question — which embedded the character's entire visual_traits string,
    hundreds of characters long — the JSON key the model had to reproduce
    VERBATIM, twice, for the forward and reversed pass to be matched. No model
    reproduces that byte-identically, so the keys never matched the checklist,
    coverage always failed, and every scene of job 050a8a33 came back
    `indeterminate` with `attributes: {}`. The gate could not pass by
    construction. Short ids fix that without weakening anything: the refusal
    stays fail-closed, `verified` merely becomes reachable.

    IDs are `char_<ordinal>_<aspect>` — collision-proof BY CONSTRUCTION rather
    than by a de-duplication pass, because anime names are frequently non-ASCII
    and can slugify to empty or collide with each other.

    `anchored_names` is the set of characters whose reference sheet actually
    reached the judge. A character without one still gets its ids into the
    expected set (so coverage forces `indeterminate`) but no question is asked
    about it, because the judge would have no ground truth to answer against —
    guessing there is exactly how a hard gate turns into noise.
    """
    chars = list(characters or [])
    anchored = {str(n).strip() for n in (anchored_names or []) if str(n).strip()}
    checks: list[dict] = []
    for ordinal, char in enumerate(chars, start=1):
        name = (getattr(char, "name", "") or "").strip() or f"character {ordinal}"
        traits = (getattr(char, "visual_traits", "") or "").strip() or "(no description)"
        is_anchored = (not anchored) or (name in anchored)
        for aspect_key, aspect_text in ATTRIBUTE_ASPECTS:
            checks.append({
                "id": f"char_{ordinal:02d}_{aspect_key}",
                "character": name,
                "anchored": is_anchored,
                "question": (
                    f"Check {name}'s {aspect_text} against {name}'s character sheet "
                    f"reference image. Reference description of {name}: \"{traits}\"."
                ),
            })
    checks.append({
        "id": CHARACTER_COUNT_ID,
        "character": "",
        "anchored": True,
        "question": (
            f"Check that exactly {len(chars)} distinct character(s) are visible in "
            f"the video frames — no duplicates, no extra people."
        ),
    })
    return checks


def render_attribute_lines(checks) -> list[str]:
    """Prompt lines "[id] question" for the checks the judge can actually answer."""
    return [
        f"[{c['id']}] {c['question']}"
        for c in (checks or [])
        if isinstance(c, dict) and c.get("anchored", True) and c.get("id")
    ]


def expected_attribute_ids(checks) -> list[str]:
    """Every id that must come back answered — anchored or not (fail-closed)."""
    return [c["id"] for c in (checks or []) if isinstance(c, dict) and c.get("id")]


def normalize_attribute(text: str) -> str:
    """Comparison-stable form of an attribute question.

    The prompt demands the question text VERBATIM because that string is the
    identity used to match a judge's forward pass against its reversed pass and
    against the expected coverage set. Exact string equality would make the
    whole gate collapse to `indeterminate` on a trivial reformat (a changed
    capital letter or a doubled space between two passes), so the key is
    normalized: lowercased, whitespace collapsed, trailing punctuation dropped.
    Nothing inside the question is reworded — only its rendering is folded.
    """
    folded = " ".join(str(text or "").split()).lower()
    return folded.rstrip(" .:;!?")


def _attribute_map(result: dict) -> dict:
    """{normalized_attribute: verdict} from one model response; malformed rows skipped."""
    out: dict = {}
    rows = result.get("attributes") if isinstance(result, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        attr = normalize_attribute(row.get("attribute", ""))
        verdict = str(row.get("verdict", "")).strip().lower()
        if not attr:
            continue
        if verdict not in ("pass", "fail", "uncertain"):
            verdict = "uncertain"
        out[attr] = verdict
    return out


def combine_order_swap(result_a: dict, result_b: dict) -> dict:
    """Merge a forward-order pass and a reversed-order pass per attribute.

    Agreement -> that verdict. Disagreement -> "indeterminate" for that
    attribute (position bias made at least one answer ordering-dependent),
    recorded in `disagreements`. An attribute missing from either pass ->
    "indeterminate": an answer that only exists in one ordering is not an
    order-stable answer.
    """
    map_a = _attribute_map(result_a)
    map_b = _attribute_map(result_b)
    attributes: dict = {}
    disagreements: list = []
    for attr in sorted(set(map_a) | set(map_b)):
        va, vb = map_a.get(attr), map_b.get(attr)
        if va is None or vb is None:
            attributes[attr] = "indeterminate"
            disagreements.append(
                {"attribute": attr, "forward": va, "reversed": vb, "cause": "missing_in_one_pass"}
            )
        elif va == vb:
            attributes[attr] = va
        else:
            attributes[attr] = "indeterminate"
            disagreements.append(
                {"attribute": attr, "forward": va, "reversed": vb, "cause": "order_disagreement"}
            )
    return {
        "attributes": attributes,
        "disagreements": disagreements,
        "order_consistent": not disagreements,
    }


def panel_verdict(
    per_judge: list[dict], *, requested: int, expected_attributes: list | set,
) -> dict:
    """Aggregate judge contributions into one three-state panel verdict.

    `requested` is REQUIRED, not optional, and that is a safety property rather
    than a style choice: the panel cannot tell "two judges agreed" from "one
    judge answered because the other's family was excluded" by inspecting the
    contributions alone. When it defaulted to None a caller that forgot the
    argument silently got a `pass` out of a one-judge panel — the exact way a
    hard gate degrades into noise. A missing argument is now a TypeError at the
    call site instead of a false green in production.

    Each entry is a `combine_order_swap` result plus `judge` metadata and an
    optional `error` (a failed judge call — contributes `indeterminate`, never
    a pass). Rules, in this order:
      1. any judge reporting a hard `fail` on any attribute -> panel `fail`;
      2. else any `indeterminate`/`uncertain` attribute, any order
         disagreement, any judge error, or fewer judges than requested ->
         panel `indeterminate`;
      3. else `pass`.
    """
    per_judge = list(per_judge or [])
    merged: dict = {}
    failed: list = []
    indeterminate: list = []
    order_consistent = True
    errors: list = []
    _rank = {"pass": 0, "uncertain": 1, "indeterminate": 1, "fail": 2}
    for entry in per_judge:
        if entry.get("error"):
            errors.append(str(entry["error"]))
        if not entry.get("order_consistent", True):
            order_consistent = False
        for attr, verdict in (entry.get("attributes") or {}).items():
            prev = merged.get(attr)
            if prev is None or _rank.get(verdict, 1) > _rank.get(prev, 1):
                merged[attr] = verdict
    for attr, verdict in sorted(merged.items()):
        if verdict == "fail":
            failed.append(attr)
        elif verdict in ("uncertain", "indeterminate"):
            indeterminate.append(attr)

    # COVERAGE, not just absence of complaints. Without this an empty
    # `attributes` map from every judge produced "all 0 attributes passed" —
    # a gate with nothing to check reporting a pass, which is the fail-open
    # shape this whole panel exists to avoid. Every expected attribute must
    # come back with a recognised verdict, and a panel built from zero
    # questions is `indeterminate` by construction.
    # Tolerant of BOTH shapes on purpose. The old code passed a list of question
    # STRINGS; the current code passes ids (or the {id, question} check dicts).
    # A job.json persisted before this change and resumed after the mandatory
    # companion-worker reload is a real caller of the old shape, so normalize
    # rather than crash — or str(dict) would silently become the key.
    expected: list[str] = []
    for item in expected_attributes or []:
        raw = item.get("id", "") if isinstance(item, dict) else item
        key = normalize_attribute(raw)
        if key:
            expected.append(key)
    _known = ("pass", "fail", "uncertain", "indeterminate")
    uncovered = [a for a in expected if merged.get(a) not in _known]
    for attr in uncovered:
        if attr not in indeterminate:
            indeterminate.append(attr)
    no_questions = not expected

    short_panel = len(per_judge) < int(requested)
    if failed:
        verdict = "fail"
        reason = f"{len(failed)} attribute(s) failed on judge agreement, e.g.: {failed[0]}"
    elif indeterminate or not order_consistent or errors or short_panel or no_questions:
        verdict = "indeterminate"
        if no_questions:
            reason = "no attribute questions were built — a gate with nothing to check cannot pass"
        elif uncovered:
            reason = (
                f"{len(uncovered)} expected attribute(s) were not answered by the "
                f"panel, e.g.: {uncovered[0]}"
            )
        elif errors:
            reason = f"judge call failed: {errors[0]}"
        elif short_panel:
            reason = (
                f"only {len(per_judge)} of {requested} requested judges available "
                f"after family exclusion — degraded panel cannot pass"
            )
        elif not order_consistent:
            reason = "judge verdicts flipped with image ordering (position bias)"
        else:
            reason = f"{len(indeterminate)} attribute(s) uncertain, e.g.: {indeterminate[0]}"
    else:
        verdict = "pass"
        reason = f"all {len(merged)} attributes passed on both orderings across all judges"

    return {
        "verdict": verdict,
        "attributes": merged,
        "failed_attributes": failed,
        "indeterminate_attributes": indeterminate,
        "judges": [entry.get("judge") for entry in per_judge],
        "order_consistent": order_consistent,
        "reason": reason,
    }


def _histogram_vector(image_path: str) -> list[float]:
    """Normalized 3x8-bin per-channel colour histogram of a 64x64 RGB downscale."""
    from PIL import Image  # lazy: an import failure becomes a recorded error, not a crash

    with Image.open(image_path) as img:
        small = img.convert("RGB").resize((64, 64))
        pixels = list(small.getdata())
    bins = [0.0] * 24  # 3 channels x 8 bins
    for r, g, b in pixels:
        bins[min(7, r // 32)] += 1.0
        bins[8 + min(7, g // 32)] += 1.0
        bins[16 + min(7, b // 32)] += 1.0
    total = float(len(pixels)) or 1.0
    return [v / total for v in bins]


def identity_similarity_proxy(image_a: str, image_b: str) -> dict:
    """Weak colour-distribution similarity between two images. DIAGNOSTIC ONLY.

    This is a Pillow-only 64x64 colour-histogram cosine — a weak colour
    distribution proxy, nothing more. A DINOv2 embedding cosine would require
    adding torch, a heavy dependency this payload deliberately does not carry.
    It is NOT calibrated, has NO threshold, and this value must never gate
    anything: two frames of the same character in different lighting score
    low, two different characters on the same background score high.

    Never raises and never returns a number it did not compute: any failure
    returns value=None with the error type+message in `error`.
    """
    result = {
        "value": None,
        "method": "pillow_histogram_cosine_64px",
        "is_dinov2": False,
        "calibrated": False,
        "threshold": None,
        "gate": "diagnostic_only",
        "error": "",
    }
    try:
        va = _histogram_vector(image_a)
        vb = _histogram_vector(image_b)
        dot = sum(x * y for x, y in zip(va, vb))
        norm_a = sum(x * x for x in va) ** 0.5
        norm_b = sum(x * x for x in vb) ** 0.5
        if norm_a <= 0.0 or norm_b <= 0.0:
            result["error"] = "ValueError: zero-norm histogram (empty or unreadable image)"
            return result
        result["value"] = round(dot / (norm_a * norm_b), 4)
    except Exception as exc:  # noqa: BLE001 — diagnostic must never kill the pipeline
        msg = str(exc).strip()
        result["error"] = f"{type(exc).__name__}: {msg}" if msg else f"{type(exc).__name__}: (no message)"
    return result
