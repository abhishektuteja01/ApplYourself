"""Pure resolver logic for the slug reconciliation pass. No network, no threads.

`scripts/reconcile_slugs.py` is the driver; everything here is a function of its
arguments so the safety-critical part -- which verdicts may retire a slug -- is
unit-testable offline.

A 404 from a board API means only "this API has no board under this exact
string". It conflates a seed slug that was never real, a rename, a move to
another platform, and a Lever casing artifact. So a 404 alone never retires
anything: `RETIREABLE` is the closed set, and `gone` needs a 404 plus every
tier finding nothing plus no candidate domain existing in DNS.

A company found under its own name on a platform we do not scrape gets its own
verdict, `moved_offsite`: it is alive and hiring, and its board is one we can
never read, so the old slug is a dead end for us and the destination is a
record rather than an addition.

Existence is a DNS question, not an HTTP one. A domain that resolves means the
company's name is registered and pointed somewhere, whatever the web server
then does with our request -- 403, 404, 500, TLS failure, timeout and reset are
all answers from something that exists. Only NXDOMAIN on every candidate may
contribute to `gone`, and a lookup that fails for any other reason is unknown,
not absence, so it poisons `gone` exactly as a board probe error does.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin, urlparse

# --- verdicts ---------------------------------------------------------------

OK = "ok"
MOVED = "moved"
MOVED_OFFSITE = "moved_offsite"
DUAL = "dual"
ALIVE_NO_BOARD = "alive_no_board"
GONE = "gone"
REVIEW = "review"
ERROR = "error"

VERDICTS = (OK, MOVED, MOVED_OFFSITE, DUAL, ALIVE_NO_BOARD, GONE, REVIEW, ERROR)

# The only two verdicts that may propose `active=0` *and* an addition. For
# `moved` the retirement is the OLD row only; the new slug is proposed as an
# addition with active=1.
RETIREABLE = frozenset({MOVED, GONE})

# `moved_offsite` retires the old row and proposes nothing: the destination is
# a platform we have no client for, so adding it to the universe would create a
# slug nothing can ever poll. Kept in its own set so a consumer of the CSV can
# tell "retire and add" from "retire and add nothing" from the verdict alone.
RETIREABLE_OFFSITE = frozenset({MOVED_OFFSITE})

# Every verdict that proposes active=0 on the input row, by either route.
RETIRES_OLD_ROW = RETIREABLE | RETIREABLE_OFFSITE

HIGH, LOW = "high", "low"

COLUMNS = ["ats", "slug", "name", "old_status", "verdict", "new_ats", "new_slug",
           "new_jobs", "acquirer", "confidence", "domain", "evidence"]

# What the audit's statuses say about the input board itself.
LIVE_STATUSES = frozenset({"active", "stale"})   # answered 200 with postings
NOT_FOUND_STATUSES = frozenset({"dead"})         # 404/410
INPUT_STATUSES = ("active", "stale", "empty", "dead")

SUPPORTED_ATS = ("greenhouse", "lever", "ashby", "workday")


def is_retireable(verdict: str) -> bool:
    """Retires the old row and proposes a replacement board."""
    return verdict in RETIREABLE


def retires_old_row(verdict: str) -> bool:
    """Proposes active=0 on the input row, with or without a replacement."""
    return verdict in RETIRES_OLD_ROW


def is_supported_ats(ats: str) -> bool:
    """Whether we have a scraper for this platform. An unsupported match is
    evidence the company is alive; it is never a board we can poll."""
    return ats in SUPPORTED_ATS


# --- name normalization -----------------------------------------------------

_LEGAL_SUFFIXES = ("inc", "llc", "ltd", "limited", "corp", "corporation", "co",
                   "gmbh", "plc", "pbc", "lp", "llp", "pte", "pty")
_SUFFIX_RE = re.compile(
    r",?\s*\b(" + "|".join(_LEGAL_SUFFIXES) + r")\b\.?", re.IGNORECASE)
_APOSTROPHE_RE = re.compile(r"[’']")

# A short or generic normalized name collides across unrelated companies, so a
# name match on one is never enough to retire the old slug. Closed list; a word
# earns its place here by having actually collided in the audit CSV.
AMBIGUOUS_MAX_LEN = 6
GENERIC_WORDS = frozenset({
    "atlas", "beacon", "bloom", "bridge", "canopy", "cascade", "circle",
    "copper", "crisp", "ember", "figure", "forge", "fusion", "harbor",
    "helix", "keystone", "lattice", "ledger", "loop", "matrix", "mosaic",
    "nexus", "north", "orbit", "parallel", "pinnacle", "prism", "pulse",
    "quantum", "rally", "signal", "slate", "spark", "sphere", "summit",
    "tandem", "tempo", "thread", "tide", "vector", "verse", "vertex",
    "vista", "zenith",
})


def normalize_name(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation, collapse whitespace."""
    n = _APOSTROPHE_RE.sub("", (name or "").lower().strip())
    n = _SUFFIX_RE.sub("", n)
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def name_stem(name: str) -> str:
    """The normalized name with separators removed -- the domain/slug form."""
    return normalize_name(name).replace(" ", "")


