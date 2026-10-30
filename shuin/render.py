"""Static board. Human surface: claims are shown quoted, apart from confirmed facts."""

import html
import json
import time
from base64 import b64encode
from functools import lru_cache
from pathlib import Path

from shuin.voice import format_left

_FONT_DIR = Path(__file__).resolve().parent / "fonts"


@lru_cache(maxsize=1)
def _font_css() -> str:
    display = b64encode((_FONT_DIR / "big-shoulders-700.woff2").read_bytes()).decode("ascii")
    mono = b64encode((_FONT_DIR / "jetbrains-mono-400.woff2").read_bytes()).decode("ascii")
    return f"""
@font-face {{
  font-family: "Shuin Display";
  src: url(data:font/woff2;base64,{display}) format("woff2");
  font-weight: 700;
  font-style: normal;
  font-display: swap;
}}
@font-face {{
  font-family: "Shuin Mono";
  src: url(data:font/woff2;base64,{mono}) format("woff2");
  font-weight: 400;
  font-style: normal;
  font-display: swap;
}}
"""


def write_board(ledger, directory) -> tuple[Path, Path]:
    snap = ledger.snapshot(include_claims=True)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    html_path = directory / "board.html"
    json_path = directory / "board.json"
    html_path.write_text(render_html(snap), encoding="utf-8")
    public = {key: value for key, value in snap.items() if key != "claims"}
    for task in public.get("tasks") or []:
        for item in task.get("evidence") or []:
            item.pop("uri", None)
    json_path.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")
    return html_path, json_path


