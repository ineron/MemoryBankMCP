#!/usr/bin/env python3
"""Weekly hours + real cost per project, derived from local Claude Code
session transcripts (~/.claude/projects/*/*.jsonl).

Hours: sum of inter-message gaps within a session, each gap capped at
GAP_CAP_SECONDS, so long idle periods between messages aren't counted as
active work time.

Cost: pulled from `ccusage session --json` (https://github.com/ryoppippi/ccusage),
which reads the same transcripts and computes real token cost per session.
A session's cost is prorated across weeks by the fraction of its active
seconds that fell in each week.

The current (incomplete) ISO week is excluded from the report.
"""
import json
import os
import glob
import subprocess
from collections import defaultdict, Counter
from datetime import datetime, timezone

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
GAP_CAP_SECONDS = 10 * 60


def iso_week_key(dt):
    y, w, _ = dt.isocalendar()
    return (y, w)


def week_label(y, w):
    monday = datetime.strptime(f"{y}-W{w}-1", "%G-W%V-%u")
    return f"{y}-W{w:02d} (week of {monday.strftime('%Y-%m-%d')})"


def project_name_from_cwd(cwd):
    if not cwd:
        return "unknown"
    return os.path.basename(cwd.rstrip("/"))


def main():
    # ---- Pass 1: parse transcripts -> per-session timestamps + cwd ----
    session_timestamps = defaultdict(list)    # sid -> [datetime]
    session_cwd_votes = defaultdict(Counter)  # sid -> Counter(cwd)

    files = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    for fp in files:
        try:
            with open(fp, "r", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") not in ("user", "assistant"):
                        continue
                    ts = d.get("timestamp")
                    sid = d.get("sessionId")
                    if not ts or not sid:
                        continue
                    try:
                        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    session_timestamps[sid].append(dt)
                    cwd = d.get("cwd")
                    if cwd:
                        session_cwd_votes[sid][cwd] += 1
        except OSError:
            continue

    session_project = {
        sid: project_name_from_cwd(votes.most_common(1)[0][0])
        for sid, votes in session_cwd_votes.items()
    }

    # ---- Pass 2: per-session, per-week active seconds ----
    session_week_seconds = defaultdict(lambda: defaultdict(float))  # sid -> wk -> seconds
    session_total_seconds = defaultdict(float)

    for sid, timestamps in session_timestamps.items():
        timestamps.sort()
        for prev, cur in zip(timestamps, timestamps[1:]):
            gap = (cur - prev).total_seconds()
            if gap <= 0:
                continue
            active = min(gap, GAP_CAP_SECONDS)
            wk = iso_week_key(cur)
            session_week_seconds[sid][wk] += active
            session_total_seconds[sid] += active

    # ---- Pass 3: pull real cost per session from ccusage ----
    result = subprocess.run(
        ["npx", "ccusage@latest", "session", "--json"],
        capture_output=True, text=True, timeout=120,
    )
    ccusage_data = json.loads(result.stdout)
    session_cost = {row["period"]: row["totalCost"] for row in ccusage_data["session"]}

    # ---- Combine: prorate each session's cost across weeks by active-time fraction ----
    week_project_seconds = defaultdict(lambda: defaultdict(float))
    week_project_cost = defaultdict(lambda: defaultdict(float))

    for sid, wk_secs in session_week_seconds.items():
        proj = session_project.get(sid, "unknown")
        total_secs = session_total_seconds.get(sid, 0)
        cost = session_cost.get(sid, 0.0)
        for wk, secs in wk_secs.items():
            week_project_seconds[wk][proj] += secs
            frac = (secs / total_secs) if total_secs > 0 else 0
            week_project_cost[wk][proj] += cost * frac

    # sessions with no inter-message gaps (single message) contribute 0 hours;
    # still attribute their full cost to their single week via last activity
    for sid, cost in session_cost.items():
        if sid not in session_week_seconds or not session_week_seconds[sid]:
            ts_list = session_timestamps.get(sid)
            if not ts_list:
                continue
            wk = iso_week_key(max(ts_list))
            proj = session_project.get(sid, "unknown")
            week_project_cost[wk][proj] += cost

    now = datetime.now(timezone.utc)
    current_wk = iso_week_key(now)
    all_weeks = sorted(set(week_project_seconds.keys()) | set(week_project_cost.keys()))
    weeks = [w for w in all_weeks if w != current_wk]

    print("=" * 90)
    print("Hours + real cost per project per week (past weeks only)")
    print("Hours: gaps between messages within a session, each capped at "
          f"{GAP_CAP_SECONDS // 60} min. $: ccusage per-session cost, prorated")
    print("across weeks by that session's hour-fraction in each week.")
    print("=" * 90)

    grand_hours = 0.0
    grand_cost = 0.0
    for wk in weeks:
        y, w = wk
        proj_secs = week_project_seconds.get(wk, {})
        proj_cost = week_project_cost.get(wk, {})
        week_hours = sum(proj_secs.values()) / 3600
        week_cost = sum(proj_cost.values())
        grand_hours += week_hours
        grand_cost += week_cost
        print(f"\n{week_label(y, w)} -- {week_hours:.1f}h, ${week_cost:.2f}")
        rows = []
        for proj in set(proj_secs.keys()) | set(proj_cost.keys()):
            h = proj_secs.get(proj, 0) / 3600
            c = proj_cost.get(proj, 0)
            rows.append((proj, h, c))
        rows.sort(key=lambda x: -x[1])
        for proj, h, c in rows:
            print(f"    {proj:35s} {h:6.2f}h   ${c:7.2f}")

    print("\n" + "=" * 90)
    print(f"TOTAL (past weeks): {grand_hours:.1f}h, ${grand_cost:.2f}")
    if weeks:
        print(f"Weekly average: {grand_hours/len(weeks):.1f}h, ${grand_cost/len(weeks):.2f}")

    print("\nTotals per project (all past weeks):")
    totals_h = defaultdict(float)
    totals_c = defaultdict(float)
    for wk in weeks:
        for proj, secs in week_project_seconds.get(wk, {}).items():
            totals_h[proj] += secs / 3600
        for proj, c in week_project_cost.get(wk, {}).items():
            totals_c[proj] += c
    all_projs = set(totals_h.keys()) | set(totals_c.keys())
    for proj in sorted(all_projs, key=lambda p: -totals_h.get(p, 0)):
        print(f"    {proj:35s} {totals_h.get(proj, 0):6.2f}h   ${totals_c.get(proj, 0):7.2f}")


if __name__ == "__main__":
    main()
