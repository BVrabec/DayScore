"""Fill an EMPTY database with ~6 months of realistic-looking days, for screenshots and demos.

    DATA_DIR=./demo-data python -m scripts.seed_demo            # password: demo
    docker compose exec dayscore python -m scripts.seed_demo

No AI calls. Refuses to touch a database that already has entries.
"""

import random
from datetime import datetime, time, timedelta, timezone

from app import auth, db, service

# Each template: base score, title, activities, the note as written, why, tip.
# Notes are mostly English; a few are Slovenian to show that any language works.
WEEKDAY = [
    (84, "Shipped the new API and a long run",
     [("Shipped the v2 API to production", "work"), ("Code review for two teammates", "work"),
      ("10 km evening run", "health"), ("Meal-prepped lunches for the week", "home")],
     "Finally shipped the v2 API 🎉 did two code reviews after lunch. Evening 10k run, then meal prep for the week.",
     "A big launch finished, plus exercise and prep for the days ahead. Very little wasted time.",
     "Protect tomorrow morning for deep work before meetings start."),
    (78, "Deep work and a gym session",
     [("3-hour focus block on the billing refactor", "work"), ("Wrote the design doc for search", "work"),
      ("Gym: legs day", "health"), ("Read two chapters of Deep Work", "learning")],
     "3h focus block on the billing refactor, no phone. Wrote the search design doc. Gym (legs), read 2 chapters.",
     "Long focused stretches on hard work, balanced with exercise and reading.",
     "Keep the phone in another room again, it clearly worked."),
    (71, "Server upkeep and a course lesson",
     [("Upgraded the Proxmox host and fixed the backup job", "projects"), ("Finished lesson 6 of the Rust course", "learning"),
      ("Answered the backlog of emails", "work"), ("Cooked dinner", "home")],
     "Posodobil Proxmox in popravil backup skripto. Končal 6. lekcijo Rust tečaja, odgovoril na maile, skuhal večerjo.",
     "Solid mix of homelab work, learning and admin with real follow-through.",
     "Write down what broke in the backup so it's easier next time."),
    (66, "Meetings day with some focus time",
     [("Planning meetings for Q4", "work"), ("One focused hour on bug fixes", "work"),
      ("Called parents", "social"), ("Evening walk", "health")],
     "Mostly meetings (Q4 planning). Got one good hour for bug fixes. Called my parents, walk in the evening.",
     "Meetings limited deep work, but the focused hour and the call made it a decent day.",
     "Block an hour in the calendar before others fill it."),
    (58, "Slow start, strong finish",
     [("Fixed the flaky integration tests", "work"), ("Groceries and pharmacy", "errands"),
      ("Scrolled social media for too long", "leisure")],
     "Slow morning, too much scrolling. After lunch fixed the flaky integration tests. Groceries + pharmacy.",
     "The afternoon fix was real progress, but the morning drifted.",
     "Start with the hardest task before opening any feeds."),
    (74, "Home office cleanup and client work",
     [("Delivered the client dashboard mockups", "work"), ("Reorganised the home office", "home"),
      ("Yoga session", "health"), ("Paid bills and sorted paperwork", "errands")],
     "Delivered the dashboard mockups to the client. Reorganised the home office, 30 min yoga, paid bills.",
     "Client delivery plus lots of small useful things done.",
     "Enjoy the tidy desk and start tomorrow there right away."),
    (81, "Big study session and a swim",
     [("4 hours studying for the AWS exam", "learning"), ("Practice exam: 82%", "learning"),
      ("Swimming, 1.5 km", "health"), ("Dinner with friends", "social")],
     "Učil sem se 4 ure za AWS izpit, poskusni test 82 %. Plaval 1,5 km, zvečer večerja s prijatelji.",
     "Long, effective study session with a measurable result, plus exercise and friends.",
     "Review the questions you missed while they're fresh."),
    (62, "Errands and a bit of coding",
     [("Car service appointment", "errands"), ("Home Assistant: new heating automation", "projects"),
      ("Tidied the kitchen", "home")],
     "Car at the service most of the morning. Built a new heating automation in Home Assistant, tidied the kitchen.",
     "Errands ate the morning but the automation was a nice win.",
     "Batch errands into one trip when you can."),
    (47, "Tired day, did the basics",
     [("Answered urgent emails", "work"), ("Laundry", "home"), ("Watched a movie", "leisure")],
     "Tired, bad sleep. Did the urgent emails and laundry, then a movie.",
     "Low energy day; the basics got done, which counts.",
     "Aim for an early night; sleep is the lever here."),
    (76, "Bug hunt and a bike ride",
     [("Tracked down the memory leak in the worker", "work"), ("Wrote a post-mortem", "work"),
      ("45 min bike ride", "health"), ("Practised Spanish on Duolingo", "learning")],
     "Found the memory leak in the worker 🙌 wrote the post-mortem. 45 min on the bike, Duolingo streak kept.",
     "A hard problem solved and documented, plus exercise and learning.",
     "Share the post-mortem with the team while it's fresh."),
]