def render_html(snap: dict) -> str:
    esc = html.escape
    voice = snap.get("voice") or "The board is still."
    tasks = [task for task in snap.get("tasks") or [] if task.get("status") != "dropped"]
    queue = snap.get("queue") or []
    claims = snap.get("claims") or []
    overlaps = snap.get("overlaps") or []
    blocked = [task for task in tasks if task.get("status") == "blocked"]
    evidence = _evidence_rows(snap)
    confirmed = [task for task in tasks if task.get("pr")]
    sample = snap.get("sample")
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    empty = not tasks
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta http-equiv="refresh" content="30">
<title>朱 Shuin</title>
<style>
{_font_css()}
:root {{
  --night: #09090b;
  --panel: #101114;
  --paper: #f4efe9;
  --ash: #d9cdc6;
  --dim: #b7a8a2;
  --seal: #ff3b2f;
  --cyan: #5ef2ee;
  --magenta: #ff4ec8;
  --line: rgba(244, 239, 233, 0.14);
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: var(--night); color: var(--paper); }}
body {{
  font-family: "Shuin Mono", "JetBrains Mono", ui-monospace, monospace;
  font-size: 13px;
  line-height: 1.45;
  min-height: 100vh;
}}
body::before {{
  content: "";
  pointer-events: none;
  position: fixed;
  inset: 0;
  background: repeating-linear-gradient(
    to bottom,
    rgba(255, 255, 255, 0.028),
    rgba(255, 255, 255, 0.028) 1px,
    transparent 1px,
    transparent 3px
  );
  z-index: 4;
}}
.wrap {{
  position: relative;
  z-index: 1;
  width: min(1180px, calc(100% - 32px));
  margin: 0 auto;
  padding: 28px 0 72px;
}}
header {{
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 18px 28px;
  align-items: end;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 26px;
}}
.brand {{ display: flex; align-items: baseline; gap: 12px; }}
.brand-mark {{
  font-family: "Iowan Old Style", "Palatino Linotype", "Hiragino Mincho ProN", "WenQuanYi Micro Hei", serif;
  color: var(--seal);
  font-size: 28px;
  text-shadow: 0 8px 22px rgba(255, 59, 47, 0.35);
}}
h1 {{
  font-family: "Shuin Display", "Arial Narrow", Impact, sans-serif;
  font-size: 56px;
  line-height: 0.8;
  font-weight: 700;
  letter-spacing: -0.03em;
  margin: 0;
  color: var(--seal);
  text-shadow: 0 10px 28px rgba(255, 59, 47, 0.28);
}}
.kicker {{
  margin: 6px 0 0;
  color: var(--ash);
  font-size: 12px;
}}
.kicker span {{ letter-spacing: 0.18em; }}
.voice {{
  font-family: "Shuin Display", "Arial Narrow", Impact, sans-serif;
  font-size: 32px;
  line-height: 0.95;
  letter-spacing: -0.03em;
  margin: 0;
}}
.clock {{ color: var(--dim); text-align: right; }}
.counts {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-top: 14px;
  color: var(--ash);
}}
.counts b {{ color: var(--paper); font-weight: 400; }}
.sample {{ color: var(--magenta); margin: 8px 0 0; }}
.desk {{
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 28px 36px;
}}
.span {{ grid-column: 1 / -1; }}
h2 {{
  font-family: "Shuin Display", "Arial Narrow", Impact, sans-serif;
  font-size: 26px;
  line-height: 1;
  letter-spacing: -0.02em;
  font-weight: 700;
  margin: 0 0 12px;
}}
.mark {{
  font-family: "Hiragino Mincho ProN", "Noto Serif CJK JP", "WenQuanYi Micro Hei", serif;
  color: var(--seal);
  font-size: 15px;
  margin-right: 8px;
  font-weight: 400;
}}
.note {{ color: var(--dim); margin: 0 0 10px; }}
.queue-item, .row, .ev, .claim, .ov, .conf {{
  border-top: 1px solid var(--line);
  padding: 9px 0;
}}
.queue-item {{ display: grid; grid-template-columns: 28px 1fr; gap: 8px; }}
.pos {{ color: var(--seal); }}
.row {{
  display: grid;
  grid-template-columns: 58px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
}}
.row-main {{
  display: grid;
  grid-template-columns: 72px minmax(0, 1.5fr) minmax(90px, 0.7fr) minmax(120px, 0.8fr);
  gap: 8px 14px;
  align-items: baseline;
}}
.stack {{ padding-left: calc(var(--d) * 14px); }}
.title {{ color: var(--paper); }}
.meta {{ color: var(--ash); }}
.sha {{ color: var(--dim); }}
.lamp {{
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 6px;
  border-radius: 50%;
  background: var(--ash);
  vertical-align: 1px;
}}
.lamp.run {{
  background: var(--cyan);
  box-shadow: 0 0 8px rgba(94, 242, 238, 0.85);
  animation: pulse 2.4s ease-in-out infinite;
}}
.lamp.review, .lamp.bad {{ background: var(--magenta); box-shadow: 0 0 8px rgba(255, 78, 200, 0.65); }}
.lamp.ok {{ background: var(--cyan); }}
@keyframes pulse {{ 50% {{ opacity: 0.28; }} }}
.badge {{
  display: inline-block;
  border: 1px solid var(--magenta);
  color: var(--paper);
  padding: 0 4px;
  font-size: 11px;
}}
.rebase {{
  display: inline-block;
  border: 1px solid var(--cyan);
  color: var(--paper);
  padding: 0 4px;
  font-size: 11px;
}}
.lower {{
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 28px 36px;
}}
.confirmed {{
  background: var(--panel);
  padding: 14px 14px 6px;
  box-shadow: 0 16px 30px rgba(0, 0, 0, 0.28);
}}
blockquote {{
  margin: 4px 0 0;
  color: var(--ash);
  white-space: pre-wrap;
}}
.tag {{ color: var(--magenta); font-size: 11px; }}
.hint {{ color: var(--dim); }}
.seal {{ width: 52px; height: 52px; display: block; }}
.seal-stamped {{ filter: drop-shadow(0 8px 10px rgba(255, 59, 47, 0.25)); }}
@media (max-width: 860px) {{
  header, .desk, .lower, .row-main {{ grid-template-columns: 1fr; }}
  .clock {{ text-align: left; }}
  h1 {{ font-size: 44px; }}
  .voice {{ font-size: 28px; }}
  .span, .lower {{ grid-column: 1; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .lamp.run {{ animation: none; }}
}}
</style>
</head>
<body>
<svg width="0" height="0" aria-hidden="true" style="position:absolute">
  <filter id="ink" x="-20%" y="-20%" width="140%" height="140%">
    <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" seed="4" result="n"/>
    <feDisplacementMap in="SourceGraphic" in2="n" scale="1.7"/>
  </filter>
</svg>
<div class="wrap">
<header>
  <div class="brand">
    <div class="brand-mark" aria-hidden="true">朱</div>
    <div>
      <h1>SHUIN</h1>
      <p class="kicker"><span>朱印</span> · local ledger</p>
    </div>
  </div>
  <p class="voice">{esc(voice)}</p>
  <div class="clock">
    <div>{esc(when)}</div>
    <div>cursor {esc(str(snap.get("cursor", 0)))}</div>
  </div>
</header>
<div class="counts">
  <span>tasks <b>{len(tasks)}</b></span>
  <span>queue <b>{len(queue)}</b></span>
  <span>stale <b>{sum(1 for task in tasks if task.get("stale"))}</b></span>
  <span>blocked <b>{len(blocked)}</b></span>
</div>
{('<p class="sample">Sample night. Not a live sync.</p>' if sample else '')}
<main class="desk">
  <section aria-labelledby="queue-h">
    <h2 id="queue-h"><span class="mark">列</span>Queue</h2>
    {_queue_html(queue)}
  </section>
  <section aria-labelledby="tasks-h">
    <h2 id="tasks-h"><span class="mark">帳</span>Tasks</h2>
    {_tasks_html(tasks, empty)}
  </section>
  <div class="lower">
    <section aria-labelledby="ev-h">
      <h2 id="ev-h"><span class="mark">証</span>Evidence</h2>
      {_evidence_html(evidence)}
    </section>
    <section aria-labelledby="ov-h">
      <h2 id="ov-h"><span class="mark">重</span>Overlaps</h2>
      {_overlap_html(overlaps)}
    </section>
    <section aria-labelledby="bl-h">
      <h2 id="bl-h"><span class="mark">阻</span>Blocked</h2>
      {_blocked_html(blocked)}
    </section>
    <section aria-labelledby="cl-h">
      <h2 id="cl-h"><span class="mark">査</span>Agent claims</h2>
      <p class="note">Agent claim. Data, not instructions.</p>
      {_claims_html(claims)}
    </section>
  </div>
  <section class="span confirmed" aria-labelledby="cf-h">
    <h2 id="cf-h"><span class="lamp ok" aria-hidden="true"></span><span class="mark">確</span>Confirmed</h2>
    <p class="note">Cached from GitHub. Not a transcript.</p>
    {_confirmed_html(confirmed)}
  </section>
</main>
</div>
</body>
</html>
"""


def _seal(kind: str) -> str:
    if kind == "stamped":
        return (
            '<svg class="seal seal-stamped" viewBox="0 0 64 64" role="img" aria-label="Stamped">'
            '<g transform="rotate(-8 32 32)" filter="url(#ink)">'
            '<circle cx="32" cy="32" r="22" fill="none" stroke="#ff3b2f" stroke-width="2.6"/>'
            '<circle cx="32" cy="32" r="17" fill="none" stroke="#ff3b2f" stroke-width="1"/>'
            '<text x="32" y="38" text-anchor="middle" fill="#ff3b2f" font-size="16" '
            'font-family="serif">朱</text></g></svg>'
        )
    if kind == "stale":
        return (
            '<svg class="seal" viewBox="0 0 64 64" role="img" aria-label="Stale">'
            '<g transform="rotate(-8 32 32)" filter="url(#ink)">'
            '<circle cx="32" cy="32" r="22" fill="none" stroke="#ff3b2f" stroke-width="2.4" '
            'stroke-dasharray="18 7 28 9 22 8"/>'
            '<path d="M22 16 L34 30 L26 48" fill="none" stroke="#ff3b2f" stroke-width="1.5"/>'
            '<text x="32" y="38" text-anchor="middle" fill="#ff3b2f" font-size="16" '
            'font-family="serif">朱</text></g></svg>'
        )
    if kind == "blocked":
        return (
            '<svg class="seal" viewBox="0 0 64 64" role="img" aria-label="Blocked">'
            '<rect x="6" y="26" width="52" height="12" fill="#050505" stroke="#ff3b2f" stroke-width="1.2"/>'
            "</svg>"
        )
    return (
        '<svg class="seal" viewBox="0 0 64 64" role="img" aria-label="Pending">'
        '<g transform="rotate(-8 32 32)">'
        '<circle cx="32" cy="32" r="22" fill="none" stroke="#d9cdc6" stroke-width="1.6" stroke-dasharray="3 3.5"/>'
        '<text x="32" y="38" text-anchor="middle" fill="#d9cdc6" font-size="16" '
        'font-family="serif">朱</text></g></svg>'
    )


def _lamp(status: str) -> str:
    if status == "verifying":
        return '<span class="lamp run" aria-hidden="true"></span>'
    if status == "in_review":
        return '<span class="lamp review" aria-hidden="true"></span>'
    if status in ("done",):
        return '<span class="lamp ok" aria-hidden="true"></span>'
    if status == "blocked":
        return '<span class="lamp bad" aria-hidden="true"></span>'
    return '<span class="lamp" aria-hidden="true"></span>'


def format_dependencies(dependencies) -> str:
    """One line for the text board and the HTML board. Empty when there are none."""
    if not dependencies:
        return ""
    parts = []
    for dep in dependencies:
        if dep.get("kind") == "pr":
            label = f"{dep.get('repo')}#{dep.get('number')}"
            fallback = "open" if dep.get("open") else "merged"
        else:
            label = str(dep.get("id") or "")
            fallback = "open" if dep.get("open") else "done"
        parts.append(f"{label} {dep.get('state') or fallback}")
    return "deps " + ", ".join(parts)


def _claim_plain(value) -> str:
    if isinstance(value, dict):
        text = str(value.get("text_quoted") or "")
        if value.get("label") == "agent_claim":
            return f"agent claim {text}".rstrip()
        return text
    return str(value or "")


def _tasks_html(tasks: list[dict], empty: bool) -> str:
    if empty:
        return '<p class="voice">No cards. The night is quiet.</p><p class="hint">shuin task add --title "..."</p>'
    rows = []
    for task in tasks:
        pr = task.get("pr") or {}
        pr_label = f'{pr.get("repo")}#{pr.get("number")}' if pr else "—"
        head = (pr.get("head_sha") or pr.get("pending_head") or "—")[:7] if pr else "—"
        runs = "".join(
            f'<div class="sha">{html.escape(str(run.get("agent_id") or "—"))} · attempt {html.escape(str(run.get("attempt") or ""))} · {html.escape(str(run.get("model") or ""))}</div>'
            for run in (task.get("runs") or [])
            if run.get("model")
        )
        rebase = '<span class="rebase">needs rebase</span>' if pr.get("needs_rebase") else ""
        depth = int(task.get("depth") or 0)
        dep_line = format_dependencies(task.get("dependencies"))
        deps = f'<div class="sha">{html.escape(dep_line)}</div>' if dep_line else ""
        rows.append(
            '<article class="row{blocked}" style="--d:{depth}">'
            "{seal}"
            '<div class="row-main stack">'
            "<div>{lamp}{tid}</div>"
            '<div><div class="title">{title}</div><div class="meta">{owner} · {status} {rebase}</div>'
            '<div class="sha">{voice}</div>{deps}{runs}</div>'
            "<div>{pr}</div>"
            '<div class="sha">{head}</div>'
            "</div></article>".format(
                blocked=" is-blocked" if task.get("status") == "blocked" else "",
                depth=depth,
                seal=_seal(task.get("seal") or "pending"),
                lamp=_lamp(task.get("status") or ""),
                tid=html.escape(task["id"]),
                title=html.escape(_claim_plain(task.get("title"))),
                owner=html.escape(task.get("owner") or "unassigned"),
                status=html.escape(
                    (task.get("status") or "")
                    + (f" · {task['closed_by']}" if task.get("status") == "done" and task.get("closed_by") else "")
                    + (f" · {task['closed_reason']}" if task.get("status") == "done" and task.get("closed_reason") else "")
                ),
                rebase=rebase,
                voice=html.escape(task.get("voice") or ""),
                deps=deps,
                runs=runs,
                pr=html.escape(pr_label),
                head=html.escape(head),
            )
        )
    return "".join(rows)


def _queue_html(queue: list[dict]) -> str:
    if not queue:
        return '<p class="note">Queue empty.</p>'
    rows = []
    for item in queue:
        left = ""
        if item.get("state") == "running":
            left = format_left(item.get("lease_left_seconds") or 0)
        holder = item.get("claimed_by") or "—"
        rows.append(
            '<div class="queue-item"><div class="pos">{pos:02d}</div><div>'
            "<div>{lamp}{repo}#{pr}</div>"
            '<div class="meta">{state} · {holder} {left}</div>'
            '<div class="sha">{task} · {sha}</div>'
            "</div></div>".format(
                pos=item["position"],
                lamp='<span class="lamp run" aria-hidden="true"></span>' if item.get("state") == "running" else '<span class="lamp" aria-hidden="true"></span>',
                repo=html.escape(str(item.get("repo") or "")),
                pr=html.escape(str(item.get("pr") or "")),
                state=html.escape(str(item.get("state") or "")),
                holder=html.escape(str(holder)),
                left=html.escape(left),
                task=html.escape(str(item.get("task_id") or "")),
                sha=html.escape(str(item.get("commit_sha") or "")[:7]),
            )
        )
    return "".join(rows)


def _evidence_rows(snap: dict) -> list[dict]:
    rows = []
    for task in snap.get("tasks") or []:
        pr = task.get("pr")
        label = f"{pr['repo']}#{pr['number']}" if pr else ""
        for item in task.get("evidence") or []:
            rows.append(
                {
                    "task_id": task["id"],
                    "pr": label,
                    "kind": item.get("kind") or "",
                    "sha": item.get("commit_sha") or "",
                    "state": item.get("result") or "info",
                    "stale": bool(item.get("stale")),
                    "uri": item.get("uri") or "",
                }
            )
    return rows


def _evidence_html(rows: list[dict]) -> str:
    if not rows:
        return '<p class="note">No evidence.</p>'
    out = []
    for row in rows:
        badge = ' <span class="badge">STALE</span>' if row["stale"] else ""
        out.append(
            '<div class="ev"><div>{task} · {pr}</div>'
            '<div class="meta">{kind} · {state}{badge}</div>'
            '<div class="sha">{sha}</div>'
            '<div class="sha">{uri}</div></div>'.format(
                task=html.escape(row["task_id"]),
                pr=html.escape(row["pr"]),
                kind=html.escape(row["kind"]),
                state=html.escape(row["state"]),
                badge=badge,
                sha=html.escape(row["sha"][:12]),
                uri=html.escape(row.get("uri") or ""),
            )
        )
    return "".join(out)


def _overlap_html(overlaps: list[dict]) -> str:
    if not overlaps:
        return '<p class="note">No overlap.</p>'
    rows = []
    for item in overlaps:
        who = ", ".join(
            html.escape(f"{pr['task_id']} {pr['repo']}#{pr['pr']}") for pr in item["prs"]
        )
        rows.append(
            f'<div class="ov"><div>{html.escape(item["path"])}</div><div class="meta">{who}</div></div>'
        )
    return "".join(rows)


def _blocked_html(tasks: list[dict]) -> str:
    if not tasks:
        return '<p class="note">No one is blocked.</p>'
    rows = []
    for task in tasks:
        rows.append(
            '<div class="row is-blocked">{seal}<div><div>{tid} {title}</div>'
            '<div class="meta">{reason}</div></div></div>'.format(
                seal=_seal("blocked"),
                tid=html.escape(task["id"]),
                title=html.escape(_claim_plain(task.get("title"))),
                reason=html.escape(task.get("blocked_reason") or ""),
            )
        )
    return "".join(rows)


def _claims_html(claims: list[dict]) -> str:
    if not claims:
        return '<p class="note">No claims.</p>'
    rows = []
    for claim in claims[:12]:
        who = claim.get("agent") or ""
        if claim.get("on_behalf_of"):
            who = f"{who} via lead for {claim['on_behalf_of']}"
        when = ""
        if claim.get("at"):
            when = time.strftime("%H:%M UTC", time.gmtime(claim["at"]))
        rows.append(
            '<div class="claim"><div class="tag">agent claim · {who} · {kind} · {task}</div>'
            "<blockquote>{text}</blockquote>"
            '<div class="sha">{when}</div></div>'.format(
                who=html.escape(who),
                kind=html.escape(str(claim.get("kind") or "")),
                task=html.escape(str(claim.get("task_id") or "")),
                text=html.escape(str(claim.get("text_quoted") or "")),
                when=html.escape(when),
            )
        )
    return "".join(rows)


def _confirmed_html(tasks: list[dict]) -> str:
    if not tasks:
        return '<p class="note">Nothing synced.</p>'
    rows = []
    for task in tasks:
        pr = task["pr"]
        synced = "never"
        if pr.get("last_synced_at"):
            synced = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(pr["last_synced_at"]))
        receipt = task.get("receipt") or {}
        gate = receipt.get("classification") or "—"
        rows.append(
            '<div class="conf"><div>{task} · {repo}#{num}</div>'
            '<div class="meta">branch {branch} · base {base} · {state}</div>'
            '<div class="meta">ci {ci} · review {review} · gate {gate}</div>'
            '<div class="sha">head {head}</div>'
            '<div class="sha">node {node} · synced {synced}</div></div>'.format(
                task=html.escape(task["id"]),
                repo=html.escape(str(pr.get("repo") or "")),
                num=html.escape(str(pr.get("number") or "")),
                branch=html.escape(str(pr.get("branch") or "—")),
                base=html.escape(str(pr.get("base_branch") or "—")),
                state=html.escape(str(pr.get("state") or "—")),
                ci=html.escape(str(pr.get("ci") or "—")),
                review=html.escape(str(pr.get("review") or "—")),
                gate=html.escape(str(gate)),
                head=html.escape(str(pr.get("head_sha") or "—")),
                node=html.escape(str(pr.get("node_id") or "—")),
                synced=html.escape(synced),
            )
        )
    return "".join(rows)