def is_ambiguous_name(normalized: str) -> bool:
    """True when a name match alone cannot identify one company."""
    if not normalized:
        return True
    if len(normalized) <= AMBIGUOUS_MAX_LEN:
        return True
    return " " not in normalized and normalized in GENERIC_WORDS


# --- the companies index ----------------------------------------------------

@dataclass(frozen=True)
class CompanyRow:
    ats: str
    name: str
    slug: str
    url: str

    @property
    def supported(self) -> bool:
        return is_supported_ats(self.ats)


def load_companies(path) -> dict[str, list[CompanyRow]]:
    """A companies dump (`ats,name,slug,url`) indexed by normalized name.

    Every platform is kept, not just the four we scrape: a company sitting on
    bamboohr is proof it exists, which is the one fact that must block `gone`.
    Each row carries its own `ats`, so the caller decides what to probe.
    """
    index: dict[str, list[CompanyRow]] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ats = (row.get("ats") or "").strip().lower()
            name = (row.get("name") or "").strip()
            slug = (row.get("slug") or "").strip()
            if not ats or not slug:
                continue
            index.setdefault(normalize_name(name), []).append(
                CompanyRow(ats, name, slug, (row.get("url") or "").strip()))
    return index


# --- workday url -> tri-part slug -------------------------------------------

_WD_URL_RE = re.compile(
    r"(?:https?://)?([A-Za-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")


def workday_slug_from_url(url: str) -> str | None:
    """`https://{tenant}.{pod}.myworkdayjobs.com/[locale/]{Site}` -> tenant|pod|Site."""
    m = _WD_URL_RE.search(url or "")
    return f"{m.group(1)}|{m.group(2)}|{m.group(3)}" if m else None


def workday_tenant(slug: str) -> str:
    return slug.split("|")[0] if slug else ""


# --- tier 1: lever casing ---------------------------------------------------

def lever_case_variants(slug: str, known: list[str] | None = None) -> list[str]:
    """Casings of a lever slug worth retrying. Lever's API is case-sensitive:
    `.../postings/fliff` 404s while `Fliff` serves 20 jobs.

    `known` is the casing companies.csv carries for the same company, tried
    first. The generated variants follow, deduped, original excluded.
    """
    if not slug:
        return []
    parts = re.split(r"([-_.])", slug)
    titled = "".join(p if p in "-_." else p.capitalize() for p in parts)
    out = list(known or []) + [slug.capitalize(), titled, slug.upper(), slug.lower()]
    seen, variants = {slug}, []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            variants.append(v)
    return variants


def same_board(ats_a: str, slug_a: str, ats_b: str, slug_b: str,
               case_sensitive: bool = False) -> bool:
    """Whether two (ats, slug) pairs name the same board.

    `case_sensitive` is for a lever row that 404s: there a casing difference is
    the finding, not noise. Everywhere else a case-only difference is the same
    board and must not be reported as a move.
    """
    if ats_a != ats_b:
        return False
    return slug_a == slug_b if case_sensitive else slug_a.lower() == slug_b.lower()


# --- tier 3: domain guessing ------------------------------------------------

TLDS = ("com", "io", "ai", "co", "org", "net", "dev", "app", "so", "xyz",
        "tech", "co.uk", "de", "in", "nl", "fr", "se", "com.br", "health",
        "finance")
