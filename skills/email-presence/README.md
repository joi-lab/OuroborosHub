# Email Presence

Version 0.8.0 negotiates timely intake (Presence continuation version 2) with
Hosts that list it: a turn's early custody answer frees its thread, new mail and
admitted turns' polls run in separate loops so neither waits for the other, and
replies answer the last mail the author covered. Version 0.7.0 added mailbox identity, observable mail
facts, configured word signals and a sending switch. New connections suggest no
immediate wakes; choose those per place. Sender authentication remains
explicitly unverified.

See [SKILL.md](SKILL.md) for setup, exact fact rules, compatibility and residuals,
and [CHANGELOG.md](CHANGELOG.md) for release changes.

Run offline tests from this directory with an isolated temporary data root:
`python3 -m pytest -p no:cacheprovider -q -rs tests`.
