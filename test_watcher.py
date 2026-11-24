"""Offline tests. Run: python -m pytest -q  (or: python test_watcher.py)"""

import json

import watcher as w

w.load_sponsors_orig = w.load_sponsors

SPONSORS = {w.normalize_company(x) for x in ["Capital One", "Amazon", "Johnson & Johnson", "Citi"]}


def test_normalize_company():
    assert w.normalize_company("Capital One, N.A.") == "capital one"
    assert w.normalize_company("Amazon.com Services LLC") == "amazon com services"
    assert w.normalize_company("Johnson & Johnson") == "johnson and johnson"


def test_known_sponsor_prefix_match():
    assert w.is_known_sponsor("Amazon Web Services, Inc.", SPONSORS)
    assert w.is_known_sponsor("Amazon.com Services LLC", SPONSORS)
    assert w.is_known_sponsor("Capital One", SPONSORS)
    assert not w.is_known_sponsor("Amazonia Foods", SPONSORS)
    assert not w.is_known_sponsor("Citizens Bank", SPONSORS)  # 'citi' must not match 'citizens'


def test_titles():
    assert w.title_ok("Software Engineer")
    assert not w.title_ok("Senior Software Engineer - Payments")
    assert not w.title_ok("Sr. Data Engineer")
    assert w.title_ok("Software Engineer II")
    assert not w.title_ok("Principal Software Engineer")
    assert not w.title_ok("Software Engineer III")
    assert not w.title_ok("Engineering Manager")
    assert not w.title_ok("Software Engineer Intern")
    assert not w.title_ok("Data Analyst")
    assert not w.title_ok("Lead Java Developer")


def test_years():
    assert w.min_years_required("Requires 2+ years of experience in SQL.") == 2
    assert w.min_years_required("3-5 years of relevant experience") == 3
    assert w.min_years_required("Experience: 5 years minimum. Also 1 year of Tableau experience") == 5
    assert w.min_years_required("8+ years of experience and a Bachelor's. 2+ years with Kubernetes") == 8
    assert w.min_years_required("5 or more yrs of experience") == 5
    assert w.min_years_required("Have at least three years of relevant experience") == 3
    assert w.min_years_required("5+ years of hands-on data warehouse, business intelligence engineering, "
                                "database development, data engineering, or closely related experience") == 5
    assert w.min_years_required("We have been in business for 30 years.") is None
    assert w.min_years_required("") is None


def test_sponsorship_tags():
    assert w.sponsorship_tag("Acme", "We are unable to sponsor visas.", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "Must be authorized to work in the US without sponsorship.", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "Applicants must be a U.S. citizen.", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "Active Secret clearance required.", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "OPEN ONLY TO CANDIDATES AUTHORIZED TO WORK WITHOUT CURRENT OR FUTURE EMPLOYER SPONSORSHIP", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "Only US citizens or green card holders.", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "Candidates must not require sponsorship.", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "USC/GC only", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "TS/SCI REQUIRED. NO EXCEPTIONS.", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "**Clearance Requirements** Secret eligible", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "Ability to obtain a secret security clearance.", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "Must obtain Public Trust", SPONSORS) is None
    # staffing posts: visa lists are positive, C2C/W2 kept as CONTRACT
    assert w.sponsorship_tag("Acme", "Visa: USC, GC, H1B, OPT, H4-EAD", SPONSORS) == "HIGH"
    assert w.sponsorship_tag("Acme", "Employment Type: W2", SPONSORS) == "CONTRACT"
    assert w.sponsorship_tag("Acme", "Tax terms: W-2, 1099, C2C", SPONSORS) == "CONTRACT"
    assert w.sponsorship_tag("Capital One", "No sponsorship available for this role.", SPONSORS) is None
    assert w.sponsorship_tag("Acme", "Visa sponsorship is available.", SPONSORS) == "HIGH"
    assert w.sponsorship_tag("Acme", "STEM OPT candidates welcome.", SPONSORS) == "HIGH"
    assert w.sponsorship_tag("Capital One", "Great benefits.", SPONSORS) == "KNOWN"
    assert w.sponsorship_tag("Acme", "Great benefits.", SPONSORS) == "UNKNOWN"
    # words containing 'opt' must not count as sponsorship
    assert w.sponsorship_tag("Acme", "Optimize and adopt optional tools.", SPONSORS) == "UNKNOWN"