MAX_DOMAIN_CANDIDATES = 220
CAREERS_PATHS = ("/careers", "/jobs", "/careers/", "/company/careers",
                 "/about/careers", "/join-us", "/careers/jobs")

# DNS outcomes. `unknown` is the important one: a resolver timeout is not an
# answer, so it must never read as absence.
DNS_OK, DNS_NXDOMAIN, DNS_UNKNOWN = "ok", "nxdomain", "unknown"

# Longest first, so `acmecom` splits on `com` rather than on `co`.
_TLD_SPLITS = tuple(sorted(TLDS, key=len, reverse=True))
TLD_SPLIT_MIN = 2

# Legal suffixes glued to the preceding word: the audit's `2seventybioinc` is
# `2seventybio`. `_SUFFIX_RE` only sees a suffix that is its own word.
_GLUED_SUFFIXES = ("holdings", "group", "gmbh", "corp", "inc", "llc", "ltd",
                   "plc", "pbc", "co", "sa", "bv", "ab")
GLUED_STEM_MIN = 4

# Suffixes a board slug carries that a domain never does: `10up-2` is `10up`.
_BOARD_DUP_SUFFIXES = ("-careers", "-jobs", "-inc", "-hq", "-2", "-3",
                       "careers", "jobs")
BOARD_STEM_MIN = 2


# A board slug is very often the company name with the segment that board
# serves glued on, and the ATS truncates the result at a fixed width. Both
# mangle the stem a domain guess is built from, and both are recoverable: strip
# the segment word, or sweep prefixes of what is left.
#
# A short prefix can hit an unrelated parked domain, which costs one spurious
# `alive_no_board`. That is the safe direction: the row is not retired either
# way, and never retiring a live company matters more than a tidy bucket.

SLUG_TRUNCATION_LEN = 30

# Stripped off the end before the prefix sweep, longest first so
# `servicingcorp` wins over `servicing`. Only when something plausible
# remains, so `studios` does not reduce to nothing.
_SEGMENT_SUFFIXES = tuple(sorted((
    "professionals", "guestservices", "servicingcorp", "servicing", "studio",
    "studios", "team", "teams", "careers", "jobs", "hiring", "talent",
    "recruiting", "contractors", "interns", "internships", "students",
    "alumni", "campus", "corporate", "external", "employees", "staff",
    "temp", "seasonal", "schoolyear", "portfolio", "group", "holdings",
    "labs", "ventures", "partners",
), key=len, reverse=True))
SEGMENT_STEM_MIN = 4

# Leading words that are not the brand: `centerforhumancompatibleartifi` is
# `humancompatible`, not `centerfor...`.
_LEADING_WORDS = ("thecenterfor", "theinstituteof", "theuniversityof",
                  "instituteof", "universityof", "centerfor", "institute",
                  "university", "center", "the")
LEADING_STEM_MIN = 6

# The prefix sweep. DNS is cheap -- a 48-candidate sweep measured ~0.9s -- so a
# much wider list is affordable, but it is still bounded: no prefix under
# `PREFIX_MIN_LEN`, a per-stem cap, and a total cap on prefix bases. Prefixes
# get the head of the TLD list only; a prefix guess on `.com` is worth more
# than the same prefix on `.xyz`.
PREFIX_MIN_LEN = 5
MAX_PREFIXES_PER_STEM = 10
MAX_PREFIXES_TRUNCATED = 24
MAX_PREFIX_BASES = 32
PREFIX_TLDS = ("com", "io", "ai")

_DOT_RE = re.compile(r"^(.{2,}?)dot([a-z]{2,})$")


def is_truncated_slug(slug: str) -> bool:
    """A slug at the board's width limit has lost its tail, so its full form is
    unavailable and the prefix sweep is the only route back to the stem."""
    return len((slug or "").split("|")[0]) >= SLUG_TRUNCATION_LEN


def decode_dot_domain(stem: str) -> str:
    """`halodotscience` -> `halo.science`: the literal `dot` a board slug uses
    where the domain has one. The TLD is already in the string, so this is a
    domain, not a stem. "" when there is no usable `dot` inside."""
    m = _DOT_RE.match(stem or "")
    return f"{m.group(1)}.{m.group(2)}" if m else ""


