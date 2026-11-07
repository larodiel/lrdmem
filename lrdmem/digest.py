"""One read-only summary of what in the store needs a human, meant for a scheduled run (cron, launchd, a scheduler).

It changes nothing: consolidation already runs at the end of every task, and every fix is a command the summary names.
"""

from lrdmem import consolidate, usage
from lrdmem.lint import lint
from lrdmem.model import today_utc
from lrdmem.stale import stale
from lrdmem.store import Store


def build(store: Store, days: int = 30) -> dict:
    """Counts of lint problems, open judgment-queue items, stale paths, decisions due for review, and (once the
    recall log has sessions) memories with file or command triggers never injected in `days` days, else None."""
    today = today_utc()
    due = sorted(m.ref for m in store.memories() if m.kind == "decision" and m.revisit and m.revisit <= today
                 and m.state in ("candidate", "active", "contested"))
    report = usage.report(store, days)
    return {"lint": len(lint(store)), "queue": len(consolidate.open_items(store)), "stale": len(stale(store, None)),
            "decisions_due": due,
            "never_injected": len(report["unused_triggered"]) if report["sessions"] else None}


def lines(r: dict) -> list[str]:
    """One line per item that needs attention, each naming the command that shows it; empty when none does."""
    out = []
    if r["lint"]:
        out.append(f"{r['lint']} lint problem(s): run `lrdmem lint`")
    if r["queue"]:
        out.append(f"{r['queue']} memory decision(s) pending: run `lrdmem consolidate --queue`")
    if r["stale"]:
        out.append(f"{r['stale']} stale path(s) in memories: run `lrdmem stale`")
    if r["decisions_due"]:
        out.append(f"{len(r['decisions_due'])} decision(s) due for review: {', '.join(r['decisions_due'][:5])}")
    if r["never_injected"]:
        out.append(f"{r['never_injected']} memor(ies) with triggers never injected: run `lrdmem usage`")
    return out