def test_job_key_dedupes_across_sites():
    a = w.job_key("Capital One, N.A.", "Data Analyst", "McLean, VA")
    b = w.job_key("Capital One", "data analyst ", "McLean, Virginia, US")
    assert a == b


FILLER = " Build services with Python, Java, Spring Boot, React, Docker and AWS." * 6


def make_job(title, company, desc, location="McLean, VA", site="indeed"):
    if len(desc) < 300:
        desc += FILLER  # real postings are long; short ones are dropped as "no description"
    return {"site": site, "title": title, "company": company, "location": location,
            "url": f"https://example.com/{abs(hash(title + company))}", "board_url": "",
            "description": desc, "date_posted": "2026-10-27", "is_remote": False}


def test_full_run(tmp_path, monkeypatch):
    """First run seeds silently; second run alerts only new matches."""
    monkeypatch.setattr(w, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(w, "STATE_FILE", tmp_path / "state.json")
    sponsors_file = tmp_path / "sponsors.txt"
    sponsors_file.write_text("Capital One\nAmazon\n")
    monkeypatch.setattr(w, "load_sponsors", lambda: w.__dict__["load_sponsors_orig"](sponsors_file))
    pushes = []
    monkeypatch.setattr(w, "send_push", lambda title, msg, click_url="", priority="default", tags="": pushes.append((title, msg, click_url)) or True)

    batch1 = [make_job("Software Engineer", "Capital One", "2+ years experience")]
    monkeypatch.setattr(w, "scrape_all", lambda *a, **k: batch1)
    monkeypatch.setattr("sys.argv", ["watcher.py"])
    assert w.main() == 0
    assert len(pushes) == 1 and pushes[0][0] == "Job Watcher is running"
    assert len(json.loads((tmp_path / "seen.json").read_text())) == 1

    pushes.clear()
    batch2 = batch1 + [
        make_job("Software Engineer II", "Amazon Web Services, Inc.", "1+ years of experience", site="linkedin"),
        make_job("Java Developer", "Acme Corp", "Visa sponsorship is available. 2 years experience"),
        make_job("Software Engineer", "Defense Co", "Active Secret clearance required"),
        make_job("Software Engineer", "Big Bank", "5+ years of experience required"),
        make_job("Principal Software Engineer", "Capital One", ""),
        make_job("Software Engineer", "Unknown LLC", "Nice team"),
    ]
    monkeypatch.setattr(w, "scrape_all", lambda *a, **k: batch2)
    assert w.main() == 0
    titles = sorted(p[0] for p in pushes)
    assert titles == ["Java Developer", "Software Engineer II"], titles
    # HIGH (sponsorship in posting) is sent before KNOWN
    assert pushes[0][0] == "Java Developer"

    pushes.clear()
    assert w.main() == 0
    assert pushes == []  # nothing new on third run




def test_federal_named_company_kept_but_clearance_dropped():
    job = make_job("Software Engineer", "Navy Federal Credit Union", "2 years experience")
    keep, _, tag, _ = w.evaluate(job, SPONSORS | {"navy federal credit union"})
    assert keep and tag == "KNOWN"
    job = make_job("Software Engineer", "Some Federal Co", "Active Secret clearance required")
    keep, reason, _, _ = w.evaluate(job, SPONSORS)
    assert not keep and reason == "no sponsorship / clearance"


def test_build_sponsors_long_and_wide(tmp_path, monkeypatch):
    import build_sponsors as b
    data = tmp_path / "uscis_data"
    data.mkdir()
    (data / "long.csv").write_text(
        "Fiscal Year ,Employer (Petitioner) Name,Measure Names,Measure Values\n"
        "2026,BETA DATA CORP,New Employment Approval,\"1,204\"\n"
        "2026,BETA DATA CORP,Continuation Approval,10\n"
        "2026,RENEW ONLY LLC,Continuation Approval,7\n"
        "2026,RENEW ONLY LLC,New Employment Approval,0\n")
    (data / "wide.csv").write_text(
        "Fiscal Year,Employer (Petitioner) Name,Initial Approval,Continuing Approval\n"
        "2024,ACME ANALYTICS INC,3,5\n")
    sponsors = tmp_path / "sponsors.txt"
    sponsors.write_text("# starter\nCapital One\n")
    monkeypatch.setattr(b, "DATA_DIR", data)
    monkeypatch.setattr(b, "SPONSORS_FILE", sponsors)
    monkeypatch.setattr("sys.argv", ["build_sponsors.py"])
    assert b.main() == 0
    names = [l for l in sponsors.read_text().splitlines() if l and not l.startswith("#")]
    assert names == ["acme analytics", "beta data", "capital one"], names


def test_titles_for_all_five_resumes():
    for t in ["DevOps Engineer", "Site Reliability Engineer", "Cloud Engineer",
              "Data Engineer II", "ETL Developer", "Frontend Engineer", "Front-End Developer",
              "React Developer", "Java Developer", "Backend Engineer", "Full Stack Developer",
              "AI Engineer", "Forward Deployed Engineer", "Junior Developer (AI/SI)",
              "Software Dev Engineer, Prime Video", "Associate Full Stack Developer", "Engineer I, Data"]:
        assert w.title_ok(t), t
    for t in ["DevOps Manager", "Lead Data Engineer", "Principal Frontend Engineer", "Solutions Architect",
              "DevOps Engineer (Secret)", "Software Engineering Asc Mgr - L4"]:
        assert not w.title_ok(t), t


def test_pick_resume():
    assert w.pick_resume("DevOps Engineer", "Docker, Kubernetes, Terraform on AWS")[0] == "devops-engineer"
    assert w.pick_resume("Data Engineer", "Build ETL pipelines with Spark and Airflow")[0] == "Data-Engineer"
    assert w.pick_resume("Frontend Engineer", "React and TypeScript")[0] == "frontend"
    assert w.pick_resume("Java Developer", "Spring Boot microservices")[0] == "java-developer"
    assert w.pick_resume("Software Engineer", "Python and FastAPI")[0] == "software enginner"
    # Generic title: description decides
    name, _, title_hit = w.pick_resume("Software Engineer",
        "Build React, TypeScript, Next.js, Redux and Tailwind UIs with Jest and Playwright tests.")
    assert name == "frontend" and not title_hit
    # 'Java' must not match inside 'JavaScript'; 'SQL' not inside 'NoSQL'
    assert "Java" not in w.matched_skills("java-developer", "JavaScript only")
    assert "SQL" not in w.matched_skills("java-developer", "NoSQL stores")


def test_low_skill_match_dropped():
    desc = ("We need someone for warehouse picking and forklift operations. " * 10) + "2 years experience"
    job = make_job("Software Engineer", "Capital One", desc)
    keep, reason, _, _ = w.evaluate(job, SPONSORS)
    assert not keep and reason == "low skill match"
    desc2 = ("Build services with Java, Spring Boot and Kafka. " * 10) + "2 years experience"
    job2 = make_job("Java Developer", "Capital One", desc2)
    keep, _, _, _ = w.evaluate(job2, SPONSORS)
    assert keep and job2["resume"] == "java-developer" and len(job2["skills"]) >= 3


def test_alert_message_names_resume(monkeypatch):
    sent = []
    monkeypatch.setattr(w, "send_push", lambda title, msg, click_url="", priority="default", tags="": sent.append(msg) or True)
    job = make_job("Data Engineer", "Capital One", "ETL Spark Python")
    job["resume"], job["skills"] = "Data-Engineer", ["ETL", "Spark", "Python"]
    w.notify_job(job, "KNOWN", 2)
    assert sent[0].startswith("Use: Data-Engineer resume")
    assert "Skills matched (3): ETL, Spark, Python" in sent[0]


def test_linkedin_429_retry(monkeypatch):
    import sys, types
    import pandas as pd
    calls = []

    def fake_scrape(**kw):
        calls.append(kw["site_name"][0])
        df = pd.DataFrame([{"site": kw["site_name"][0], "title": "Data Analyst", "company": "Acme",
                            "location": "VA", "job_url": "u", "description": "d"}])
        if kw["site_name"][0] == "linkedin" and calls.count("linkedin") == 1:
            df = df.iloc[0:0]
            df.attrs["errors"] = {"linkedin": "response status code 429 (blocked: use a proxy)"}
        return df

    monkeypatch.setitem(sys.modules, "jobspy", types.SimpleNamespace(scrape_jobs=fake_scrape))
    monkeypatch.setattr(w, "SEARCH_TERMS", ["data analyst", "business analyst"])
    monkeypatch.setattr(w, "SITES", ["indeed", "linkedin"])
    monkeypatch.setattr(w, "LINKEDIN_TERMS_PER_RUN", 4)
    sleeps = []
    import time
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    jobs = w.scrape_all()
    assert calls == ["indeed", "linkedin", "linkedin", "indeed", "linkedin"]
    assert sleeps == [w.LINKEDIN_RETRY_WAIT, w.LINKEDIN_PAUSE_SECONDS]
    assert len(jobs) == 4



def test_linkedin_rotation_takes_stalest_terms():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    state = {}
    covered = []
    runs = -(-len(w.SEARCH_TERMS) // w.LINKEDIN_TERMS_PER_RUN)
    for run in range(runs):
        terms = w.linkedin_terms_this_run(state)
        assert len(terms) == min(w.LINKEDIN_TERMS_PER_RUN, len(w.SEARCH_TERMS))
        covered += terms
        t = (now + timedelta(minutes=15 * run)).isoformat()
        for term in terms:
            state.setdefault("last_searched", {})[f"linkedin|{term}"] = t
    assert set(covered) == set(w.SEARCH_TERMS)


def test_window_grows_with_gap():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    st = lambda h: {"last_searched": {"indeed|data analyst": (now - timedelta(hours=h)).isoformat(),
                                      "linkedin|data analyst": (now - timedelta(hours=h)).isoformat()}}
    assert w.window_for("indeed", "data analyst", st(0.25), now) == (2, 50)
    assert w.window_for("indeed", "data analyst", st(8), now) == (10, 150)
    assert w.window_for("linkedin", "data analyst", st(8), now) == (10, 100)
    assert w.window_for("indeed", "data analyst", st(100), now) == (48, 200)
    assert w.window_for("indeed", "new term", {}, now)[0] == w.HOURS_OLD_FIRST_TIME


def test_state_saved_only_for_clean_searches(monkeypatch):
    import sys, types
    import pandas as pd

    def fake_scrape(**kw):
        df = pd.DataFrame([{"site": kw["site_name"][0], "title": "Data Analyst", "company": "Acme",
                            "location": "VA", "job_url": "u", "description": "d"}])
        if kw["site_name"][0] == "glassdoor":
            df.attrs["errors"] = {"glassdoor": "403"}
        return df

    monkeypatch.setitem(sys.modules, "jobspy", types.SimpleNamespace(scrape_jobs=fake_scrape))
    monkeypatch.setattr(w, "SEARCH_TERMS", ["data analyst"])
    monkeypatch.setattr(w, "SITES", ["indeed", "glassdoor"])
    state = {}
    w.scrape_all(state)
    assert set(state["last_searched"]) == {"indeed|data analyst"}


def test_merge_state_keeps_both_runs():
    import merge_state as m
    base_seen = {"a": "2026-11-11T19:00:00+00:00", "b": "2026-11-11T19:10:00+00:00"}
    ours_seen = {"b": "2026-11-11T19:05:00+00:00", "c": "2026-11-11T19:40:00+00:00"}
    assert m.merge_seen(base_seen, ours_seen) == {
        "a": "2026-11-11T19:00:00+00:00", "b": "2026-11-11T19:10:00+00:00",
        "c": "2026-11-11T19:40:00+00:00"}
    base = {"last_searched": {"indeed|x": "2026-11-11T19:00:00+00:00"}}
    ours = {"last_searched": {"indeed|x": "2026-11-11T19:35:00+00:00",
                              "linkedin|y": "2026-11-11T19:20:00+00:00"}}
    assert m.merge_state(base, ours)["last_searched"] == ours["last_searched"]
    assert m.merge_state({}, {})["last_searched"] == {}

if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
