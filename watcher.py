"""
Job Watcher
-----------
Checks Indeed, LinkedIn and Google Jobs for newly posted Software / Java /
Frontend / Data / DevOps engineer roles in the USA, filters for 0-3 years of experience and
visa sponsorship, and sends a push notification to your phone via ntfy.

Runs every 15 minutes on GitHub Actions (see .github/workflows/watch.yml).

Local usage:
    python watcher.py                 # normal run
    python watcher.py --dry-run       # print matches, send nothing, save nothing
    python watcher.py --test-notify   # send one test push to your phone
"""

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# =============================================================================
# SETTINGS (edit these)
# =============================================================================

# =============================================================================
# YOUR 5 RESUMES
# Each alert says which resume to use. Names match the folders in
# ~/Documents/career-ops/resume-generation/<name>/content.tex
#   search:  terms searched on the job sites for this resume
#   titles:  job titles this resume is built for (regex, case-insensitive)
#   skills:  skills on this resume; matched against the job description
#   title_weight: how much a title match counts versus each matched skill.
#            Specific titles (DevOps, Data Engineer) count a lot; generic ones
#            (Software Engineer) less, so the description decides.
# Order matters only to break ties (more specific resumes first).
# =============================================================================

RESUMES = {
    "devops-engineer": {
        "title_weight": 10,
        "search": ["devops engineer", "site reliability engineer"],
        "titles": [
            r"\bdev\s*ops\b", r"\bsite reliability\b", r"\bsre\b",
            r"\bplatform engineer\b", r"\bcloud engineer\b",
            r"\binfrastructure engineer\b", r"\b(build|release) engineer\b",
        ],
        "skills": [
            "Docker", "Kubernetes", "Terraform", "Ansible", "CI/CD", "Jenkins",
            "GitHub Actions", "Infrastructure as Code", "Nginx", "AWS", "EC2",
            "S3", "IAM", "GCP", "Prometheus", "Grafana", "monitoring",
            "observability", "logging", "alerting", "Linux", "Bash", "Python",
            "Cloudflare", "DNS",
        ],
    },
    "Data-Engineer": {
        "title_weight": 10,
        "search": ["data engineer"],
        "titles": [
            r"\bdata engineer", r"\betl (developer|engineer)\b",
            r"\bbig data\b", r"\banalytics engineer\b", r"\bdata pipeline\b",
        ],
        "skills": [
            "ETL", "ELT", "data pipeline", "SQL", "Python", "Spark", "Kafka",
            "Airflow", "Spring Batch", "PostgreSQL", "MySQL", "Oracle", "MongoDB",
            "TimescaleDB", "Redis", "data warehouse", "data modeling", "schema",
            "batch", "streaming", "AWS", "GCP", "Lambda", "S3", "Docker",
            "Terraform", "Pandas",
        ],
    },
    "frontend": {
        "title_weight": 10,
        "search": ["frontend engineer", "react developer"],
        "titles": [
            r"\bfront\s*-?\s*end\b", r"\bui (engineer|developer)\b",
            r"\breact\b", r"\bjavascript (engineer|developer)\b",
            r"\btypescript (engineer|developer)\b", r"\bweb developer\b",
        ],
        "skills": [
            "React", "React Native", "TypeScript", "JavaScript", "Next.js",
            "Redux", "Zustand", "React Query", "Tailwind", "HTML", "CSS",
            "Expo", "Node.js", "REST", "GraphQL", "Jest", "Vitest",
            "React Testing Library", "Playwright", "Webpack", "Vite",
            "accessibility", "Lighthouse", "Material UI",
        ],
    },
    "java-developer": {
        "title_weight": 6,
        "search": ["java developer"],
        "titles": [
            r"\bjava\b", r"\bspring boot\b", r"\bback\s*-?\s*end\b",
            r"\bj2ee\b",
        ],
        "skills": [
            "Java", "Spring Boot", "Spring Batch", "Spring Security",
            "Spring Data JPA", "Hibernate", "Microservices", "REST", "Kafka",
            "Multithreading", "Concurrency", "Oracle", "MySQL", "PostgreSQL",
            "SQL", "PL/SQL", "JUnit", "Mockito", "Maven", "Gradle", "SonarQube",
            "Docker", "Kubernetes", "AWS", "OAuth", "JWT",
        ],
    },
    "software enginner": {
        "title_weight": 3,
        "search": ["software engineer", "full stack developer", "junior software engineer",
                   "associate software engineer", "entry level software engineer"],
        "titles": [
            r"\bsoftware (engineer|developer|dev(elopment)? engineer)\b", r"\bsde\b",
            r"\b(junior|jr|associate|entry[- ]level|new grad|graduate|early career)\b.*\b(developer|engineer)\b",
            r"\b(developer|engineer) (i|1)\b",
            r"\bfull\s*-?\s*stack\b", r"\bapplication developer\b",
            r"\bai engineer\b", r"\bforward deployed\b", r"\bllm engineer\b",
        ],
        "skills": [
            "Python", "Java", "TypeScript", "JavaScript", "React", "Next.js",
            "Spring Boot", "FastAPI", "Django", "Flask", "Node.js", "REST",
            "GraphQL", "Microservices", "WebSockets", "PostgreSQL", "MongoDB",
            "Redis", "Kafka", "Docker", "Kubernetes", "AWS", "GCP", "CI/CD",
            "LangChain", "LangGraph", "LLM", "RAG", "OAuth", "system design",
        ],
    },
}

