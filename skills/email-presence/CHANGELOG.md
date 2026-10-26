# Changelog

## 0.7.0

- Declare the mailbox's sending/reading identity and unverified sender status
  only to hosts advertising `presence_identity: 1`.
- Report direct mail, automated senders and configured whole-word signals;
  suggest no immediate wakes at Connect.
- Add a sending switch while retaining reads, drafts and mailbox tidying.
- Refuse reuse of a request id for a different payload; identical retries keep
  the first delivery and its receipt.
- Preserve authored outgoing text without an added signature or machine marker.
