"""The resume source model: load the profile sources, check them once, and
assemble a resume from a plan. Deterministic (R7): no LLM calls.

Every fact has one home, and each thing says where it appears:

  profile/resume.yaml      name, contact, education, and one `contexts:` entry
                           per job or project (header + per-lane bullet budget),
                           keyed by the bullet prefix (`DEL` in `B-DEL-01`)
  profile/bullets.md       claims; `default_for: [lanes]` marks baseline picks
  profile/skills_master.md skills; `show: {lane: line label}` places a skill
  verticals.yaml lane      `resume:` shape: section order, total bullet budget,
                           skill line labels, baseline summary

`check_sources` validates every pointer between them (run by verticals-check),
so deleting a skill, bullet or project means deleting one block, and a stale
pointer is named. `build` turns a plan (the model's choices) into resume
markdown plus a trace, and reports every broken rule as a problem instead of
rendering it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from src import lint
from src.session_schema import ProblemCode
from src.shared import paths, verticals

RESUME = paths.PROFILE / "resume.yaml"
BULLETS = paths.PROFILE / "bullets.md"
SKILLS = paths.PROFILE / "skills_master.md"

SECTIONS = ("summary", "work", "projects", "education", "skills")
BULLET_KEYS = ("canonical", "tags", "default_for", "evidence", "allowable_synonyms")
SKILL_KEYS = ("name", "evidence", "allowable_synonyms", "show")
SECTION_TITLES = {
    "summary": "SUMMARY",
    "work": "WORK EXPERIENCE",
    "projects": "PROJECTS",
    "education": "EDUCATION",
    "skills": "TECHNICAL SKILLS",
}
_BULLET_ID = re.compile(r"^B-([A-Z0-9]+)-(\d+)$")
_BULLET_REF = re.compile(r"\bB-([A-Z0-9]+)-(\d+(?:/\d+)*)")
_ANY_ID = re.compile(r"\b(?:B-[A-Z0-9]+-\d+|SKILL-[A-Z0-9-]+)\b")


class SourceError(ValueError):
    """A profile source file is missing or malformed."""


# =====================================================================
# Loading
# =====================================================================


def parse_entries(text: str, name: str = "<text>") -> dict[str, dict]:
    """Parse the `## ID` / `key: value` format of bullets.md, skills_master.md
    and stories.md. A value starting with `[` or `{` is YAML. Comment lines
    (`#` at column 0) and blank lines are skipped. A repeated id, a line that
    is not `key: value` and bad inline YAML raise SourceError with the line."""
    entries: dict[str, dict] = {}
    current: dict | None = None
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip()
        if line.startswith("## "):
            eid = line[3:].strip()
            if eid in entries:
                raise SourceError(f"{name}:{n}: duplicate id {eid!r}")
            current = entries[eid] = {}
            continue
        if not line or line.startswith("#"):
            continue
        if current is None or ":" not in line:
            raise SourceError(f"{name}:{n}: stray line (not `key: value` under a `## ID`)")
        key, _, val = line.partition(":")
        val = val.strip()
        if val[:1] in "[{":
            try:
                val = yaml.safe_load(val)
            except yaml.YAMLError as exc:
                raise SourceError(f"{name}:{n}: bad inline YAML in {key.strip()!r}") from exc
        current[key.strip()] = val
    return entries


@dataclass(frozen=True)
class Sources:
    resume: dict
    bullets: dict[str, dict]
    skills: dict[str, dict]
    rules: dict

    @property
    def contexts(self) -> dict[str, dict]:
        return self.resume.get("contexts") or {}

    def context_of(self, bullet_id: str) -> str:
        m = _BULLET_ID.match(bullet_id)
        return m.group(1) if m else ""


def load_sources() -> Sources:
    for p in (RESUME, BULLETS, SKILLS):
        if not p.is_file():
            raise SourceError(
                f"{p.relative_to(paths.REPO_ROOT)} missing — copy its .example twin and fill it in."
            )
    resume = yaml.safe_load(RESUME.read_text(encoding="utf-8")) or {}
    if not isinstance(resume, dict):
        raise SourceError("profile/resume.yaml: top level must be a mapping")
    return Sources(
        resume=resume,
        bullets=parse_entries(BULLETS.read_text(encoding="utf-8"), BULLETS.name),
        skills=parse_entries(SKILLS.read_text(encoding="utf-8"), SKILLS.name),
        rules=lint.load_de_ai_rules(),
    )


def evidence_bullets(skill: dict) -> set[str]:
    """Bullet ids named in a skill's free-text `evidence`, expanding the
    `B-RAGSEC-01/02/04` shorthand."""
    out: set[str] = set()
    for m in _BULLET_REF.finditer(str(skill.get("evidence") or "")):
        for n in m.group(2).split("/"):
            out.add(f"B-{m.group(1)}-{n}")
    return out


def lane_budget(src: Sources, ctx: str, lane: str) -> tuple[int, int] | None:
    """(min, max) bullets for a context on a lane, or None when not shown."""
    b = (src.contexts.get(ctx, {}).get("lanes") or {}).get(lane)
    return (int(b["min"]), int(b["max"])) if isinstance(b, dict) else None


def lane_order(src: Sources, ctx: str, lane: str) -> tuple[int, int]:
    """Sort key for a context on a lane's baseline: its `order`, then file order."""
    b = (src.contexts.get(ctx, {}).get("lanes") or {}).get(lane) or {}
    return (int(b.get("order", 999)), list(src.contexts).index(ctx) if ctx in src.contexts else 999)


