#!/usr/bin/env python
"""One-off reconciliation of the slug audit's dead ends. Report only.

`scripts/audit_slugs.py` says which boards answered. It cannot say *why* one
did not: a 404 is equally a seed slug that was never real, a rename, a move to
Workday, an acquisition, and a Lever casing artifact. This walks the audit CSV
and tries to tell those apart, in four tiers, stopping at the first board
confirmed live (200 with at least one posting) -- or, on a platform we have no
client for, at the name match itself.

It writes a single proposal CSV and prints a summary. It never touches
`data/universe/*.csv`, the health ledgers, or anything under `pipeline/` --
applying a proposal is a separate pass.

    uv run python scripts/reconcile_slugs.py --audit jobs/slug_audit_2026-09-21.csv
    uv run python scripts/reconcile_slugs.py --audit jobs/slug_audit_2026-09-21.csv \\
        --ats lever --status dead --no-web
    uv run python scripts/reconcile_slugs.py --audit ... --resume   # continue today's run
    uv run python scripts/reconcile_slugs.py --audit ... --workers 16  # widen a lane
"""
from __future__ import annotations

import argparse
import csv
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from src import paths
from src.ats_http import CareersError, fetch_json
from src.discovery.config import load_config, pacing_floor
from src.discovery.sources.ats import workday as wd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slug_resolvers as R  # noqa: E402

OUT_DIR = paths.REPO_ROOT / "jobs"
# The name-match source. `main_companies.csv` is the wide dump -- every ATS
# platform, a strict superset of `companies.csv` on the four lanes we scrape.
MAIN_COMPANIES_CSV = paths.REPO_ROOT / "main_companies.csv"
COMPANIES_CSV = paths.REPO_ROOT / "companies.csv"
DEFAULT_STATUSES = R.INPUT_STATUSES

# Website fetching is not an ATS API: a real browser UA and a short timeout,
# because a company site that hangs is the row budget's largest line item.
WEB_TIMEOUT = 6
WEB_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}
# A bot-blocked homepage often answers a second UA. `abtasty.com` 403s the
# header above and serves the one below.
ALT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
                  "Gecko/20100101 Firefox/128.0")
UA_RETRY_STATUSES = (403, 406, 429)

# The only resolver errnos that mean "this name does not exist". Everything
# else -- EAI_AGAIN above all -- is a lookup we did not get an answer from.
NXDOMAIN_ERRNOS = tuple(
    e for e in (getattr(socket, n, None) for n in ("EAI_NONAME", "EAI_NODATA"))
    if e is not None)

# The SOA query the address-less names fall through to. The system resolver
# first: it is a local cache, so the junk candidates cost almost nothing.
DNS_QUERY_TIMEOUT = 2.0
DNS_FALLBACK_SERVERS = ("1.1.1.1", "8.8.8.8")


def nameservers() -> tuple[str, ...]:
    """The resolvers to ask, system first."""
    found = []
    try:
        for line in Path("/etc/resolv.conf").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                found.append(parts[1])
    except OSError:
        pass
    return tuple(dict.fromkeys(found + list(DNS_FALLBACK_SERVERS)))


NAMESERVERS = nameservers()

# Per-row caps. One slow or sprawling company must not stall a lane. The DNS
# sweep is what makes the wide TLD list affordable: every candidate gets a
# lookup, only the ones that resolve get an HTTP request.
ROW_BUDGET_SECONDS = 90.0
MAX_BOARD_PROBES = 8
MAX_WEB_FETCHES = 18
MAX_DNS_LOOKUPS = R.MAX_DOMAIN_CANDIDATES
MAX_DOMAIN_SITES = 3

# Rows per lane run concurrently. The row cost is latency, not CPU: a DNS
# sweep plus up to `MAX_WEB_FETCHES` fetches against unrelated third-party
# hosts, none of which we rate-limit. ATS board requests are gated by the
# shared per-ATS `Pacer` instead, so this knob cannot raise the request rate
# against any board API.
DEFAULT_WORKERS = 8

# Paths this script must never write into, whatever --out says.
FORBIDDEN_OUT = ("data/universe", "pipeline", "profile")


# --- network ----------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    """One board API answer. `live` is the cascade's stop condition."""
    jobs: int = 0
    not_found: bool = False
    error: str = ""

    @property
    def live(self) -> bool:
        return not self.not_found and not self.error and self.jobs > 0