WEEKEND = [
    (69, "Garage cleanup and a family lunch",
     [("Cleaned out the garage", "home"), ("Fixed the bike brakes", "home"),
      ("Lunch with family", "social"), ("Short walk in the park", "health")],
     "Cleaned out the garage and fixed the bike brakes. Family lunch, short walk after.",
     "A productive weekend: a long-postponed chore done, with time for family.",
     "Take tomorrow easier; you earned a rest."),
    (55, "Relaxed Saturday",
     [("Hike to the castle", "health"), ("Played board games with friends", "social"),
      ("Read a novel", "leisure")],
     "Hike to the castle in the morning, board games with friends in the evening, some reading.",
     "An intentional rest day with exercise and people. Healthy, not lazy.",
     "Plan one small thing for Sunday so the week starts smoothly."),
    (63, "Side project Sunday",
     [("Built the export feature for my side project", "projects"), ("Weekly review and planning", "work"),
      ("Cooked a big Sunday dinner", "home")],
     "Nedelja: naredil izvoz podatkov za svoj projekt, tedenski pregled in plan, velika nedeljska večerja.",
     "Real progress on the side project and a clear plan for the week.",
     "Keep the weekly review; it sets up Monday nicely."),
    (41, "Lazy Sunday",
     [("Watched a series", "leisure"), ("Ordered groceries online", "errands")],
     "Lazy day. Series and ordering groceries.",
     "Mostly rest today, which is fine now and then.",
     "A short walk tomorrow would help reset."),
    (72, "Garden and homelab",
     [("Planted the vegetable garden", "home"), ("Set up Grafana dashboards for the homelab", "projects"),
      ("Evening run, 6 km", "health")],
     "Planted the veggie garden, set up Grafana dashboards for the homelab, 6 km run in the evening.",
     "Hands-on work outside and on the server, plus a run. A full, good weekend day.",
     "Water the new plants first thing tomorrow."),
]

TODAY = (86, "Launch day and a long run",
         [("Launched the new onboarding flow", "work"), ("Wrote the release notes", "work"),
          ("Fixed the nightly backup on the home server", "projects"), ("12 km run by the river", "health"),
          ("Called grandma", "social")],
         "Launched the new onboarding flow 🚀 and wrote the release notes. Fixed the nightly backup on my server. "
         "12 km run by the river, called grandma in the evening.",
         "A launch, a real fix and a long run: lots of meaningful, finished work with good balance.",
         "Celebrate a little, then keep tomorrow light to recover.")

# A few Todoist tasks for the History dropdown (recent days only).
TASKS = [
    ("Fix nightly backup job", "The rsync job to the NAS fails since the disk upgrade", 4, "Homelab", ["server"]),
    ("Write release notes", "", 3, "Work", ["writing"]),
    ("Renew car registration", "Bring the insurance papers", 3, "Personal", []),
    ("Go for a run", "", 2, "Health", ["habit"]),
    ("Review Ana's pull request", "Billing refactor, part 2", 3, "Work", []),
    ("Order new router", "", 1, "Homelab", []),
    ("Book dentist appointment", "", 2, "Personal", []),
    ("Read chapter 7 of Deep Work", "", 1, "Learning", ["reading"]),
]