def shown_skills(src: Sources, lane: str) -> dict[str, list[str]]:
    """{line label: [SKILL ids in skills_master.md order]} for a lane."""
    out: dict[str, list[str]] = {}
    for sid, s in src.skills.items():
        label = (s.get("show") or {}).get(lane) if isinstance(s.get("show"), dict) else None
        if label:
            out.setdefault(label, []).append(sid)
    return out


# =====================================================================
# Checking the sources (once, at the source)
# =====================================================================


def check_sources(src: Sources, cfg: verticals.VerticalsConfig) -> list[str]:
    """Every problem in the sources, as one line each. Empty = clean."""
    problems: list[str] = []
    lanes = {n: v for n, v in cfg.verticals.items() if v.resume is not None}
    lane_names = set(cfg.verticals)
    r = src.resume

    if not isinstance(r.get("name"), str) or not r["name"].strip():
        problems.append("resume.yaml: `name` must be a nonempty string")
    for i, c in enumerate(r.get("contact") or []):
        if not isinstance(c, dict) or not isinstance(c.get("text"), str):
            problems.append(f"resume.yaml contact[{i}]: needs `text` (and optional `url`)")
    if not r.get("contact"):
        problems.append("resume.yaml: `contact` must list at least one item")
    for i, e in enumerate(r.get("education") or []):
        if not isinstance(e, dict) or not all(
            isinstance(e.get(k), str) for k in ("degree", "dates")
        ):
            problems.append(f"resume.yaml education[{i}]: needs `degree` and `dates`")
    for ctx, c in src.contexts.items():
        where = f"resume.yaml contexts.{ctx}"
        if not isinstance(c, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        if c.get("section") not in ("work", "projects"):
            problems.append(f"{where}.section: must be work or projects")
        for k in ("title", "dates"):
            if not isinstance(c.get(k), str) or not c[k].strip():
                problems.append(f"{where}.{k}: must be a nonempty string")
        if c.get("section") == "work" and not all(
            isinstance(c.get(k), str) for k in ("org", "location")
        ):
            problems.append(f"{where}: a work context needs `org` and `location`")
        for lane, b in (c.get("lanes") or {}).items():
            if lane not in lane_names:
                problems.append(f"{where}.lanes: {lane!r} is not a configured lane")
            elif not (
                isinstance(b, dict)
                and set(b) <= {"min", "max", "order"}
                and all(type(b.get(k)) is int for k in ("min", "max"))
                and 0 <= b["min"] <= b["max"]
                and type(b.get("order", 0)) is int
            ):
                problems.append(
                    f"{where}.lanes.{lane}: must be {{min, max, order?}} with 0 <= min <= max"
                )

    for label, entries, keys in (
        ("bullets.md", src.bullets, BULLET_KEYS),
        ("skills_master.md", src.skills, SKILL_KEYS),
    ):
        for eid, e in entries.items():
            for k in sorted(set(e) - set(keys)):
                problems.append(f"{label} {eid}: unknown key {k!r} (allowed: {', '.join(keys)})")

    ctx_bullets: dict[str, list[str]] = {}
    for bid, b in src.bullets.items():
        m = _BULLET_ID.match(bid)
        if not m:
            problems.append(f"bullets.md {bid}: id must look like B-<CTX>-NN")
            continue
        if not str(b.get("canonical") or "").strip():
            problems.append(f"bullets.md {bid}: missing `canonical`")
        if m.group(1) not in src.contexts:
            problems.append(f"bullets.md {bid}: no context {m.group(1)!r} in resume.yaml")
        ctx_bullets.setdefault(m.group(1), []).append(bid)
        for lane in b.get("default_for") or []:
            if lane not in lane_names:
                problems.append(f"bullets.md {bid}.default_for: {lane!r} is not a configured lane")
            elif lane_budget(src, m.group(1), lane) is None:
                problems.append(
                    f"bullets.md {bid}.default_for: context {m.group(1)} "
                    f"is not shown on lane {lane}"
                )
    for ctx in src.contexts:
        if ctx not in ctx_bullets:
            problems.append(f"resume.yaml contexts.{ctx}: no bullets in bullets.md")

    for sid, s in src.skills.items():
        if not str(s.get("name") or "").strip():
            problems.append(f"skills_master.md {sid}: missing `name`")
        show = s.get("show") or {}
        if not isinstance(show, dict):
            problems.append(f"skills_master.md {sid}.show: must be {{lane: line label}}")
            continue
        for lane, label in show.items():
            if lane not in lane_names:
                problems.append(f"skills_master.md {sid}.show: {lane!r} is not a configured lane")
            elif lane in lanes and label not in lanes[lane].resume.skill_lines:
                problems.append(
                    f"skills_master.md {sid}.show.{lane}: {label!r} is not one of "
                    f"that lane's skill_lines"
                )
        for ref in sorted(evidence_bullets(s) - set(src.bullets)):
            problems.append(f"skills_master.md {sid}.evidence: {ref} is not in bullets.md")

    for lane, v in lanes.items():
        problems += _check_lane(src, lane, v)

    for text, where in _source_texts(src, lanes):
        for viol in lint.find_phrase_violations(text, context="resume", rules=src.rules):
            problems.append(f"{where}: banned phrase {viol['phrase']!r} ({viol['category']})")
    return problems


def _check_lane(src: Sources, lane: str, v: verticals.Vertical) -> list[str]:
    problems = []
    lo, hi = v.resume.bullets
    shown = [c for c in src.contexts if lane_budget(src, c, lane) is not None]
    mins = sum(lane_budget(src, c, lane)[0] for c in shown)
    maxes = sum(lane_budget(src, c, lane)[1] for c in shown)
    if mins > hi or maxes < lo:
        problems.append(
            f"lane {lane}: context budgets ({mins}-{maxes}) cannot meet "
            f"resume.bullets {list(v.resume.bullets)}"
        )
    defaults = [b for b, e in src.bullets.items() if lane in (e.get("default_for") or [])]
    if not lo <= len(defaults) <= hi:
        problems.append(
            f"lane {lane}: {len(defaults)} default_for bullets, outside "
            f"resume.bullets {list(v.resume.bullets)}"
        )
    for c in shown:
        n = sum(1 for b in defaults if src.context_of(b) == c)
        cmin, cmax = lane_budget(src, c, lane)
        if not cmin <= n <= cmax:
            problems.append(
                f"lane {lane}: {n} default_for bullets in {c}, outside [{cmin}, {cmax}]"
            )
    lines = shown_skills(src, lane)
    for label in v.resume.skill_lines:
        if not lines.get(label):
            problems.append(f"lane {lane}: skill line {label!r} has no skill with that `show`")
    names = {sid: str(src.skills[sid]["name"]) for ids in lines.values() for sid in ids}
    by_name = {n.lower(): sid for sid, n in names.items()}
    for a, na in names.items():
        for group in re.findall(r"\(([^)]*)\)", na):
            for item in (i.strip().lower() for i in group.split(",")):
                if item in by_name and by_name[item] != a:
                    problems.append(
                        f"lane {lane}: {a} name {na!r} lists {by_name[item]} "
                        f"({names[by_name[item]]!r}); the resume would show it twice"
                    )
    prose = verticals.VERTICALS_DIR / lane / "tailoring.md"
    if prose.is_file():
        ids = sorted(set(_ANY_ID.findall(prose.read_text(encoding="utf-8"))))
        if ids:
            problems.append(
                f"profile/verticals/{lane}/tailoring.md names ids {ids[:5]}: "
                f"picks live in bullets.md default_for and skills in `show`"
            )
    return problems


def _source_texts(src: Sources, lanes: dict) -> list[tuple[str, str]]:
    out = [(str(b.get("canonical") or ""), f"bullets.md {bid}") for bid, b in src.bullets.items()]
    out += [(v.resume.summary, f"verticals.yaml {n}.resume.summary") for n, v in lanes.items()]
    out += [
        (str(c.get(k) or ""), f"resume.yaml contexts.{ctx}.{k}")
        for ctx, c in src.contexts.items()
        for k in ("title", "subtitle")
    ]
    return out


# =====================================================================
# Grounding: numbers and reworded words
# =====================================================================

_NUMBER = re.compile(r"(?<![\w.])[~$]?\d[\d,]*(?:\.\d+)?(?:%|x|k|m|b)?(?!\w)", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9][a-z0-9+#&/.%'-]*[a-z0-9%+#]|[a-z0-9]")


def numbers(text: str) -> set[str]:
    return {m.group().lower().lstrip("~$").replace(",", "") for m in _NUMBER.finditer(text)}


def _stem(word: str) -> str:
    for suffix in ("ies", "es", "ed", "ing", "s", "d"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def bullet_text(src: Sources, bid: str) -> str:
    b = src.bullets.get(bid) or {}
    return " ".join(
        [str(b.get("canonical") or "")] + [str(s) for s in b.get("allowable_synonyms") or []]
    )


def unlicensed_words(src: Sources, bid: str, text: str) -> list[str]:
    """Words in a rewording found in neither the bullet's canonical text, its
    allowable_synonyms, nor the stopword list (simple endings forgiven)."""
    stop = set(str(src.rules.get("stopwords") or "").lower().split())
    allowed = set(words(bullet_text(src, bid))) | stop
    stems = {_stem(w) for w in allowed}
    return [w for w in words(text) if w not in allowed and _stem(w) not in stems]


def ungrounded_numbers(text: str, sources_text: str) -> list[str]:
    allowed = numbers(sources_text)
    return sorted(n for n in numbers(text) if n not in allowed)


def claimed_skills(src: Sources, text: str) -> list[str]:
    """Skill ids whose name or alias appears in text."""
    low = text.lower()
    hits = []
    for sid, s in src.skills.items():
        terms = [str(s.get("name") or "")] + [str(a) for a in s.get("allowable_synonyms") or []]
        if any(t and re.search(rf"(?<!\w){re.escape(t.lower())}(?!\w)", low) for t in terms):
            hits.append(sid)
    return hits


def keep_terms(src: Sources, *extra: str) -> list[str]:
    """Proper nouns the linter must not scan: skill names and aliases, context
    titles and orgs, plus caller extras (company, recipient)."""
    terms = [str(s.get("name") or "") for s in src.skills.values()]
    terms += [str(a) for s in src.skills.values() for a in s.get("allowable_synonyms") or []]
    terms += [str(c.get(k) or "") for c in src.contexts.values() for k in ("title", "org")]
    return [t for t in [*terms, *extra] if t]


# =====================================================================
# Plans and assembly
# =====================================================================


@dataclass
class Problem:
    where: str
    code: ProblemCode
    detail: str

    def as_dict(self) -> dict:
        return {"where": self.where, "code": self.code, "detail": self.detail}


@dataclass
class Built:
    markdown: str
    trace: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    swapped: list[str] = field(default_factory=list)  # --final fallbacks taken


def baseline_plan(src: Sources, lane: str) -> dict:
    """The lane's default resume: baseline summary, default_for picks, every
    shown skill in file order."""
    return {
        "summary": {"text": verticals.get_config().verticals[lane].resume.summary, "uses": []},
        "entries": [
            {"id": b}
            for b in sorted(
                (b for b, e in src.bullets.items() if lane in (e.get("default_for") or [])),
                key=lambda b: lane_order(src, src.context_of(b), lane),
            )
        ],
        "skills": {},
    }


def baseline_markdown(lane: str) -> str:
    src = load_sources()
    return build(src, lane, baseline_plan(src, lane)).markdown


def _canonical_key(bid: str) -> tuple[str, int]:
    m = _BULLET_ID.match(bid)
    return (m.group(1), int(m.group(2))) if m else (bid, 0)


def build(
    src: Sources,
    lane: str,
    plan: dict,
    *,
    company: str = "",
    keywords: tuple[str, ...] = (),
    final: bool = False,
) -> Built:
    """Assemble resume markdown from a plan. Problems are collected, never
    raised; with `final`, a failing rewording falls back to its canonical text
    and a failing summary to the lane baseline, and the swap is recorded."""
    v = verticals.get_config().verticals[lane]
    shape = v.resume
    problems: list[Problem] = []
    swapped: list[str] = []
    keep = keep_terms(src, company)

    def fix(t):
        return lint.fix_mechanical(t, src.rules)[0]

    def lint_problems(text: str, where: str) -> list[Problem]:
        return [
            Problem(where, ProblemCode.LINT, f"banned phrase {x['phrase']!r} ({x['category']})")
            for x in lint.find_phrase_violations(text, context="resume", rules=src.rules, keep=keep)
        ]

    # --- summary
    summary = plan.get("summary") or {}
    s_text = fix(str(summary.get("text") or "").strip()) or shape.summary
    s_uses = [u for u in summary.get("uses") or []]
    s_source = "baseline"
    if s_text != shape.summary:
        s_source = "written"
        sp = [
            Problem("summary", ProblemCode.UNKNOWN_ID, f"{u} is not in bullets.md")
            for u in s_uses
            if u not in src.bullets
        ]
        grounds = " ".join(
            [shape.summary, context_text(src)] + [bullet_text(src, u) for u in s_uses]
        )
        sp += [
            Problem("summary", ProblemCode.NUMBER, f"{n} is in none of the cited bullets")
            for n in ungrounded_numbers(s_text, grounds)
        ]
        backed = {s for s in claimed_skills(src, grounds)} | {
            sid for sid, s in src.skills.items() if evidence_bullets(s) & set(s_uses)
        }
        sp += [
            Problem(
                "summary",
                ProblemCode.SKILL,
                f"{src.skills[s]['name']!r} ({s}) is backed by no cited bullet",
            )
            for s in claimed_skills(src, s_text)
            if s not in backed
        ]
        sp += lint_problems(s_text, "summary")
        if sp and final:
            swapped.append("summary -> lane baseline")
            s_text, s_source = shape.summary, "baseline"
        else:
            problems += sp

    # --- bullets
    chosen: dict[str, str] = {}
    rewordings: dict[str, str] = {}
    order: list[str] = []
    for e in plan.get("entries") or []:
        bid = str((e or {}).get("id") or "")
        if bid not in src.bullets:
            problems.append(
                Problem(bid or "entries", ProblemCode.UNKNOWN_ID, f"{bid!r} is not in bullets.md")
            )
            continue
        if bid in chosen:
            problems.append(Problem(bid, ProblemCode.UNKNOWN_ID, "listed twice"))
            continue
        ctx = src.context_of(bid)
        if lane_budget(src, ctx, lane) is None:
            problems.append(
                Problem(bid, ProblemCode.CONTEXT, f"context {ctx} is not shown on lane {lane}")
            )
            continue
        canonical = fix(str(src.bullets[bid]["canonical"]).strip())
        text = fix(str(e.get("text") or "").strip())
        if text and text != canonical:
            bp = [
                Problem(
                    bid, ProblemCode.LICENSE, f"{w!r} is in neither the bullet nor its synonyms"
                )
                for w in dict.fromkeys(unlicensed_words(src, bid, text))
            ]
            bp += [
                Problem(bid, ProblemCode.NUMBER, f"{n} is not in the bullet")
                for n in ungrounded_numbers(text, bullet_text(src, bid))
            ]
            bp += lint_problems(text, bid)
            if bp and final:
                swapped.append(f"{bid} -> canonical")
                text = canonical
            else:
                problems += bp
                rewordings[bid] = text
        chosen[bid] = text or canonical
        if ctx not in order:
            order.append(ctx)

    counts = {c: sum(1 for b in chosen if src.context_of(b) == c) for c in src.contexts}
    for c in src.contexts:
        budget = lane_budget(src, c, lane)
        if budget and not budget[0] <= counts[c] <= budget[1]:
            problems.append(
                Problem(
                    c, ProblemCode.BUDGET, f"{counts[c]} bullets, lane {lane} allows {list(budget)}"
                )
            )
    lo, hi = shape.bullets
    if not lo <= len(chosen) <= hi:
        problems.append(
            Problem(
                "entries",
                ProblemCode.BUDGET,
                f"{len(chosen)} bullets, lane {lane} allows [{lo}, {hi}]",
            )
        )
    for c in src.contexts:
        budget = lane_budget(src, c, lane)
        if budget and budget[0] > 0 and c not in order:
            order.append(c)

    # --- skills
    lines = shown_skills(src, lane)
    plan_skills = plan.get("skills") or {}
    skill_lines: list[tuple[str, list[tuple[str, str]]]] = []
    for label in plan_skills:
        if label not in shape.skill_lines:
            problems.append(
                Problem(
                    f"skills.{label}", ProblemCode.LAYOUT, f"not one of lane {lane}'s skill_lines"
                )
            )
    for label in shape.skill_lines:
        picks = plan_skills.get(label)
        rendered: list[tuple[str, str]] = []
        for item in picks if picks else lines.get(label, []):
            sid, alias = (
                (item, "")
                if isinstance(item, str)
                else (str(item.get("id")), str(item.get("alias") or ""))
            )
            s = src.skills.get(sid)
            if s is None:
                problems.append(
                    Problem(
                        f"skills.{label}",
                        ProblemCode.UNKNOWN_ID,
                        f"{sid} is not in skills_master.md",
                    )
                )
                continue
            if sid not in lines.get(label, []):
                problems.append(
                    Problem(
                        f"skills.{label}",
                        ProblemCode.LAYOUT,
                        f"{sid} does not show on this line for lane {lane}",
                    )
                )
                continue
            if alias and alias not in (s.get("allowable_synonyms") or []):
                problems.append(
                    Problem(
                        f"skills.{label}",
                        ProblemCode.SKILL,
                        f"{alias!r} is not an allowable synonym of {sid}",
                    )
                )
                continue
            rendered.append((sid, alias or str(s["name"])))
        if picks:  # a plan reorders a line; the skills it leaves out follow in default order
            listed = {sid for sid, _ in rendered}
            rendered += [
                (sid, str(src.skills[sid]["name"]))
                for sid in lines.get(label, [])
                if sid not in listed and sid in src.skills
            ]
        if rendered:
            skill_lines.append((label, rendered))

    # --- markdown + trace
    md, trace = _render(src, lane, s_text, s_source, s_uses, order, chosen, rewordings, skill_lines)
    trace += _keyword_trace(md, keywords)
    return Built(markdown=md, trace=trace, problems=problems, swapped=swapped)


def context_text(src: Sources) -> str:
    return " ".join(
        str(c.get(k) or "") for c in src.contexts.values() for k in ("title", "org", "dates")
    )


def _render(src, lane, s_text, s_source, s_uses, order, chosen, rewordings, skill_lines):
    r = src.resume
    v = verticals.get_config().verticals[lane]
    lines: list[str] = []
    trace: list[str] = []

    def add(line: str, source: str = "", how: str = "", extra: list[str] | None = None) -> None:
        lines.append(line)
        lines.append("")
        if source:
            trace.append(f"L{len(lines) - 1} source={source} transformation={how}")
            trace.extend(extra or [])

    add(f"**{r['name']}**")
    add(
        " | ".join(
            f"[{c['text']}]({c['url']})" if c.get("url") else c["text"] for c in r["contact"]
        )
    )
    for section in v.resume.sections:
        add(f"**{SECTION_TITLES[section]}**")
        if section == "summary":
            add(s_text, "summary", s_source, [f"  uses: {s_uses}"] if s_uses else [])
        elif section in ("work", "projects"):
            for ctx in order:
                c = src.contexts[ctx]
                if c["section"] != section:
                    continue
                if section == "work":
                    add(f"**{c['title']}** - {c['org']}\t{c['location']} | {c['dates']}")
                else:
                    sub = f" - {c['subtitle']}" if c.get("subtitle") else ""
                    add(f"**{c['title']}**{sub}\t{c['dates']}")
                for bid in sorted(
                    (b for b in chosen if src.context_of(b) == ctx), key=_canonical_key
                ):
                    if bid in rewordings:
                        add(
                            f"* {chosen[bid]}",
                            bid,
                            "rephrase",
                            [
                                f"  before:   {src.bullets[bid]['canonical']}",
                                f"  after:    {chosen[bid]}",
                            ],
                        )
                    else:
                        add(f"* {chosen[bid]}", bid, "unchanged")
        elif section == "education":
            for e in r.get("education") or []:
                add(f"**{e['degree']}**\t{e['dates']}")
                if e.get("detail"):
                    add(e["detail"])
        elif section == "skills":
            for label, items in skill_lines:
                add(
                    f"**{label}:** " + ", ".join(t for _, t in items),
                    ",".join(s for s, _ in items),
                    "skills",
                )
    return "\n".join(lines).rstrip() + "\n", trace


def _keyword_trace(md: str, keywords: tuple[str, ...]) -> list[str]:
    if not keywords:
        return []
    md_lines = md.split("\n")
    out = ["", "## Keywords"]
    for kw in keywords:
        hit = [
            f"L{i}"
            for i, line in enumerate(md_lines, start=1)
            if re.search(rf"(?<!\w){re.escape(kw.lower())}(?!\w)", line.lower())
        ]
        out.append(f"- {kw}: {', '.join(hit) if hit else 'not landed'}")
    return out