class Pacer:
    """Per-ATS request spacing, shared across lanes.

    Tier 2 and 3 probe boards on lanes other than the row's own, so spacing
    cannot live in the lane loop the way `audit_slugs.py`'s does.
    """

    def __init__(self, pacing: dict):
        self._pacing = pacing
        self._locks = {ats: threading.Lock() for ats in pacing}
        self._last = {ats: 0.0 for ats in pacing}

    def wait(self, ats: str) -> None:
        gap = self._pacing.get(ats)
        if not gap:
            return
        with self._locks[ats]:
            delay = gap - (time.monotonic() - self._last[ats])
            if delay > 0:
                time.sleep(delay)
            self._last[ats] = time.monotonic()


def probe_board(ats: str, slug: str, pacer: Pacer | None = None) -> Probe:
    """Ask one board how many postings it has. Never raises."""
    if pacer is not None:
        pacer.wait(ats)
    try:
        if ats == "workday":
            company, pod, site = wd.parse_slug(slug)
            payload = wd.list_page(company, pod, site, offset=0)
        else:
            payload = fetch_json(R.board_url(ats, slug))
    except CareersError as e:
        if e.status in (404, 410):
            return Probe(not_found=True)
        return Probe(error=f"{ats}/{slug}:{e.status or type(e).__name__}")
    except Exception as e:  # noqa: BLE001 -- a bad slug must not end the pass
        return Probe(error=f"{ats}/{slug}:{type(e).__name__}")
    try:
        return Probe(jobs=R.posting_count(ats, payload))
    except Exception as e:  # noqa: BLE001 -- malformed 200, not a dead board
        return Probe(error=f"{ats}/{slug}:malformed:{type(e).__name__}")


