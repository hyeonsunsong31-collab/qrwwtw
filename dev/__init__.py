"""
dev -- JARVIS's local developer (v1.5.0): workspaces you choose, where JARVIS builds new
programs and keeps changing a product you sent it until every request is done and checked.

  workspaces.py  session slots: data/workspaces/<id>/ with input/ (what you sent), project/
                 (the working tree, with history), output/ (deliverables) and the stacked
                 requests with their progress
  history.py     one commit per finished request, so "undo the last request" is exact
  scheduler.py   one job at a time (the graphics card is the bottleneck), a queue across all
                 workspaces, pause / resume / stop, resumes after a restart
  agent.py       the coding loop: requirements -> acceptance checklist -> plan -> scaffold ->
                 write / run / verify / fix, without giving up, until every check passes
  protocol.py    how the model acts: native tool calls (bigger models) or whole files in a
                 plain-text format (small models), switching by itself when one fails
  tools.py       the workspace terminal and file tools (inside the workspace: free; outside,
                 software installs, destructive commands: ask first)
  procs.py       running programs: output capture, time and memory limits, whole-tree kill
  verify.py      the definition of done: it builds, its tests pass, it runs, it does what
                 each criterion says (browser checks for web pages and games)
  files.py       opening what you send: archives, .exe/.dll (source recovery where the format
                 allows it), .gba ROMs (header, IPS/BPS patches, tiles), documents, media
  convert.py     changing a file's format (images, audio/video, documents, data, archives, 3D,
                 code -> executable); anything else is built by the coding loop
  models.py      the coding model: the largest Qwen2.5-Coder that fits this PC's memory
  toolchains.py  compilers and runtimes, found or installed on demand (you are asked first)
  serve.py       links for web products: this PC, your Wi-Fi network, or a public link
  service.py     glue to the JARVIS server: events for the HUD, permission prompts, delivery
  cli.py         `jarvis-tool`: the file utilities above, runnable by the coding model

Everything runs on this PC with local models and open-source tools: no accounts, no API keys,
no credits, no usage limits, nothing to pay.
"""
VERSION = "1.5.0"