def dot_stems(stem: str) -> list[str]:
    """The two stems a `dot` slug also implies: the brand alone and the halves
    joined -- `halodotscience` -> [`halo`, `haloscience`]."""
    m = _DOT_RE.match(stem or "")
    return [m.group(1), m.group(1) + m.group(2)] if m else []


def strip_segment_suffixes(stem: str) -> list[str]:
    """Each successively shorter stem a board-segment strip yields.

    `pjtpartnersprofessionals` -> [`pjtpartners`]: the second pass would leave
    `pjt`, under `SEGMENT_STEM_MIN`, so it stops.
    """
    out, current = [], stem or ""
    while True:
        for suffix in _SEGMENT_SUFFIXES:
            if (current.endswith(suffix)
                    and len(current) - len(suffix) >= SEGMENT_STEM_MIN):
                current = current[:-len(suffix)].rstrip("-")
                out.append(current)
                break
        else:
            return out


def strip_leading_word(stem: str) -> str:
    """`centerforhumancompatibleartifi` -> `humancompatibleartifi`. "" when
    there is no leading filler, or stripping it leaves too little."""
    for word in _LEADING_WORDS:
        if stem.startswith(word) and len(stem) - len(word) >= LEADING_STEM_MIN:
            return stem[len(word):]
    return ""


def word_prefixes(name: str) -> list[str]:
    """Cumulative word prefixes of a name -- the one truncation point we can
    actually infer. `RoundPoint Mortgage Servicing Corp` ->
    [`roundpoint`, `roundpointmortgage`, `roundpointmortgageservicing`].

    The whole stem is excluded: `domain_stems` already carries it.
    """
    words = normalize_name(name).split()
    out, acc = [], ""
    for word in words[:-1]:
        acc += word
        if len(acc) >= PREFIX_MIN_LEN:
            out.append(acc)
    return out


def length_prefixes(stem: str, truncated: bool = False) -> list[str]:
    """Prefixes of `stem`, longest first: the longest is the likeliest cut
    point. A slug at the width limit gets the deeper sweep, because its real
    stem may be many characters shorter than what survived."""
    cap = MAX_PREFIXES_TRUNCATED if truncated else MAX_PREFIXES_PER_STEM
    out = []
    for n in range(len(stem) - 1, PREFIX_MIN_LEN - 1, -1):
        if len(out) >= cap:
            break
        out.append(stem[:n])
    return out


def split_tld_stem(stem: str) -> str:
    """`21co` -> `21.co`, `wisecarbonio` -> `wisecarbon.io`. "" when the stem
    does not end in a known TLD."""
    for tld in _TLD_SPLITS:
        flat = tld.replace(".", "")
        if stem.endswith(flat) and len(stem) - len(flat) >= TLD_SPLIT_MIN:
            return f"{stem[:-len(flat)]}.{tld}"
    return ""


def strip_glued_suffix(stem: str) -> str:
    """`2seventybioinc` -> `2seventybio`. "" when there is nothing to strip."""
    for suffix in _GLUED_SUFFIXES:
        if stem.endswith(suffix) and len(stem) - len(suffix) >= GLUED_STEM_MIN:
            return stem[:-len(suffix)]
    return ""


def strip_board_suffix(stem: str) -> str:
    """`10up-2` -> `10up`. "" when there is nothing to strip."""
    for suffix in _BOARD_DUP_SUFFIXES:
        if stem.endswith(suffix) and len(stem) - len(suffix) >= BOARD_STEM_MIN:
            return stem[:-len(suffix)].rstrip("-")
    return ""


def domain_stems(name: str, slug: str) -> list[str]:
    """The bases a domain guess is built from, most likely first."""
    stem = name_stem(name)
    hyphen = re.sub(r"[^a-z0-9]+", "-", normalize_name(name)).strip("-")
    slug_stem = re.sub(r"[^a-z0-9-]", "", (slug or "").split("|")[0].lower())
    # A stem under three characters guesses nothing useful, and prefixing it
    # ("get0g") only makes a junk domain long enough to pass the length check.
    prefixed = "get" + stem if len(stem) >= 3 else ""
    bases: list[str] = []
    for base in (stem, slug_stem, hyphen, prefixed):
        for b in (base, strip_board_suffix(base), strip_glued_suffix(base),
                  strip_leading_word(base), *strip_segment_suffixes(base),
                  *dot_stems(base)):
            if b and len(b) >= 3 and b not in bases:
                bases.append(b)
    return bases