class WebClient:
    """DNS lookups and homepage/careers-page fetching, budgeted per row.

    The two are deliberately separate calls: `resolves` answers whether the
    company's name exists, `get` answers whether its web server will talk to
    us. Only the first may ever contribute to a retirement.
    """

    def __init__(self, limit: int = MAX_WEB_FETCHES):
        self._session = requests.Session()
        self._session.headers.update(WEB_HEADERS)
        self.limit = limit
        self.used = 0

    def resolves(self, domain: str) -> str:
        """One of R.DNS_OK / R.DNS_NXDOMAIN / R.DNS_UNKNOWN. Never raises.

        An address settles it. No address does not: `EAI_NONAME` covers both a
        name nothing claims and a registered name with no A record, so that
        case falls through to the zone check. `EAI_AGAIN` and every other
        failure is a lookup we got no answer from -- unknown, not absence.
        """
        for host in (domain, f"www.{domain}"):
            try:
                if socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
                    return R.DNS_OK
            except socket.gaierror as e:
                if e.errno not in NXDOMAIN_ERRNOS:
                    return R.DNS_UNKNOWN
            except Exception:  # noqa: BLE001 -- no answer is not an answer
                return R.DNS_UNKNOWN
        return self.zone_state(domain)

    def zone_state(self, domain: str) -> str:
        """Whether any zone claims `domain`, by SOA response code.

        Tries each resolver in turn; a resolver that will not answer leaves the
        name unknown, which cannot retire anything.
        """
        query_id = int.from_bytes(os.urandom(2), "big")
        packet = R.dns_query_packet(domain, query_id)
        state = R.DNS_UNKNOWN
        for server in NAMESERVERS:
            family = socket.AF_INET6 if ":" in server else socket.AF_INET
            try:
                with socket.socket(family, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(DNS_QUERY_TIMEOUT)
                    sock.sendto(packet, (server, 53))
                    state = R.dns_response_state(sock.recv(512), query_id)
            except Exception:  # noqa: BLE001 -- an unusable resolver is not an answer
                state = R.DNS_UNKNOWN
            if state != R.DNS_UNKNOWN:
                return state
        return state

    def get(self, url: str) -> tuple[str, str] | None:
        """(final_url, body) for a page that answered under 400, else None."""
        r = self._fetch(url, None)
        if r is not None and r.status_code in UA_RETRY_STATUSES:
            r = self._fetch(url, ALT_USER_AGENT)
        if r is None or r.status_code >= 400 or not (r.text or "").strip():
            return None
        return r.url, r.text

    def close(self) -> None:
        self._session.close()

    def _fetch(self, url: str, user_agent: str | None):
        if self.used >= self.limit:
            return None
        self.used += 1
        headers = {"User-Agent": user_agent} if user_agent else None
        try:
            return self._session.get(url, timeout=WEB_TIMEOUT,
                                     allow_redirects=True, headers=headers)
        except Exception:  # noqa: BLE001 -- an unreachable server is a normal answer here
            return None


# --- the cascade ------------------------------------------------------------

@dataclass
class Context:
    companies: dict
    pacer: Pacer | None = None
    no_web: bool = False
    web_factory: object = None


class RowBudget:
    def __init__(self, probes: int = MAX_BOARD_PROBES, seconds: float = ROW_BUDGET_SECONDS):
        self.probes = probes
        self._deadline = time.monotonic() + seconds

    def spend(self) -> bool:
        if self.probes <= 0 or self.expired():
            return False
        self.probes -= 1
        return True

    def expired(self) -> bool:
        return time.monotonic() > self._deadline


def _try_candidate(ats: str, slug: str, tier: str, confidence: str,
                   ctx: Context, out: R.Outcome, budget: RowBudget,
                   domain: str = "") -> bool:
    """Probe one candidate board; record it on `out` if it is live."""
    if not budget.spend():
        # Recorded as an error, not a negative finding: a row we ran out of
        # budget on was not disproved, and must be re-probed rather than retired.
        out.errors.append("budget_exhausted")
        return False
    p = probe_board(ats, slug, ctx.pacer)
    if p.error:
        out.errors.append(p.error)
        return False
    if p.live:
        out.found = R.Candidate(ats, slug, p.jobs, tier, confidence, domain)
        out.evidence.append(f"{tier}:{ats}/{slug}={p.jobs}")
        return True
    return False


def tier1_lever_case(row: dict, ctx: Context, out: R.Outcome, budget: RowBudget) -> bool:
    """Lever's API is case-sensitive; a 404 on a lowercased slug proves nothing."""
    if row["ats"] != "lever" or out.old_status not in R.NOT_FOUND_STATUSES:
        return False
    normalized = R.normalize_name(row["name"])
    known = [c.slug for c in ctx.companies.get(normalized, [])
             if c.ats == "lever" and c.slug != row["slug"]]
    for variant in R.lever_case_variants(row["slug"], known):
        if _try_candidate("lever", variant, "tier1_lever_case", R.HIGH, ctx, out, budget):
            return True
    return False


def _name_candidates(row: dict, ctx: Context,
                     out: R.Outcome) -> tuple[str, list[tuple, ]]:
    """(normalized name, [(company row, board slug)]) for every other board the
    dump carries under this company's name.

    The input board itself is excluded: the audited slug came out of the same
    universe, so matching it is not new evidence of anything. Sets
    `out.name_match` when anything survives that filter -- a company present on
    some other board exists, and that is the fact that forbids `gone`.
    """
    normalized = R.normalize_name(row["name"])
    case_sensitive = row["ats"] == "lever" and out.old_status in R.NOT_FOUND_STATUSES
    keep = []
    for c in ctx.companies.get(normalized, []):
        slug = R.workday_slug_from_url(c.url) if c.ats == "workday" else c.slug
        if not slug:
            continue
        if R.same_board(c.ats, slug, row["ats"], row["slug"], case_sensitive):
            continue
        keep.append((c, slug))
    if keep:
        out.name_match = True
        out.evidence.append("name_match")
    return normalized, keep


def tier2_name_match(row: dict, ctx: Context, out: R.Outcome, budget: RowBudget) -> bool:
    """The dump carries a second slug for the same company often enough to be
    the cheapest tier. Supported lanes only, because only those can be probed;
    a hit here is a live board we can actually poll, the best outcome there is.
    Short and generic names collide, so those are `low`."""
    normalized, candidates = _name_candidates(row, ctx, out)
    if not candidates:
        return False
    confidence = R.LOW if R.is_ambiguous_name(normalized) else R.HIGH
    for c, slug in candidates:
        if not R.is_supported_ats(c.ats):
            continue
        if _try_candidate(c.ats, slug, "tier2_name", confidence, ctx, out, budget):
            return True
    return False


def tier2b_offsite_name_match(row: dict, ctx: Context, out: R.Outcome,
                              budget: RowBudget) -> bool:
    """The same match on a platform we have no client for.

    Nothing is probed -- there is no API here to call -- so this is unverified,
    but a company in a live board dump is alive and hiring. That is enough to
    stop the cascade before the slow web tier: the website check could only
    confirm a company we already know exists.
    """
    normalized, candidates = _name_candidates(row, ctx, out)
    offsite = [(c, slug) for c, slug in candidates if not R.is_supported_ats(c.ats)]
    if not offsite:
        return False
    c, slug = offsite[0]
    confidence = R.LOW if R.is_ambiguous_name(normalized) else R.HIGH
    out.offsite = R.Offsite(c.ats, slug, confidence)
    out.evidence.append(f"tier2_offsite:{c.ats}/{slug}")
    return True


def _resolve_domains(candidates: list[str], web, out: R.Outcome,
                     budget: RowBudget) -> list[str]:
    """The candidate domains that exist in DNS, in candidate order.

    Nothing resolving is the only finding that may license `gone`, so a lookup
    that failed for a non-NXDOMAIN reason is recorded as an error instead --
    but only when no candidate resolved, since existence once established is
    not undone by a later resolver hiccup.
    """
    resolving, unknown = [], []
    for domain in candidates[:MAX_DNS_LOOKUPS]:
        if budget.expired():
            unknown.append(domain)
            break
        state = web.resolves(domain)
        if state == R.DNS_OK:
            resolving.append(domain)
            if len(resolving) >= MAX_DOMAIN_SITES:
                # Only the first `MAX_DOMAIN_SITES` are ever read, and
                # existence is already established, so the rest of the sweep
                # cannot change the verdict. Stopping here never makes `gone`
                # more reachable: that needs the list to stay empty.
                break
        elif state != R.DNS_NXDOMAIN:
            unknown.append(domain)
    if not resolving and unknown:
        out.errors.append(f"dns_unknown:{unknown[0]}")
    return resolving


def _read_site(domain: str, web) -> list[tuple[str, str]]:
    """Every page we could read for one domain: homepage, the guessed careers
    paths, then one hop from the homepage -- many careers pages are a click
    deeper. The guessed paths are tried even when the homepage itself 404s."""
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    urls = [f"https://{domain}{p}" for p in R.CAREERS_PATHS]
    home = web.get(f"https://{domain}")
    if home:
        pages.append(home)
        seen.add(home[0])
        urls += R.careers_links(home[1], home[0])
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        got = web.get(url)
        if got:
            pages.append(got)
    return pages


def tier3_domain(row: dict, ctx: Context, out: R.Outcome, budget: RowBudget) -> bool:
    """Resolve the company's domain, then read an ATS link off its careers page.

    Records `alive_no_board` evidence when the name resolves but carries no
    link we recognize -- a company whose name exists is never retired, whether
    or not its web server answered us.
    """
    if ctx.no_web:
        return False
    web = (ctx.web_factory or WebClient)()
    try:
        return _tier3(row, ctx, out, budget, web)
    finally:
        closer = getattr(web, "close", None)
        if closer is not None:
            closer()


def _tier3(row: dict, ctx: Context, out: R.Outcome, budget: RowBudget, web) -> bool:
    candidates = R.domain_candidates(row["name"], row["slug"])
    if not candidates:
        # A two-character stem guesses nothing, so there was no name to look
        # up. `web_checked` stays False: absence of a guess is not absence.
        out.evidence.append("no_candidates")
        return False
    out.web_checked = True
    if R.is_truncated_slug(row["slug"]):
        # At the board's width limit the slug's tail is gone, so the guesses
        # that matter are prefixes. Recorded so a reader of the CSV can tell a
        # prefix hit from a whole-stem one.
        out.evidence.append("truncated_slug")
    resolving = _resolve_domains(candidates, web, out, budget)
    if not resolving:
        out.evidence.append("no_domain")
        return False

    # DNS alone settles existence, so the domain is recorded before any fetch.
    out.domain = resolving[0]
    out.evidence.append(f"dns:{resolving[0]}")
    pages: list[tuple[str, str]] = []
    for domain in resolving[:MAX_DOMAIN_SITES]:
        pages = _read_site(domain, web)
        if pages:
            out.domain = domain
            out.site_read = True
            break
    if not pages:
        # Resolves, would not talk to us: 403, timeout, TLS, a dead server.
        out.evidence.append("dns_only")
        return False

    confidence = R.LOW if R.is_ambiguous_name(R.normalize_name(row["name"])) else R.HIGH
    for _, html in pages:
        links = R.extract_ats_links(html)
        out.evidence.extend(R.unsupported_markers(links))
        for link in links:
            if not link.supported:
                continue
            if R.same_board(link.ats, link.slug, row["ats"], row["slug"]):
                continue
            if _try_candidate(link.ats, link.slug, "tier3_site", confidence,
                              ctx, out, budget, domain=out.domain):
                return True
    out.evidence.append("site_up_no_board")
    return False


def resolve_row(row: dict, ctx: Context) -> list:
    """The full cascade for one input row. Never raises."""
    out = R.Outcome(old_status=row.get("status", ""))
    out.input_live = out.old_status in R.LIVE_STATUSES
    budget = RowBudget()
    try:
        for tier in (tier1_lever_case, tier2_name_match,
                     tier2b_offsite_name_match, tier3_domain):
            if tier(row, ctx, out, budget):
                break
    except Exception as e:  # noqa: BLE001 -- one bad row must not end the pass
        out.found = None
        out.errors.append(f"row:{type(e).__name__}")
    return R.result_row(row, out)


# --- I/O --------------------------------------------------------------------

class Writer:
    """Appends a row per slug and flushes each one, so a run killed at hour two
    keeps everything it learned and `--resume` picks up from there."""

    def __init__(self, path, existing: bool):
        self._fh = path.open("a" if existing else "w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._fh)
        self._lock = threading.Lock()
        if not existing:
            self._csv.writerow(R.COLUMNS)
            self._fh.flush()

    def write(self, row: list) -> None:
        with self._lock:
            self._csv.writerow(row)
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def load_done(path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as f:
        return {(r["ats"], r["slug"]) for r in csv.DictReader(f)}


def load_audit(path, lanes, statuses, done, limit) -> dict:
    """The audit CSV split into per-lane worklists."""
    work: dict[str, list[dict]] = {ats: [] for ats in lanes}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ats, slug = (row.get("ats") or "").strip(), (row.get("slug") or "").strip()
            if ats not in work or not slug:
                continue
            if (row.get("status") or "").strip() not in statuses:
                continue
            if (ats, slug) in done:
                continue
            work[ats].append({"ats": ats, "slug": slug,
                              "name": (row.get("name") or "").strip(),
                              "status": (row.get("status") or "").strip()})
    if limit:
        work = {ats: rows[:limit] for ats, rows in work.items()}
    return work


def reconcile_lane(rows, ctx: Context, writer: Writer, counts: dict,
                   workers: int = DEFAULT_WORKERS) -> None:
    """One lane's rows, `workers` at a time.

    Safe to widen because nothing here decides how fast a board API is asked:
    every probe goes through the shared per-ATS `Pacer`, which holds that
    lane's lock for the whole gap. More threads only overlap the per-row DNS
    sweep and website fetches, which hit a different third-party host per row.

    One pool per lane rather than one over all rows, so `--workers` stays a
    per-lane number and each lane's `counts` dict is touched by its own pool
    only -- under its own lock, since `counts.get` + assign is not atomic.
    """
    lock = threading.Lock()

    def one(row) -> None:
        result = resolve_row(row, ctx)
        verdict = result[R.COLUMNS.index("verdict")]
        with lock:
            counts[verdict] = counts.get(verdict, 0) + 1
        writer.write(result)

    if workers <= 1:
        for row in rows:
            one(row)
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="row") as pool:
        for fut in [pool.submit(one, row) for row in rows]:
            fut.result()


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit", required=True, help="audit CSV from scripts/audit_slugs.py")
    p.add_argument("--ats", action="append", choices=sorted(R.SUPPORTED_ATS),
                   help="repeatable; default every lane present in the audit CSV")
    p.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                   help=f"comma-separated audit statuses (default {','.join(DEFAULT_STATUSES)})")
    p.add_argument("--limit", type=int, help="first N rows per lane, for a smoke run")
    p.add_argument("--pacing", type=float,
                   help="seconds between board fetches per lane; default is discovery.yaml's")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"rows in flight per lane (default {DEFAULT_WORKERS}). "
                        "Board pacing is unaffected: the shared per-ATS pacer "
                        "gates every probe whatever this is set to")
    p.add_argument("--resume", action="store_true",
                   help="skip slugs already in today's output file")
    p.add_argument("--no-web", action="store_true",
                   help="skip tier 3 (the slow one): tier 1 and 2 only. No row "
                        "can read `gone` in this mode -- absence was not established")
    p.add_argument("--companies",
                   help="name-match source CSV (ats,name,slug,url); default "
                        "main_companies.csv, falling back to companies.csv")
    p.add_argument("--out", help="output CSV (default jobs/slug_reconcile_<date>.csv)")
    return p.parse_args(argv)


