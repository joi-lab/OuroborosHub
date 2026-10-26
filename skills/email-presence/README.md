# Email Presence

Version 0.7.0 adds mailbox identity, observable mail facts, configured word
signals and a sending switch. New connections suggest no immediate wakes;
choose those per place. Sender authentication remains explicitly unverified.

See [SKILL.md](SKILL.md) for setup, exact fact rules, compatibility and residuals,
and [CHANGELOG.md](CHANGELOG.md) for release changes.

Run offline tests from this directory with an isolated temporary data root:
`python3 -m pytest -p no:cacheprovider -q -rs tests`.