def prefix_bases(name: str, slug: str) -> list[str]:
    """The bounded prefix sweep: inferred word boundaries first, then a
    length sweep over the stem and over each board-segment strip of it.

    This is what recovers a truncated slug -- `roundpointmortgageservicingcor`
    is 30 characters, so its real stem is only reachable as a prefix.
    """
    truncated = is_truncated_slug(slug)
    stem = name_stem(name)
    slug_stem = re.sub(r"[^a-z0-9]", "", (slug or "").split("|")[0].lower())
    roots: list[str] = []
    for base in (stem, slug_stem):
        for b in (base, *strip_segment_suffixes(base), strip_leading_word(base)):
            if b and b not in roots:
                roots.append(b)
    out = word_prefixes(name)
    for root in roots:
        for p in length_prefixes(root, truncated):
            if p not in out:
                out.append(p)
    return [p for p in out if len(p) >= PREFIX_MIN_LEN][:MAX_PREFIX_BASES]


def literal_domains(name: str, slug: str) -> list[str]:
    """Domains a slug spells out in full, TLD included. Today only the `dot`
    form; these bypass the TLD cross-product entirely."""
    slug_stem = re.sub(r"[^a-z0-9]", "", (slug or "").split("|")[0].lower())
    out = [d for d in (decode_dot_domain(b) for b in (name_stem(name), slug_stem)) if d]
    return list(dict.fromkeys(out))


def domain_candidates(name: str, slug: str) -> list[str]:
    """Guessed domains for a company, most likely first.

    TLD-major after the literals and the stem splits: with a per-row cap, every
    base deserves its `.com` before any base gets its `.xyz`. Prefix bases ride
    along inside the same TLD-major walk, on `PREFIX_TLDS` only, so a prefix's
    `.com` still lands ahead of a full stem's long-tail TLD.
    """
    bases = domain_stems(name, slug)
    prefixes = [p for p in prefix_bases(name, slug) if p not in bases]
    out = literal_domains(name, slug)
    out += [d for d in (split_tld_stem(b) for b in bases) if d]
    for tld in TLDS:
        out.extend(f"{base}.{tld}" for base in bases)
        if tld in PREFIX_TLDS:
            out.extend(f"{p}.{tld}" for p in prefixes)
    return list(dict.fromkeys(out))[:MAX_DOMAIN_CANDIDATES]


# --- raw DNS: does the name exist at all ------------------------------------
# `getaddrinfo` cannot answer that question on its own. macOS returns
# `EAI_NONAME` both for a name no zone claims and for a name that exists with
# no address record -- `21.co` is registered, delegated, and has no A. Retiring
# on that is exactly the false positive this pass has to avoid, so an
# address-less name is settled by asking for the zone's SOA and reading only
# the response code. No record parsing: RCODE 0 means something answered for
# this name, RCODE 3 means nothing claims it.

DNS_SOA_QTYPE = 6
DNS_IN_QCLASS = 1
DNS_RCODE_NOERROR = 0
DNS_RCODE_NXDOMAIN = 3


def dns_query_packet(domain: str, query_id: int) -> bytes:
    """A recursion-desired SOA query for one name."""
    header = bytes([query_id >> 8 & 0xFF, query_id & 0xFF, 0x01, 0x00,
                    0, 1, 0, 0, 0, 0, 0, 0])
    qname = b"".join(bytes([len(p)]) + p
                     for p in (l.encode("idna") for l in domain.split(".")) if p)
    return header + qname + b"\x00" + bytes([0, DNS_SOA_QTYPE, 0, DNS_IN_QCLASS])