def companies_path(override: str | None = None) -> Path:
    """The name-match source: the override, else the wide dump, else the narrow
    one. `main_companies.csv` is a superset, so it is the default when present."""
    if override:
        path = Path(override)
        return path if path.is_absolute() else paths.REPO_ROOT / path
    return MAIN_COMPANIES_CSV if MAIN_COMPANIES_CSV.exists() else COMPANIES_CSV


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    audit = Path(args.audit)
    if not audit.is_absolute():
        audit = paths.REPO_ROOT / audit
    if not audit.exists():
        print(f"audit CSV not found: {audit}")
        return 1

    statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
    unknown = [s for s in statuses if s not in R.INPUT_STATUSES]
    if unknown:
        print(f"unknown --status values: {', '.join(unknown)}")
        return 1

    today = date.today()
    out = (paths.REPO_ROOT / args.out if args.out
           else OUT_DIR / f"slug_reconcile_{today}.csv")
    rel = out.resolve().as_posix()
    if any(f"/{bad}/" in rel for bad in FORBIDDEN_OUT):
        print(f"refusing to write into {rel}: this pass is report-only")
        return 1

    workers = max(1, args.workers)
    lanes = tuple(dict.fromkeys(args.ats)) if args.ats else R.SUPPORTED_ATS
    done = load_done(out) if args.resume else set()
    if args.resume and not done:
        print(f"--resume: nothing to resume from in {out}, starting fresh")

    work = load_audit(audit, lanes, statuses, done, args.limit)
    lanes = tuple(ats for ats in lanes if work.get(ats))
    if not lanes:
        print("nothing to reconcile")
        return 0

    cfg = load_config()
    pacing = {ats: (args.pacing if args.pacing is not None
                    else max(pacing_floor(ats), cfg.sources[ats].pacing_seconds))
              for ats in R.SUPPORTED_ATS}
    companies = companies_path(args.companies)
    if not companies.exists():
        print(f"companies CSV not found: {companies}")
        return 1
    ctx = Context(companies=R.load_companies(companies),
                  pacer=Pacer(pacing), no_web=args.no_web)

    for ats in lanes:
        print(f"{ats:11} {len(work[ats]):6} rows at {pacing[ats]:g}s")
    print(f"workers: {workers} per lane ({workers * len(lanes)} rows in flight)")
    print(f"names: {companies.name} ({len(ctx.companies)} companies)")
    print(f"statuses: {','.join(statuses)}"
          f"{'  (tier 1+2 only, no gone verdicts)' if args.no_web else ''}")
    if done:
        print(f"resuming: {len(done)} already done")
    print(f"-> {out}\n")

    writer = Writer(out, existing=bool(done) and out.exists())
    counts = {ats: {} for ats in lanes}
    try:
        with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            futures = {ats: pool.submit(reconcile_lane, work[ats], ctx,
                                        writer, counts[ats], workers)
                       for ats in lanes}
            for fut in futures.values():
                fut.result()
    finally:
        writer.close()

    print_summary(lanes, counts, out)
    return 0


def print_summary(lanes, counts, out) -> None:
    header = f"{'ats':11}" + "".join(f"{v:>16}" for v in R.VERDICTS)
    print(header)
    retire = 0
    for ats in lanes:
        c = counts[ats]
        print(f"{ats:11}" + "".join(f"{c.get(v, 0):>16}" for v in R.VERDICTS))
        retire += sum(n for v, n in c.items() if R.retires_old_row(v))
    print(f"\n{retire} rows would propose active=0 "
          f"(moved, moved_offsite or gone only).")
    print(f"Wrote {out}. Nothing else was modified.")


if __name__ == "__main__":
    raise SystemExit(main())
