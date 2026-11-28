UV M1 adapter backup evidence
================================

Important limitation
--------------------
The backup instruction arrived after cli.py, protocol.py, runner.py and
worker.py had already been modified in the previous turn. Their genuine
pre-change bytes are not available in this workspace (there is no repository
checkout or existing backup). No modified copy is labelled or presented as an
original here.

Genuine original preserved before modification
----------------------------------------------
execution_contract.py
  backup: _uv_m1_backup/execution_contract.py
  sha256: 7F3C7BB7F2628074AAE4132D3FCC83E2097903D34D5A59D9A26B7FBA1551D5B4

Pre-change SHA-256 recorded before the previous adapter edits
--------------------------------------------------------------
cli.py       600151741275020C2CBF7668CC06D3BD92CCBFA7E26B7E085C09F540481A51F3
protocol.py  29BFB8FD65E82B75596FFEDBF5A1955D6A205D19AA69FA75DEA3061BDF0A7263
runner.py    BF7142E60899B0D1482FB2323E490E590F433F84EB4F38E02960193FF0B409A9
worker.py    033200FB171E447DEBA950BE91375F43656B732977E395713D09BC08BC7A3E1B

These hashes identify the original files but are not file contents. Do not
reconstruct "originals" by relabelling current post-change copies.

Current post-change hashes (NOT originals)
------------------------------------------
cli.py                  A2D27FE34D7CE1B77BF6C1818D171A50AABDC0EB6E00D8467E6D1D98E4EEF8F3
protocol.py             E4B1302F31102006547608859124B8A22FA01E7274B69327EA40D0144E754FA9
runner.py               213460988A0EDCFAD3850FAB5924B86E09A99895D231FCB9FA8E501511F254E2
worker.py               D7E26613370809B77997556043E27099F025594A51E3E0A13757094145C6EF7F
execution_contract.py   AECA7826699433FA361CDFC306C53A574D197363AB5FF96C951BCE2E99D8EE44

Cross-domain identity note
--------------------------
lighting_identity.py and geometry_identity.py hash the full
tools/blenderctl/*.py surface. Adding the UV adapter files and changing
execution_contract.py therefore changes their implementation identity.
Their old resume reports/receipts must not be reused as current evidence.