SUMMARIES = {
    "Shipped the new API and a long run": "You shipped the v2 API, reviewed code for two teammates and fit in a 10 km run and meal prep.",
    "Deep work and a gym session": "You had a phone-free 3-hour focus block, wrote the search design doc, trained legs and read.",
    "Server upkeep and a course lesson": "You upgraded the Proxmox host, fixed the backups, finished a Rust lesson and cleared your inbox.",
    "Meetings day with some focus time": "You spent most of the day in Q4 planning but still fixed bugs, called your parents and walked.",
    "Slow start, strong finish": "After a slow, scroll-heavy morning you fixed the flaky integration tests and ran errands.",
    "Home office cleanup and client work": "You delivered the client mockups, reorganised your office, did yoga and sorted the bills.",
    "Big study session and a swim": "You studied 4 hours for the AWS exam, scored 82% on a practice test, swam 1.5 km and saw friends.",
    "Errands and a bit of coding": "A car service took the morning, then you built a heating automation and tidied the kitchen.",
    "Tired day, did the basics": "You were low on sleep but handled urgent emails and laundry before resting.",
    "Bug hunt and a bike ride": "You found the worker's memory leak, wrote the post-mortem, biked 45 minutes and kept your Spanish streak.",
    "Garage cleanup and a family lunch": "You cleared out the garage, fixed the bike brakes and had lunch with family.",
    "Relaxed Saturday": "You hiked to the castle, played board games with friends and read.",
    "Side project Sunday": "You built the export feature for your side project, planned the week and cooked Sunday dinner.",
    "Lazy Sunday": "A restful day with a series and an online grocery order.",
    "Garden and homelab": "You planted the vegetable garden, set up Grafana dashboards and ran 6 km.",
    "Launch day and a long run": "You launched the new onboarding flow, fixed your server's backups, ran 12 km and called your grandma."
}


def _entry(template: tuple, score: int) -> tuple[dict, str]:
    _, title, acts, note, reason, tip = template
    return {"score": score, "title": title, "summary": SUMMARIES[title],
            "activities": [{"text": t, "category": c} for t, c in acts], "reason": reason, "tip": tip}, note


def main() -> None:
    db.init()
    if db.list_entries():
        raise SystemExit("Database already has entries - not seeding.")
    if not auth.password_configured():
        auth.set_password("demo")

    rng = random.Random(42)
    today = service.today()
    start = today - timedelta(days=180)
    day, count = start, 0
    while day < today:
        days_ago = (today - day).days
        # A few missed days, but none in the last 12 (a nice streak).
        if days_ago > 12 and rng.random() < 0.09:
            day += timedelta(days=1)
            continue
        weekend = day.weekday() >= 5
        template = rng.choice(WEEKEND if weekend else WEEKDAY)
        trend = (180 - days_ago) / 180 * 12 - 6          # gently improving over time
        score = round(template[0] + trend + rng.uniform(-6, 6))
        score = max(18, min(96, score))
        result, note = _entry(template, score)
        db.save_entry(day, note, result, "demo", rng.choice(["telegram", "telegram", "web"]))
        count += 1
        day += timedelta(days=1)

    result, note = _entry(TODAY, TODAY[0])
    db.save_entry(today, note, result, "demo", "telegram")
    count += 1

    # Todoist completions for the last two weeks.
    tz = service.now().tzinfo
    for days_ago in range(14):
        d = today - timedelta(days=days_ago)
        for i, (content, desc, prio, project, labels) in enumerate(rng.sample(TASKS, rng.randint(1, 3))):
            done_at = datetime.combine(d, time(rng.randint(8, 20), rng.choice([0, 15, 30, 45])), tz)
            task_id = f"demo-{days_ago}-{i}"
            snapshot = {
                "id": task_id, "content": content, "description": desc, "priority": prio, "labels": labels,
                "due": d.isoformat() if rng.random() < 0.6 else "", "due_string": "", "recurring": content == "Go for a run",
                "deadline": "", "project_id": project, "project": project, "url": "https://app.todoist.com/app/today",
            }
            db.save_todoist_done(task_id, d, done_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                                 "dayscore", "done", snapshot)
    print(f"Seeded {count} days (password: demo) into {db.settings.db_path}")


if __name__ == "__main__":
    main()