# Jobs whose description mentions fewer of the best resume's skills than this
# are skipped. Only applied when the description is long enough to judge.
MIN_SKILL_MATCHES = 2

# Jobs with a shorter description are dropped: without the text, the
# sponsorship and experience checks can't run (LinkedIn often returns none).
MIN_DESCRIPTION_CHARS = 300

# Searched terms = all resume search terms, without duplicates.
SEARCH_TERMS = list(dict.fromkeys(t for r in RESUMES.values() for t in r["search"]))

LOCATION = "United States"

# Sites to check. Each site runs separately; if one fails or is blocked,
# the error is logged and the others still run.
# Note: zip_recruiter and glassdoor round "hours old" up to 1 day, so a job
# first seen there may be up to ~24 hours old.
# ponytail: google, zip_recruiter, glassdoor return 403/empty from GitHub runners; add back if that changes
SITES = ["indeed", "linkedin"]

# Look-back window. GitHub often runs the schedule hours late, so each search
# looks back to the last time THAT site+term was searched successfully (saved
# in state.json), plus 1 hour of overlap. Never less than HOURS_OLD_MIN, never
# more than HOURS_OLD_MAX. Already-alerted jobs are skipped.
HOURS_OLD_MIN = 2
HOURS_OLD_MAX = 48
HOURS_OLD_FIRST_TIME = 24   # window for a search never run before

# How many results to pull per search: RESULTS_PER_SEARCH for a short window,
# growing with the window up to the caps (LinkedIn lower: it rate-limits).
RESULTS_PER_SEARCH = 50
RESULTS_MAX = 200
RESULTS_MAX_LINKEDIN = 100

# LinkedIn rate-limits (HTTP 429) when searched back to back. Wait this many
# seconds between LinkedIn searches, and on a 429 wait LINKEDIN_RETRY_WAIT
# seconds and retry that search once.
LINKEDIN_PAUSE_SECONDS = 20
LINKEDIN_RETRY_WAIT = 60

# Each LinkedIn search also opens ~50 job pages for descriptions, so all
# terms every run triggers rate limits. LinkedIn searches only this many terms
# per run: the ones searched least recently (see state.json), so every term
# takes its turn and its next search looks back far enough to cover the gap.
LINKEDIN_TERMS_PER_RUN = 4