def dns_response_state(packet: bytes, query_id: int) -> str:
    """DNS_OK / DNS_NXDOMAIN / DNS_UNKNOWN from a response's header alone.

    A truncated, mis-IDed or short packet is unknown, never absence.
    """
    if len(packet) < 12:
        return DNS_UNKNOWN
    if (packet[0] << 8 | packet[1]) != query_id:
        return DNS_UNKNOWN
    rcode = packet[3] & 0x0F
    if rcode == DNS_RCODE_NOERROR:
        return DNS_OK
    if rcode == DNS_RCODE_NXDOMAIN:
        return DNS_NXDOMAIN
    return DNS_UNKNOWN


# --- ATS link extraction ----------------------------------------------------

@dataclass(frozen=True)
class AtsLink:
    ats: str
    slug: str
    evidence: str
    supported: bool


# Embed first: `boards.greenhouse.io/embed/job_board?for=<slug>` must capture
# the `for=` token, not the word "embed".
_GH_EMBED = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/embed/job_board[^\"'\s]*?"
    r"[?&](?:amp;)?for=([A-Za-z0-9_-]+)")
_SUPPORTED_PATTERNS = (
    ("greenhouse", _GH_EMBED),
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?!embed\b)([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)")),
    # Both URL forms, and `%20` is in the character class deliberately: an
    # Ashby slug may contain literal spaces. Flock Safety's board is
    # `Flock Safety`, which no name-derived candidate would ever generate, so
    # scraping the page for the slug is the only way to reach it.
    ("ashby", re.compile(
        r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)"
        r"/([A-Za-z0-9_.%+-]+)")),
    ("workday", _WD_URL_RE),
)

# Platforms we do not scrape. Recorded, never probed -- how much of the
# universe left our four lanes is the point of collecting them.
_UNSUPPORTED_PATTERNS = (
    ("workable", re.compile(r"(?:apply|[A-Za-z0-9-]+)\.workable\.com/([A-Za-z0-9_-]*)")),
    ("smartrecruiters", re.compile(
        r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]*)")),
    ("rippling", re.compile(r"ats\.rippling\.com/([A-Za-z0-9_-]*)")),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([A-Za-z0-9_-]*)")),
    ("bamboohr", re.compile(r"([A-Za-z0-9-]+)\.bamboohr\.com")),
    ("icims", re.compile(r"([A-Za-z0-9-]+)\.icims\.com")),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/recruiting/jobs/([A-Za-z0-9_/-]*)")),
    ("teamtailor", re.compile(r"([A-Za-z0-9-]+)\.teamtailor\.com")),
    ("recruitee", re.compile(r"([A-Za-z0-9-]+)\.recruitee\.com")),
    ("personio", re.compile(r"([A-Za-z0-9-]+)\.jobs\.personio\.(?:com|de)")),
)

# Greenhouse/Lever/Ashby path segments that are never a board slug.
_SLUG_STOPWORDS = frozenset({"embed", "job_board", "jobs", "job", "static",
                             "assets", "css", "js", "api", "v1", "images"})


def extract_ats_links(html: str) -> list[AtsLink]:
    """Every ATS board reference in one page, supported lanes first.

    Deduped on (ats, slug). Order within a lane is document order.
    """
    text = html or ""
    found: list[AtsLink] = []
    seen: set[tuple[str, str]] = set()

    def add(ats: str, slug: str, evidence: str, supported: bool) -> None:
        if not slug or slug.lower() in _SLUG_STOPWORDS:
            return
        if (ats, slug) in seen:
            return
        seen.add((ats, slug))
        found.append(AtsLink(ats, slug, evidence, supported))

    for ats, rx in _SUPPORTED_PATTERNS:
        for m in rx.finditer(text):
            slug = ("|".join(m.groups()) if ats == "workday"
                    else m.group(1))
            add(ats, slug, m.group(0), True)
    for ats, rx in _UNSUPPORTED_PATTERNS:
        for m in rx.finditer(text):
            add(ats, m.group(1) or "", m.group(0), False)
    return found


def unsupported_markers(links) -> list[str]:
    """`unsupported_ats:<platform>=<slug>` evidence tokens."""
    out = []
    for link in links:
        if not link.supported:
            out.append(f"unsupported_ats:{link.ats}" + (f"={link.slug}" if link.slug else ""))
    return list(dict.fromkeys(out))


# --- one hop from the homepage ----------------------------------------------

