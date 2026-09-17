# SberCA Root Ext

Public root certificate supplied by the repository owner on 2026-09-10 as
`SberCA_Root_Ext.crt`. Stored in PEM format; no private key is included.

- Subject / issuer: `CN=SberCA Root Ext,O=Sberbank of Russia,C=RU`
- Validity (UTC): 2020-10-29 14:51:43 — 2040-10-24 14:51:43
- Certificate SHA-256 fingerprint: `e77059cd24606a8a24043100ec7e23db917bfc85054ee08b79ca71b43b8f4690`

The CA constraint and self-signature were checked locally. Its identity was
checked against the live gateway chain and a public Sber bundle on 2026-09-17
(see the verification record below); this is not an authenticated bank
publication of the root. This is a single root, not a
complete intermediate chain. Used as the default trust root for PROM only.
User-installed CA material takes precedence. Review replacement certificates and
update this record and catalog hashes when the bank rotates its CA.

## Verification record (2026-09-17, OuroborosHub review of PR #58)

- `openssl s_client -connect fintech.sberbank.ru:9443 -servername fintech.sberbank.ru -showcerts`
  returned the chain `CN=fintech.sberbank.ru` <- `CN=SberCA Ext` <- `CN=SberCA Root Ext`.
  The depth-2 certificate is DER-identical to this file (SHA-256
  `e77059cd24606a8a24043100ec7e23db917bfc85054ee08b79ca71b43b8f4690`) and the
  gateway sends the intermediate itself, so this single root is a sufficient
  trust anchor for PROM. (The capture reports `verify error 19` only because the
  reviewing machine's trust store does not contain this private root.)
- The public Sber bundle
  `https://3dsec.sberbank.ru/techportal/lib/exe/fetch.php/certificates:add:cert.rar`
  (`Cert_CA.pem` = `CN=SberCA Ext`, SHA-256
  `b9b14bf1cbc063d106d095eac17f1bc93720031ac2894bac557b9bf91e2c1ae8`) verifies
  against this root with `openssl verify -CAfile`.
- Limits: this establishes that the file is the root the gateway presents and
  that a bank-published intermediate chains to it; it is not a bank statement
  about the root itself. The `chain_prom.zip` linked from the Sber API TLS
  documentation carries a different PKI (`SberAPI Root CA` / `SberAPI CA`) that
  this MCP gateway does not present. Keep this root for PROM; install a
  replacement through `install_ca_certificate` if the bank rotates its CA.