def linkedin_terms_this_run(state=None):
    """The LINKEDIN_TERMS_PER_RUN terms searched longest ago on LinkedIn.
    Never-searched terms come first; ties keep SEARCH_TERMS order."""
    last = (state or {}).get("last_searched", {})
    terms = list(SEARCH_TERMS)
    n = min(LINKEDIN_TERMS_PER_RUN, len(terms))
    ranked = sorted(terms, key=lambda t: last.get(f"linkedin|{t}", ""))
    return ranked[:n]


def window_for(site: str, term: str, state: dict, now: datetime):
    """(hours_old, results_wanted) for one search."""
    stamp = (state or {}).get("last_searched", {}).get(f"{site}|{term}")
    if stamp:
        try:
            gap = (now - datetime.fromisoformat(stamp)).total_seconds() / 3600
            hours = int(gap) + 2   # round up + 1 hour overlap
        except ValueError:
            hours = HOURS_OLD_FIRST_TIME
    else:
        hours = HOURS_OLD_FIRST_TIME
    hours = max(HOURS_OLD_MIN, min(HOURS_OLD_MAX, hours))
    cap = RESULTS_MAX_LINKEDIN if site == "linkedin" else RESULTS_MAX
    results = min(cap, RESULTS_PER_SEARCH * max(1, -(-hours // 4)))
    return hours, results

# Fetch full LinkedIn descriptions (needed for sponsorship/experience checks).
# Slower and more likely to be rate limited. Set False if LinkedIn keeps failing.
FETCH_LINKEDIN_DESCRIPTION = True

# Title must match at least one resume's title patterns (see RESUMES).
TITLE_INCLUDE = [p for r in RESUMES.values() for p in r["titles"]]

# Title must NOT match any of these. Senior/Sr dropped: those roles ask
# 5+ years even when the description also mentions smaller numbers.
TITLE_EXCLUDE = [
    r"\bsenior\b",
    r"\bsr\b",
    r"\bprincipal\b",
    r"\bstaff\b",
    r"\blead\b",
    r"\bmanager\b",
    r"\bmgr\b",
    r"\bsecret\b",
    r"\bpolygraph\b",
    r"\bdirector\b",
    r"\bhead of\b",
    r"\bvice president\b",
    r"\bvp\b",
    r"\bchief\b",
    r"\barchitect\b",
    r"\bintern(ship)?\b",
    r"\bIII\b",
    r"\bIV\b",
    r"\bclearance\b",
    r"\bTS/SCI\b",
]

# Company names matching these are dropped. Empty by default: companies with
# "Federal" or "Defense" in the name (e.g. Navy Federal Credit Union) can
# still sponsor. Roles that need citizenship or clearance are already dropped
# by NO_SPONSORSHIP_PATTERNS and TITLE_EXCLUDE. Add regexes here to skip
# specific companies, e.g. r"\bacme federal\b".
COMPANY_EXCLUDE = []

# Skip jobs whose lowest stated experience requirement is above this.
MAX_YEARS_EXPERIENCE = 3

# Description phrases that mean "no sponsorship" or "not for you". Job is dropped.
NO_SPONSORSHIP_PATTERNS = [
    r"no (visa )?sponsorship",
    r"not (be )?(able|eligible|in a position) to (provide |offer )?sponsor",
    r"(will|can|does|do) not (provide |offer )?(visa )?sponsor",
    r"unable to (provide |offer )?(visa )?sponsor",
    r"cannot (provide |offer )?(visa )?sponsor",
    r"(must|should|will) not (now or in the future )?require (visa |employer )?sponsorship",
    r"\bno (h-?1b|visa)\b",
    r"only[^.]{0,40}citizens? (or|and) (lawful )?(permanent residents?|green card)",
    r"citizens? (or|and) (lawful )?(permanent residents?|green card)( holders?)? only",
    r"\b(usc|gc)\s*(/|or|and|,)\s*(gc|usc)\s*(only|candidates only)",
    r"u\.?s\.? citizen(ship)? (is )?required",
    # Any clearance mention: "Clearance Requirements", "obtain a secret security
    # clearance", "Clearance Level", "DOE Q Clearance"... all need US citizenship.
    r"\bclearance\b",
    r"\bts\s*/+\s*sci\b",
    r"\btop[\s-]secret\b",
    r"\bpolygraph\b",
    r"\bpublic trust\b",
    r"sponsorship (is )?not (available|provided|offered)",
    r"without (the need for )?(current or future |now or in the future )?(visa |employer )?sponsorship",
    r"not (currently )?(offering|providing) (visa )?sponsorship",
    r"must be (a )?u\.?s\.? citizen",
    r"u\.?s\.? citizenship (is )?required",
    r"only u\.?s\.? citizens",
    r"green card holders? only",
    r"(active|current|secret|top secret|ts/sci|public trust) (security )?clearance",
    r"security clearance (is )?required",
    r"ability to obtain (a )?(security )?clearance",
]

# Description phrases that mean sponsorship is offered. Job is tagged HIGH.
SPONSORSHIP_POSITIVE_PATTERNS = [
    r"visa sponsorship (is )?available",
    r"sponsorship (is )?available",
    r"(will|can|able to) sponsor",
    r"h-?1b (visa )?(sponsorship|transfer)",
    r"open to sponsor",
    r"\bstem opt\b",
    r"\bf-?1 (opt|visa)\b",
    r"\bopt (candidates|students|eligible)\b",
    # visa lists in staffing posts: "H1B, OPT, H4-EAD", "OPT/CPT"
    r"\b(h-?1b|opt|cpt|h4[\s-]?ead|gc[\s-]?ead|l2[\s-]?ead|ead)\s*(,|/|or|and)\s*(h-?1b|opt|cpt|h4|gc|usc|ead|tn|l2)\b",
]

# Contract roles (C2C / W2 / 1099) from staffing firms. Many work with H-1B
# and OPT but aren't on the sponsor list, so they are kept and tagged CONTRACT.
CONTRACT_PATTERNS = [
    r"\bc2c\b",
    r"\bcorp[\s-]to[\s-]corp\b",
    r"\bw-?2\b",
    r"\b1099\b",
]

# Early-career titles are sent first within each tag.
JUNIOR_TITLE_RE = re.compile(
    r"\b(junior|jr|associate|entry[- ]level|new grad|graduate|early career|i|1)\b", re.IGNORECASE)

# If True, also alert on jobs from companies NOT in sponsors.txt when the
# posting says nothing about sponsorship (tagged "Unknown"). More alerts,
# more noise. Recommended: False after you build the full sponsors.txt.
ALERT_UNKNOWN_SPONSOR = False

# Push notification limits per run (avoids a flood on busy runs).
MAX_ALERTS_PER_RUN = 10

# Forget alerted jobs after this many days (keeps seen.json small).
SEEN_RETENTION_DAYS = 21

# =============================================================================
# FILES
# =============================================================================

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "seen.json"
STATE_FILE = ROOT / "state.json"
SPONSORS_FILE = ROOT / "sponsors.txt"

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

# =============================================================================
# HELPERS
# =============================================================================

COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp",
    "corporation", "co", "company", "plc", "group", "holdings", "holding",
    "na", "usa", "us", "the", "pc", "pllc", "sa", "ag", "gmbh",
}


def normalize_company(name: str) -> str:
    """'Capital One, N.A.' -> 'capital one'; 'Amazon.com Services LLC' -> 'amazon com services'."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    text = re.sub(r"(?<=\b[a-z])\.(?=[a-z]\b)", "", text)  # 'n.a.' -> 'na', 'u.s.' -> 'us'
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = text.split()
    # Strip legal suffixes only from the END ('U.S. Bank' keeps 'us'),
    # and a leading 'the'.
    while words and (words[-1] in COMPANY_SUFFIXES or words[-1] == "and"):
        words.pop()
    if words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def load_sponsors(path: Path = SPONSORS_FILE) -> set:
    sponsors = set()
    if not path.exists():
        return sponsors
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        norm = normalize_company(line)
        if norm:
            sponsors.add(norm)
    return sponsors


def is_known_sponsor(company: str, sponsors: set) -> bool:
    """Exact match, or sponsor name matches the start of the company name on
    a word boundary ('amazon' matches 'amazon web services', not 'amazonia')."""
    norm = normalize_company(company)
    if not norm:
        return False
    if norm in sponsors:
        return True
    words = norm.split()
    for i in range(len(words) - 1, 0, -1):
        if " ".join(words[:i]) in sponsors:
            return True
    return False


def any_match(patterns, text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus|or more)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b",
    re.IGNORECASE,
)


NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten".split())}
NUMBER_WORDS_RE = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", re.IGNORECASE)


def min_years_required(description: str):
    """Highest 'X years' minimum mentioned near the word 'experience'.
    Highest, not lowest: '8+ years overall, 2+ years Kubernetes' means 8.
    None if not stated."""
    if not description:
        return None
    description = NUMBER_WORDS_RE.sub(lambda m: NUMBER_WORDS[m.group(0).lower()], description)
    found = []
    for m in YEARS_RE.finditer(description):
        # 150 after: "5+ years of hands-on data warehouse, BI engineering, ... or related experience"
        window = description[max(0, m.start() - 80): m.end() + 150].lower()
        if "experience" not in window and "exp." not in window:
            continue
        low = int(m.group(1))
        # ponytail: >15 is usually company age ("30 years of experience serving..."), not a requirement
        if 0 <= low <= 15:
            found.append(low)
    return max(found) if found else None


def title_ok(title: str) -> bool:
    if not title:
        return False
    return any_match(TITLE_INCLUDE, title) and not any_match(TITLE_EXCLUDE, title)


def sponsorship_tag(company: str, description: str, sponsors: set):
    """Returns 'HIGH', 'CONTRACT', 'KNOWN', 'UNKNOWN', or None (drop: says no sponsorship)."""
    text = description or ""
    if any_match(NO_SPONSORSHIP_PATTERNS, text):
        return None
    if any_match(SPONSORSHIP_POSITIVE_PATTERNS, text):
        return "HIGH"
    if any_match(CONTRACT_PATTERNS, text):
        return "CONTRACT"
    if is_known_sponsor(company, sponsors):
        return "KNOWN"
    return "UNKNOWN"


def job_key(company: str, title: str, location: str) -> str:
    """Same job on Indeed and LinkedIn should only alert once."""
    city = (location or "").split(",")[0].strip().lower()
    raw = f"{normalize_company(company)}|{(title or '').strip().lower()}|{city}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def clean(value) -> str:
    """Pandas NaN / None -> ''."""
    if value is None:
        return ""
    try:
        if value != value:  # NaN
            return ""
    except Exception:
        pass
    return str(value).strip()


def header_safe(text: str) -> str:
    """HTTP headers must be ASCII."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()[:250]


# =============================================================================
# STATE
# =============================================================================

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("WARNING: state.json unreadable, starting fresh")
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("WARNING: seen.json unreadable, starting fresh")
        return {}


def save_seen(seen: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)
    kept = {}
    for key, stamp in seen.items():
        try:
            if datetime.fromisoformat(stamp) >= cutoff:
                kept[key] = stamp
        except ValueError:
            continue
    SEEN_FILE.write_text(json.dumps(kept, indent=0, sort_keys=True), encoding="utf-8")


# =============================================================================
# SCRAPING
# =============================================================================

def scrape_all(state=None, now=None):
    """Returns list of job dicts. Each site runs separately so one failure
    does not stop the others."""
    from jobspy import scrape_jobs  # imported here so tests run without it

    import time

    def run(kwargs):
        df = scrape_jobs(**kwargs)
        return df, (df.attrs.get("errors") or {})

    jobs = []
    linkedin_calls = 0
    state = state if state is not None else {}
    now = now or datetime.now(timezone.utc)
    last = state.setdefault("last_searched", {})
    li_terms = set(linkedin_terms_this_run(state))
    print(f"  LinkedIn terms this run: {sorted(li_terms)}")
    for term in SEARCH_TERMS:
        for site in SITES:
            if site == "linkedin" and term not in li_terms:
                continue
            hours, results = window_for(site, term, state, now)
            kwargs = dict(
                site_name=[site],
                search_term=term,
                location=LOCATION,
                results_wanted=results,
                hours_old=hours,
                country_indeed="USA",
                verbose=0,
            )
            if site == "google":
                kwargs["google_search_term"] = f"{term} jobs near United States since yesterday"
            if site == "linkedin" and FETCH_LINKEDIN_DESCRIPTION:
                kwargs["fetch_description"] = True
            if site == "linkedin":
                if linkedin_calls:
                    time.sleep(LINKEDIN_PAUSE_SECONDS)
                linkedin_calls += 1
            try:
                df, errors = run(kwargs)
                if site == "linkedin" and "429" in str(errors):
                    print(f"  [linkedin] '{term}': rate limited ({len(df)} results), "
                          f"retrying in {LINKEDIN_RETRY_WAIT}s")
                    time.sleep(LINKEDIN_RETRY_WAIT)
                    df2, errors2 = run(kwargs)
                    if len(df2) >= len(df):
                        df, errors = df2, errors2
            except Exception as exc:  # noqa: BLE001
                print(f"  [{site}] '{term}': FAILED ({type(exc).__name__}: {exc})")
                continue
            if errors:
                print(f"  [{site}] '{term}': error {errors}")
            print(f"  [{site}] '{term}': {len(df)} results (last {hours}h)")
            if not errors:
                last[f"{site}|{term}"] = now.isoformat(timespec="seconds")
            for row in df.to_dict(orient="records"):
                jobs.append({
                    "site": clean(row.get("site")) or site,
                    "title": clean(row.get("title")),
                    "company": clean(row.get("company")),
                    "location": clean(row.get("location")),
                    "url": clean(row.get("job_url_direct")) or clean(row.get("job_url")),
                    "board_url": clean(row.get("job_url")),
                    "description": clean(row.get("description")),
                    "date_posted": clean(row.get("date_posted")),
                    "is_remote": bool(row.get("is_remote")) if clean(row.get("is_remote")) else False,
                })
    return jobs


# =============================================================================
# RESUME PICKING
# =============================================================================

def _skill_pattern(skill: str) -> str:
    return r"(?<![a-z0-9])" + re.escape(skill.lower()) + r"(?![a-z0-9])"


_SKILL_RE = {
    name: [(sk, re.compile(_skill_pattern(sk), re.IGNORECASE)) for sk in r["skills"]]
    for name, r in RESUMES.items()
}


def matched_skills(resume: str, description: str) -> list:
    text = description or ""
    return [sk for sk, rx in _SKILL_RE[resume] if rx.search(text)]


def pick_resume(title: str, description: str):
    """Best resume for a job. Score = title_weight if the title matches that
    resume, plus 1 per resume skill found in the description. Ties go to the
    resume listed first in RESUMES. Returns (name, matched_skills, title_matched)."""
    best = None
    for name, r in RESUMES.items():
        title_hit = any_match(r["titles"], title or "")
        skills = matched_skills(name, description)
        score = (r.get("title_weight", 5) if title_hit else 0) + len(skills)
        if best is None or score > best[0]:
            best = (score, name, skills, title_hit)
    _, name, skills, title_hit = best
    return name, skills, title_hit


# =============================================================================
# FILTERING
# =============================================================================

def evaluate(job: dict, sponsors: set):
    """Returns (keep: bool, reason: str, tag: str|None, years: int|None).
    Also stores the chosen resume in job["resume"] and job["skills"]."""
    if not title_ok(job["title"]):
        return False, "title", None, None
    if not job["company"]:
        return False, "no company", None, None
    if any_match(COMPANY_EXCLUDE, job["company"]):
        return False, "company excluded", None, None
    if len(job["description"] or "") < MIN_DESCRIPTION_CHARS:
        return False, "no description", None, None
    years = min_years_required(job["description"])
    if years is not None and years > MAX_YEARS_EXPERIENCE:
        return False, f"{years}+ yrs", None, years
    tag = sponsorship_tag(job["company"], job["description"], sponsors)
    if tag is None:
        return False, "no sponsorship / clearance", None, years
    if tag == "UNKNOWN" and not ALERT_UNKNOWN_SPONSOR:
        return False, "sponsor unknown", tag, years
    resume, skills, _ = pick_resume(job["title"], job["description"])
    job["resume"], job["skills"] = resume, skills
    if len(skills) < MIN_SKILL_MATCHES:
        return False, "low skill match", tag, years
    return True, "match", tag, years


# =============================================================================
# NOTIFY
# =============================================================================

TAG_LABEL = {
    "HIGH": "Posting mentions sponsorship",
    "CONTRACT": "C2C/W2 contract",
    "KNOWN": "Known H-1B sponsor",
    "UNKNOWN": "Sponsorship unknown",
}


def send_push(title: str, message: str, click_url: str = "", priority: str = "default",
              tags: str = "briefcase", actions: str = "") -> bool:
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC not set, skipping push)")
        return False
    headers = {"Title": header_safe(title), "Priority": priority, "Tags": tags}
    if click_url:
        headers["Click"] = header_safe(click_url)
    if actions:
        headers["Actions"] = actions
    try:
        resp = requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                             data=message.encode("utf-8"), headers=headers, timeout=20)
        if resp.status_code >= 300:
            print(f"  push failed: HTTP {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"  push failed: {exc}")
        return False


# =============================================================================
# BLOCK BUTTONS
# Each alert has buttons that POST "<reason>|<company>" to a side topic,
# <NTFY_TOPIC>-blocked. Every run reads that topic and adds the company to
# state.json "blocked"; jobs from blocked companies are skipped from then on.
# ntfy.sh keeps messages ~12 hours, and runs every 2 hours, so none are lost.
# To unblock, delete the company from "blocked" in state.json.
# =============================================================================

BLOCK_REASONS = {"staffing": "Staffing", "nosponsor": "No sponsor"}


def block_topic_url() -> str:
    return f"{NTFY_SERVER}/{NTFY_TOPIC}-blocked"


def block_actions(company: str) -> str:
    """ntfy Actions header: one http button per block reason."""
    name = header_safe(company).replace("'", "").replace('"', "").replace("|", " ")
    return "; ".join(
        f"http, {label}, {block_topic_url()}, body='{reason}|{name}', clear=true"
        for reason, label in BLOCK_REASONS.items())


def fetch_blocks(state: dict) -> None:
    """Add companies blocked from the phone since the last run to state['blocked']."""
    if not NTFY_TOPIC:
        return
    try:
        resp = requests.get(f"{block_topic_url()}/json", params={"poll": 1, "since": "all"}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  could not read blocked companies: {exc}")
        return
    blocked = state.setdefault("blocked", {})
    for line in resp.text.splitlines():
        try:
            reason, _, company = (json.loads(line).get("message") or "").partition("|")
        except (ValueError, AttributeError):
            continue
        norm = normalize_company(company)
        if reason in BLOCK_REASONS and norm and norm not in blocked:
            blocked[norm] = {"company": company.strip(), "reason": reason}
            print(f"Blocked from phone: {company.strip()} ({BLOCK_REASONS[reason]})")


def notify_job(job: dict, tag: str, years) -> bool:
    exp = f"{years}+ yrs exp" if years is not None else "exp not stated"
    remote = " | Remote" if job.get("is_remote") else ""
    skills = job.get("skills") or []
    skill_line = (f"Skills matched ({len(skills)}): {', '.join(skills[:6])}"
                  if skills else "Skills: description not available")
    message = (
        f"Use: {job.get('resume', 'software enginner')} resume\n"
        f"{job['company']}\n"
        f"{job['location'] or 'Location not listed'}{remote}\n"
        f"{TAG_LABEL.get(tag, tag)} | {exp}\n"
        f"{skill_line}\n"
        f"via {job['site']}"
    )
    priority = "high" if tag in ("HIGH", "CONTRACT") else "default"
    return send_push(job["title"], message, job["url"], priority=priority,
                     actions=block_actions(job["company"]))


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print matches only")
    parser.add_argument("--test-notify", action="store_true", help="send one test push")
    args = parser.parse_args()

    if args.test_notify:
        ok = send_push("Job Watcher test", "If you see this, alerts are working. "
                       "The buttons block 'Test Company' (harmless) to try them out.",
                       priority="high", tags="white_check_mark",
                       actions=block_actions("Test Company"))
        print("Test push sent." if ok else "Test push FAILED.")
        return 0 if ok else 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sponsors = load_sponsors()
    seen = load_seen()
    first_run = len(seen) == 0
    print(f"Run at {now} | sponsors loaded: {len(sponsors)} | seen: {len(seen)}")

    state = load_state()
    fetch_blocks(state)
    blocked = state.get("blocked", {})
    print(f"Blocked companies: {len(blocked)}")
    jobs = scrape_all(state)
    print(f"Scraped {len(jobs)} postings")

    reasons = {}
    matches = []
    keys_this_run = set()
    for job in jobs:
        key = job_key(job["company"], job["title"], job["location"])
        if key in seen or key in keys_this_run:
            continue
        if normalize_company(job["company"]) in blocked:
            reasons["blocked"] = reasons.get("blocked", 0) + 1
            continue
        keep, reason, tag, years = evaluate(job, sponsors)
        reasons[reason] = reasons.get(reason, 0) + 1
        if keep:
            keys_this_run.add(key)
            matches.append((key, job, tag, years))

    print("Filter results: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    print(f"New matches: {len(matches)}")
    # HIGH first, then CONTRACT, KNOWN, UNKNOWN; early-career titles first within each
    order = {"HIGH": 0, "CONTRACT": 1, "KNOWN": 2, "UNKNOWN": 3}
    matches.sort(key=lambda m: (order.get(m[2], 9), not JUNIOR_TITLE_RE.search(m[1]["title"])))
    for _, job, tag, years in matches:
        print(f"  [{tag}] {job['title']} | {job['company']} | {job['location']} | "
              f"yrs={years} | resume={job.get('resume')} | "
              f"skills={len(job.get('skills') or [])} | {job['url']}")

    if args.dry_run:
        print("Dry run: nothing sent, nothing saved.")
        return 0

    if first_run:
        # Do not flood the phone on the very first run: mark current jobs as
        # seen and send one "started" message.
        for key, *_ in matches:
            seen[key] = now
        send_push("Job Watcher is running",
                  f"Watching {', '.join(SEARCH_TERMS)} on {', '.join(SITES)}. "
                  f"{len(matches)} current matches saved; you will be alerted on new ones.",
                  tags="rocket")
        save_seen(seen)
        save_state(state)
        return 0

    sent = 0
    for key, job, tag, years in matches:
        if sent >= MAX_ALERTS_PER_RUN:
            break
        if notify_job(job, tag, years):
            sent += 1
        seen[key] = now  # mark seen even if push failed, to avoid repeats

    extra = len(matches) - sent
    if extra > 0:
        for key, *_ in matches[sent:]:
            seen[key] = now
        lines = [f"- {j['title']} | {j['company']} | {j.get('resume')}"
                 for _, j, _, _ in matches[sent:sent + 15]]
        send_push(f"+{extra} more new jobs", "\n".join(lines), tags="clipboard")

    save_seen(seen)
    save_state(state)
    print(f"Sent {sent} alerts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