_ANCHOR_RE = re.compile(
    r"""<a\b[^>]*?href\s*=\s*["']([^"'>]+)["'][^>]*>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_CAREER_WORDS = ("career", "job")
MAX_CAREER_HOPS = 4


def careers_links(html: str, base_url: str, limit: int = MAX_CAREER_HOPS) -> list[str]:
    """Absolute URLs of homepage links that look like a careers page.

    Many careers pages are one click deeper than the guessed paths; following
    one hop is what lifts the tier-3 hit rate.
    """
    out: list[str] = []
    for m in _ANCHOR_RE.finditer(html or ""):
        href = unescape(m.group(1)).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        label = unescape(_TAG_RE.sub(" ", m.group(2))).lower()
        if not any(w in href.lower() or w in label for w in _CAREER_WORDS):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).scheme not in ("http", "https"):
            continue
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


# --- acquisitions -----------------------------------------------------------

def acquirer_of(name: str, slug: str, new_ats: str, new_slug: str) -> str:
    """The acquiring tenant, when the resolved board plainly belongs to someone
    else -- `greenhouse/paciolan` resolving to `learfield.wd5...`/`Paciolan`.

    Such a board is a net-new company for the universe, not a replacement, so
    the tenant is recorded in its own column. Empty when the owner token still
    looks like the input company.
    """
    owner = (workday_tenant(new_slug) if new_ats == "workday" else new_slug or "")
    owner_stem = re.sub(r"[^a-z0-9]", "", owner.lower())
    if not owner_stem:
        return ""
    stem = name_stem(name)
    slug_stem = re.sub(r"[^a-z0-9]", "", (slug or "").split("|")[0].lower())
    for known in (stem, slug_stem):
        if known and (known in owner_stem or owner_stem in known):
            return ""
    return owner


# --- the verdict ------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """A board confirmed live: 200 with at least one posting."""
    ats: str
    slug: str
    jobs: int
    tier: str
    confidence: str = HIGH
    domain: str = ""


@dataclass(frozen=True)
class Offsite:
    """A company matched on a platform we have no client for.

    Never probed -- there is no API we can call -- so it carries no posting
    count. An unverified but strong positive: the row is in a live board dump
    under this company's name.
    """
    ats: str
    slug: str
    confidence: str = HIGH


@dataclass
class Outcome:
    """Everything the cascade learned about one input row."""
    old_status: str
    input_live: bool = False
    found: Candidate | None = None
    offsite: Offsite | None = None
    """A match on an unsupported platform. Mutually exclusive with `found`:
    the cascade stops at whichever it reaches first."""
    name_match: bool = False
    """Whether any platform's dump carries this company under another board.
    Positive evidence of existence, so it alone forbids `gone`."""
    domain: str = ""
    """A candidate domain that exists in DNS. Set from the lookup, not from a
    successful page fetch: the company's name existing is what blocks `gone`."""
    site_read: bool = False
    """Whether any page under `domain` actually answered. False with `domain`
    set is the resolves-but-unreadable case -- 403, timeout, dead web server."""
    web_checked: bool = False
    """Whether tier 3 actually ran. `--no-web` leaves it False, and `gone`
    requires it: skipping the website check is not evidence of absence."""
    errors: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


def decide_verdict(outcome: Outcome) -> tuple[str, str]:
    """(verdict, confidence). The only place a retireable verdict is produced.

    `error` outranks every negative finding: a 403/429/5xx/timeout means we did
    not learn anything, so the row must be re-probed on a later run rather than
    retired now. A DNS lookup that failed for anything but NXDOMAIN arrives as
    one of those errors.

    The three tier-3 outcomes land as: a domain read -> `alive_no_board` high;
    a domain that resolved but would not answer -> `alive_no_board` low;
    nothing resolving anywhere -> the only input that may reach `gone`.
    """
    found = outcome.found
    if found is not None:
        # Both halves of the live test, not just `input_live`: retiring the old
        # row requires the audit to have *observed* it not serving. Today the
        # two agree by construction (`input_live` is derived from
        # `old_status`), so the second clause is inert -- it is here so that
        # making `input_live` a real re-probe cannot turn a board we merely
        # failed to reach into a `moved`.
        if outcome.input_live or outcome.old_status in LIVE_STATUSES:
            return DUAL, found.confidence
        if outcome.old_status not in NOT_FOUND_STATUSES:
            # `empty` is the only other status that reaches here, and an empty
            # board answered 200 -- it exists. Retiring it is the one case that
            # would drop a live board, and it buys nothing: an empty board
            # yields no rows anyway, so keeping it costs a third of a poll on
            # the cold rotation. Both boards are kept and the find is still
            # reported in new_ats/new_slug.
            return DUAL, found.confidence
        if found.confidence == LOW:
            return REVIEW, LOW
        return MOVED, found.confidence
    if outcome.input_live:
        return OK, HIGH
    if outcome.errors:
        return ERROR, LOW
    offsite = outcome.offsite
    if offsite is not None:
        # Gated exactly as `moved`: a confirmed 404 on the old board, high
        # confidence in the name match, the old board never observed serving,
        # and no errors (already returned above). An `empty`, `stale` or
        # `active` board answered 200 and is never retired here.
        if (not outcome.input_live
                and outcome.old_status not in LIVE_STATUSES
                and outcome.old_status in NOT_FOUND_STATUSES
                and offsite.confidence == HIGH):
            return MOVED_OFFSITE, HIGH
        return REVIEW, offsite.confidence
    if outcome.domain:
        # A domain that resolved but served nothing readable still proves the
        # name exists, so it can never be `gone`; the low confidence says we
        # only ever saw DNS.
        return ALIVE_NO_BOARD, (HIGH if outcome.site_read else LOW)
    if outcome.name_match:
        # A company the dumps carry on some other board exists, whatever the
        # website check did or did not manage. Stated here rather than left to
        # fall out of the tiers: this is the guard, not a side effect of one.
        return REVIEW, LOW
    if (outcome.old_status in NOT_FOUND_STATUSES and outcome.web_checked
            and not outcome.name_match):
        return GONE, HIGH
    return REVIEW, LOW


def result_row(row: dict, outcome: Outcome) -> list:
    """One output CSV row. `new_ats`/`new_slug` carry the destination board.

    On `moved` that destination is a proposed addition. On `moved_offsite` it
    is a record only, and `new_jobs` stays blank because nothing was probed --
    a consumer must not read a 0 there as an empty board.
    """
    verdict, confidence = decide_verdict(outcome)
    found, offsite = outcome.found, outcome.offsite
    if found is not None:
        new_ats, new_slug, new_jobs = found.ats, found.slug, found.jobs
    elif offsite is not None:
        new_ats, new_slug, new_jobs = offsite.ats, offsite.slug, ""
    else:
        new_ats, new_slug, new_jobs = "", "", ""
    acquirer = (acquirer_of(row.get("name", ""), row.get("slug", ""), new_ats, new_slug)
                if new_ats and verdict in (MOVED, MOVED_OFFSITE, DUAL, REVIEW) else "")
    evidence = list(dict.fromkeys(outcome.evidence + [f"err:{e}" for e in outcome.errors]))
    return [
        row.get("ats", ""), row.get("slug", ""), row.get("name", ""),
        outcome.old_status, verdict,
        new_ats, new_slug, new_jobs, acquirer, confidence,
        outcome.domain, ";".join(evidence),
    ]


# --- board endpoints --------------------------------------------------------
# Deliberately lighter than the scrapers', same as the audit's: a liveness call
# needs the posting count, not the JDs. Workday goes through `workday.list_page`
# in the driver instead, because its list is a POST.

def board_url(ats: str, slug: str) -> str:
    if ats == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    if ats == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    if ats == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    raise ValueError(f"no board URL for ats {ats!r}")


def posting_count(ats: str, payload) -> int:
    """Postings in a board payload. Raises on a payload of the wrong shape --
    a malformed 200 is a wall, not an empty board."""
    if ats == "lever":
        if not isinstance(payload, list):
            raise TypeError(f"expected a JSON array, got {type(payload).__name__}")
        return len(payload)
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
    key = "jobPostings" if ats == "workday" else "jobs"
    items = payload.get(key)
    if not isinstance(items, list):
        raise TypeError(f"expected a list under {key!r}, got {type(items).__name__}")
    if ats == "workday":
        total = payload.get("total")
        return total if isinstance(total, int) and total >= len(items) else len(items)
    return len(items)
