# Synthetic worked examples

All situations, people and numerical facts here are invented test fixtures, not observations or quotations from real authors. Facts needed by a rewrite are supplied in the input. A different stylistic choice can also be valid when it preserves the contract.

## Scientific result: edit without upgrading evidence

Before: 'Our groundbreaking adapter achieved mean accuracy of 78.9% across five seeds on Dataset A, compared with 71.2% for full fine-tuning, while reducing training time by 31%. The standard deviation of adapter accuracy was 0.4 percentage points. This transformative approach did not improve accuracy on Dataset B.'

After: 'Across five seeds on Dataset A, the adapter achieved mean accuracy of 78.9%, compared with 71.2% for full fine-tuning, and reduced training time by 31%. Adapter accuracy had a standard deviation of 0.4 percentage points. The adapter did not improve accuracy on Dataset B.'

Keep: both datasets, five seeds, comparator, all numbers, standard deviation (not CI), percentage points, negative result. Remove unsupported evaluation. Do not add statistical significance.

## Documentation: repetition is useful

Before: 'Our seamless retry mechanism uses exponential backoff. The retry subsystem defaults to 3 attempts, with an initial delay of 500 ms and a maximum delay of 8 s. Use `--retry-attempts N` to change the number of attempts. If the final attempt fails, the retry mechanism raises `RetryExhaustedError`.'

After: 'The retry mechanism uses exponential backoff. By default, it makes 3 attempts, with an initial delay of 500 ms and a maximum delay of 8 s. Use `--retry-attempts N` to change the number of attempts. If the final attempt fails, the retry mechanism raises `RetryExhaustedError`.'

Keep: attempts, units, flag, exception and failure condition. Three attempts must not become three retries. Reusing 'retry mechanism' is intentional.

## Personal post: no change

'I missed the 7:40 train to Gdańsk on 14 March 2026—by maybe ten seconds. I stood on the platform, angry, cold, eating a gas-station croissant. That croissant was the best part of my week.'

Keep unchanged for a personal post. The dash, list and evaluation carry scene and voice. Do not replace 'maybe ten seconds' with a precise measurement or reuse this synthetic memory as a real person's experience.

## Slide: compress without hiding conditions

Input: 'Q2 2026 churn was 4.1%, down 2.3 percentage points from Q1. Churn means customers inactive for at least 90 days. The calculation excludes trial accounts. The data are preliminary.'

Slide:

**Preliminary Q2 2026 churn: 4.1%**

Down 2.3 percentage points from Q1.

Caption: Churn = inactive ≥90 days; trial accounts excluded.

The bold headline is functional, not a defect. Notes may expand the explanation; they must not be the only location of essential caveats in a circulated slide.

## Correspondence: retain consent and tone

Before: 'Hi Alex, I hope you’re well. I’m writing regarding the migration plan. Could you please approve the DNS cutover window in `migration-plan-v3.pdf` by 26 September 2026 at 15:00 UTC? If I haven’t heard from you by then, I will wait rather than book a window. Thanks.'

After: 'Hi Alex, could you please approve the DNS cutover window in `migration-plan-v3.pdf` by 26 September 2026, 15:00 UTC? If I haven’t heard from you by then, I’ll wait rather than book a window. Thanks.'

Keeping the friendly opening is also reasonable if the relationship calls for it. Never change the default to booking without approval.

## Real distinction: no change

'For this prototype, the queue stores jobs, not results. Results go in the archive.'

This contrast communicates an actual architectural boundary. Do not erase it for resembling a stock antithesis.

## Useful uncertainty: no change

'We suspect the cache change caused the regression, but we have not bisected it.'

'Caused the regression' alone would convert a hypothesis into a finding.

## Operational structure: no change

'1. Stop the worker. 2. **Back up** `state.db`. 3. Run the migration. 4. Start the worker.'

The ordered list and emphasis serve the task. This example does not assert that these are sufficient steps for any real system.

## Missing substance: do not invent a better story

Input: 'This release fundamentally improves reliability.' No release facts are provided.

Appropriate drafting response: ask which behavior changed and how reliability was measured, or retain a clearly marked placeholder such as '[Describe the verified behavior change]'. Do not invent status codes, fixes, latency numbers or user counts.

## Choppy rhythm: restore the supplied relation

Fact sheet: the disk being full is the established cause of the export failure.

Before: 'The export failed. The disk was full. We freed space. The retry succeeded.'

After: 'The export failed because the disk was full. We freed space, and the retry succeeded.'

Use 'because' only if the brief establishes that cause; if these are merely four observations, preserve the uncertainty rather than infer causality from sequence.

## Citation alignment: existence is not support

Input fact sheet: 'Paper A reports a vocabulary shift in a corpus of abstracts. It cannot identify individual AI-written abstracts.' Draft: 'Paper A proves this paragraph was written by AI.'

After: 'Paper A reports a corpus-level vocabulary shift; it does not establish the authorship of this paragraph.'

The repair changes an unsupported proposition with an explicit factual basis. In an audit, name that substantive correction rather than calling it punctuation cleanup.

## Preserve two different voices

Same supplied fact: a demo crashed twice and worked on the third try.

Technical status: 'The demo crashed on the first two attempts and succeeded on the third.'

Light personal voice: 'Third try: the demo finally worked, after two crashes.'

Both retain the facts. Neither needs a grand lesson, invented feeling or generic 'humanizing' typo.
