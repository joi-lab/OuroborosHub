---
name: anti-slop
description: Evidence-informed writing prompt and checklist for substantive, calibrated prose that preserves voice and fits the reader, genre, and requested language.
version: 0.1.0
type: instruction
permissions: []
when_to_use: Drafting or revising human-facing prose, including scientific or technical writing, documentation, posts, slides, speeches, and correspondence; or when asked to audit writing for slop.
model_experience:
  what_model_sees: Read the body when writing; open only the needed reference. Adds no tools and is not auto-injected into children; a parent may pass the body to a writing subagent.
  token_effect: A compact core on demand; optional domain, signal, example and evidence references add context only when read.
---

# Anti-slop: write with something to say

Use this as a writing instruction, not an authorship detector. Here, slop means prose whose polish disguises missing substance or makes readers reconstruct work the writer should have done. This is an editorial working definition, not a scientific diagnostic category. AI assistance and poor writing are different questions.

## Before writing

Infer the reader, purpose, genre, requested output language and degree of editing from the request. Ask only when a material ambiguity prevents a useful answer. Explicit target-language instructions outrank the language of the request. Follow applicable venue and disclosure requirements.

Identify what must survive: claims, numbers, units, dates, names, identifiers, quotations, attribution, negations, comparison conditions, uncertainty, commitments and the author's position. In a revision, preserve intentional voice and meaningful texture. In a new draft, use only supplied or checked facts; a missing detail is a question or a clearly marked placeholder, not permission to invent.

## While writing

- Make the point the reader needs. Explain the relation, mechanism or consequence where it matters; do not orbit around an unnamed insight.
- Support the exact claim. A real citation about the same topic is not necessarily supporting evidence. Never fabricate a reference, anecdote, experience, number, quotation or consensus to make prose vivid.
- Match confidence to evidence. Keep qualifications that limit scope or express real uncertainty. Remove empty hedging or unsupported certainty only after checking their function. Preserve ambiguity that the genre deliberately needs.
- Choose structure for use. Lists, headings, tables, repetition and short sentences can be excellent in instructions or slides. Continuous prose can be better for an argument or personal message. Neither minimal Markdown nor maximum brevity is the goal.
- Let rhythm follow the thought. Avoid unintentional repetition of the same hook, contrast, triad, paragraph shape or conclusion. Do not manufacture irregularity, slang, typos or a generic terse persona to sound human.
- Keep the writer's distinctions, humor, warmth, formality and stance when they serve the request. Do not turn every voice into the same polished neutral voice.

## Final private check

1. **Substance:** Is the actual point stated? Do examples explain it? Does a sentence add meaning, navigation, emphasis, rhythm or interpersonal value? Cut only what serves no purpose here.
2. **Fidelity:** Compare the result with the brief/source. Did any fact, qualification, referent, causal relation, unit, commitment or voice change? Verify added facts separately or remove them. Do not silently correct a disputed fact while calling the change stylistic.
3. **Fit:** Is this appropriate for this reader, genre and language? Are familiar devices doing real work, or merely creating borrowed emphasis?

Return what was requested, usually the text itself. Do not append a ritual checklist, diagnosis or explanation. Briefly flag a material factual concern or preservation conflict; do not silently resolve an ambiguous referent, deadline/time zone or technical term, including in translation. For an explicit audit, identify concrete passages, reader cost and the smallest justified change. For revision, no change is a valid outcome; do not announce 'no change' when simply drafting new text.

## Boundaries

An em dash, a word, polished grammar, a contrast, or a detector score does not prove machine authorship or bad quality. Signals trigger inspection, not automatic deletion. A specific user style preference may guide the current task; it is not a universal law. Never optimize for deceiving detectors or hiding required AI-use disclosure.

The references and examples are in English. Output adapts to the task. Do not translate an English list of 'tells' into another language and claim validation: punctuation, register and rhetorical conventions differ. Where competence or evidence is insufficient, make conservative suggestions and disclose the need for a competent reader. This package has no claim of universal language or current-model validation.

## Optional references

- [Signals and counterexamples](references/signals.md): inspect a suspected pattern without banning it.
- [Domains](references/domains.md): preserve different contracts across genres.
- [Worked examples](references/examples.md): synthetic before/after, no-change and overcorrection cases.
- [Evidence](references/evidence.md): dated empirical findings, opinions, model-specific limits and sources.

## Delegating writing

Pass this body (or a resolvable path the child can actually read), the concrete writing brief, source facts, preservation requirements and only relevant reference material. Ask for the requested deliverable, not a checklist performance. Do not assume a child inherits this skill or your private preferences. Carry only preferences relevant and authorized for that task; never copy private biography into a reusable package.
