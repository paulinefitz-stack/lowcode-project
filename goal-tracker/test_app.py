"""Acceptance tests for the proof of concept. Run:  python test_app.py"""
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

_tmp = tempfile.mkdtemp()
os.environ["GOALTRACKER_SECRET_KEY"] = "test-secret-key"
os.environ["GOALTRACKER_DB"] = os.path.join(_tmp, "test.db")

import app as goal_app  # noqa: E402

TODAY = date.today()
iso = lambda d: d.isoformat()  # noqa: E731


class Client:
    """A tiny wrapper that handles the CSRF token for form posts."""

    def __init__(self):
        self.c = goal_app.app.test_client()

    def token(self, path="/login"):
        html = self.c.get(path).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def post(self, path, data, page=None):
        data = dict(data, csrf_token=self.token(page or path))
        return self.c.post(path, data=data)

    def get(self, path):
        return self.c.get(path)


def register(client, name, email, password="correct horse battery"):
    return client.post("/register", {"name": name, "email": email,
                                     "password": password})


def goal_form(**over):
    data = {"title": "Run a marathon", "description": "Finish 26.2 miles.",
            "start_date": iso(TODAY), "end_date": iso(TODAY + timedelta(days=28)),
            "acceptance_pct": "0", "plan_style": "even", "share_level": "private",
            "m0_name": "Distance", "m0_unit": "miles", "m0_aggregation": "sum",
            "m0_direction": "increase", "m0_final": "40",
            "m1_name": "Long run", "m1_unit": "miles", "m1_aggregation": "latest",
            "m1_direction": "increase", "m1_start": "4", "m1_final": "20",
            "primary": "0"}
    data.update(over)
    return data


def db_rows(sql, *args):
    con = sqlite3.connect(goal_app.app.config["DATABASE"])
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


def db_exec(sql, *args):
    con = sqlite3.connect(goal_app.app.config["DATABASE"])
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


class AccountTests(unittest.TestCase):
    def test_bad_email_rejected_server_side(self):
        r = register(Client(), "A", "a@b")
        self.assertEqual(r.status_code, 400)
        self.assertIn("valid email", r.get_data(as_text=True))

    def test_empty_fields_show_errors_and_keep_input(self):
        r = register(Client(), "", "keep@example.com", password="")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Enter your name.", html)
        self.assertIn("Enter a password.", html)
        self.assertIn('value="keep@example.com"', html)
        self.assertIn('aria-invalid="true"', html)

    def test_register_login_and_duplicate(self):
        c = Client()
        self.assertEqual(register(c, "Ann", "ann@example.com").status_code, 302)
        self.assertEqual(register(Client(), "Ann", "ann@example.com").status_code, 400)
        c2 = Client()
        bad = c2.post("/login", {"email": "ann@example.com", "password": "nope"})
        self.assertEqual(bad.status_code, 400)
        ok = c2.post("/login", {"email": "ann@example.com",
                                "password": "correct horse battery"})
        self.assertEqual(ok.status_code, 302)

    def test_csrf_required(self):
        c = Client()
        c.get("/login")
        r = c.c.post("/login", data={"email": "x@example.com", "password": "y"})
        self.assertEqual(r.status_code, 400)

    def test_login_required(self):
        self.assertEqual(Client().get("/").status_code, 302)


class GoalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = Client()
        register(cls.c, "Pat", "pat@example.com")

    def test_validation_rules(self):
        cases = {
            "end before start": ({"end_date": iso(TODAY - timedelta(days=1))}, "after the start"),
            "under a week": ({"end_date": iso(TODAY + timedelta(days=3))}, "at least one week"),
            "past start": ({"start_date": iso(TODAY - timedelta(days=2))}, "be in the past"),
            "empty title": ({"title": ""}, "Enter a goal title"),
            "long description": ({"description": "word " * 51}, "50 words"),
            "bad threshold": ({"acceptance_pct": "80"}, "0 to 50"),
            "zero target": ({"m0_final": "0"}, "greater than 0"),
            "no primary": ({"primary": "9"}, "primary metric"),
            "wrong direction": ({"m1_final": "2"}, "above your starting value"),
        }
        for label, (over, text) in cases.items():
            with self.subTest(label):
                r = self.c.post("/goals/new", goal_form(**over), page="/goals/new")
                self.assertEqual(r.status_code, 400)
                self.assertIn(text, r.get_data(as_text=True))

    def test_values_survive_a_failed_submit(self):
        r = self.c.post("/goals/new", goal_form(title="", m0_name="Kept name"),
                        page="/goals/new")
        self.assertIn('value="Kept name"', r.get_data(as_text=True))

    def test_create_and_one_primary(self):
        r = self.c.post("/goals/new", goal_form(primary="1"), page="/goals/new")
        self.assertEqual(r.status_code, 302)
        gid = int(r.headers["Location"].rsplit("/", 1)[1])
        prim = db_rows("SELECT name FROM metrics WHERE goal_id = ? AND is_primary = 1", gid)
        self.assertEqual([p["name"] for p in prim], ["Long run"])
        # the database refuses a second primary metric
        with self.assertRaises(sqlite3.IntegrityError):
            db_exec("UPDATE metrics SET is_primary = 1 WHERE goal_id = ?", gid)
        weeks = db_rows("SELECT COUNT(*) n FROM weekly_targets t JOIN plan_versions v"
                        " ON v.id = t.plan_version_id JOIN metrics m ON m.id = v.metric_id"
                        " WHERE m.goal_id = ? AND m.name = 'Distance'", gid)
        self.assertEqual(weeks[0]["n"], 4)
        total = db_rows("SELECT SUM(target_value) s FROM weekly_targets t JOIN plan_versions v"
                        " ON v.id = t.plan_version_id JOIN metrics m ON m.id = v.metric_id"
                        " WHERE m.goal_id = ? AND m.name = 'Distance'", gid)[0]["s"]
        self.assertAlmostEqual(total, 40, places=3)
        blank = db_rows("SELECT 1 FROM weekly_targets t JOIN plan_versions v ON v.id = t.plan_version_id"
                        " JOIN metrics m ON m.id = v.metric_id WHERE m.goal_id = ? AND t.description = ''", gid)
        self.assertEqual(blank, [])

    def test_log_updates_status_and_graph(self):
        r = self.c.post("/goals/new", goal_form(), page="/goals/new")
        gid = int(r.headers["Location"].rsplit("/", 1)[1])
        mids = [m["id"] for m in db_rows("SELECT id FROM metrics WHERE goal_id = ? ORDER BY id", gid)]
        bad = self.c.post(f"/goals/{gid}/log", {"entry_date": iso(TODAY + timedelta(days=1)),
                                                f"value_{mids[0]}": "3"}, page=f"/goals/{gid}/log")
        self.assertEqual(bad.status_code, 400)
        empty = self.c.post(f"/goals/{gid}/log", {"entry_date": iso(TODAY)}, page=f"/goals/{gid}/log")
        self.assertEqual(empty.status_code, 400)
        ok = self.c.post(f"/goals/{gid}/log", {"entry_date": iso(TODAY), f"value_{mids[0]}": "12",
                                               f"value_{mids[1]}": "6"}, page=f"/goals/{gid}/log")
        self.assertEqual(ok.status_code, 302)
        page = self.c.get(f"/goals/{gid}").get_data(as_text=True)
        self.assertIn("12 of 40 miles", page)
        self.assertIn("line-actual", page)
        self.assertIn("Ahead", page)               # 12 > this week's target of 10

    def test_threshold_and_colour_scale(self):
        judge = goal_app.judge
        self.assertEqual(judge(0, 0, 10), "on_track")          # exact target
        self.assertEqual(judge(-0.01, 0, 10), "ahead")
        self.assertEqual(judge(0.01, 0, 10), "behind")         # amber
        self.assertEqual(judge(2.5, 0, 10), "behind")          # exactly 25% short
        self.assertEqual(judge(2.51, 0, 10), "off_track")      # red, over 25%
        self.assertEqual(judge(1, 1.5, 10), "on_track")        # user's own threshold
        self.assertEqual(judge(2, 1.5, 10), "behind")

    def test_decreasing_reading_direction(self):
        r = self.c.post("/goals/new", goal_form(
            title="Lose weight", m0_name="Weight", m0_unit="kg", m0_aggregation="latest",
            m0_direction="decrease", m0_start="80", m0_final="76", m1_name="", m1_unit="",
            m1_start="", m1_final="", primary="0"), page="/goals/new")
        self.assertEqual(r.status_code, 302)
        gid = int(r.headers["Location"].rsplit("/", 1)[1])
        mid = db_rows("SELECT id FROM metrics WHERE goal_id = ?", gid)[0]["id"]
        # Week 1 target is 79; weighing 78 is ahead, even though the number is lower.
        self.c.post(f"/goals/{gid}/log", {"entry_date": iso(TODAY), f"value_{mid}": "78"},
                    page=f"/goals/{gid}/log")
        page = self.c.get(f"/goals/{gid}").get_data(as_text=True)
        self.assertIn("Ahead", page)

    def test_revision_keeps_original_and_locks_finished_weeks(self):
        r = self.c.post("/goals/new", goal_form(m1_name="", m1_unit="", m1_start="", m1_final=""),
                        page="/goals/new")
        gid = int(r.headers["Location"].rsplit("/", 1)[1])
        # pretend the goal began 14 days ago, so two weeks are finished
        db_exec("UPDATE goals SET start_date = date(start_date, '-14 day'),"
                " end_date = date(end_date, '-14 day') WHERE id = ?", gid)
        db_exec("UPDATE weekly_targets SET week_start = date(week_start, '-14 day')")
        db_exec("UPDATE plan_versions SET end_date = date(end_date, '-14 day')")
        end = TODAY + timedelta(days=14)
        end_before = db_rows("SELECT end_date FROM goals WHERE id = ?", gid)[0]["end_date"]
        self.assertEqual(end_before, iso(end))
        no_reason = self.c.post(f"/goals/{gid}/revise", {"end_date": iso(end + timedelta(days=14)),
                                f"final_{db_rows('SELECT id FROM metrics WHERE goal_id=?', gid)[0]['id']}": "40",
                                "reason": ""}, page=f"/goals/{gid}/revise")
        self.assertEqual(no_reason.status_code, 400)
        mid = db_rows("SELECT id FROM metrics WHERE goal_id = ?", gid)[0]["id"]
        before = [round(w["target_value"], 4) for w in db_rows(
            "SELECT target_value FROM weekly_targets t JOIN plan_versions v ON v.id = t.plan_version_id"
            " WHERE v.metric_id = ? ORDER BY week_start", mid)]
        ok = self.c.post(f"/goals/{gid}/revise", {"end_date": iso(end + timedelta(days=14)),
                         f"final_{mid}": "60", "reason": "Injury, need more time"},
                         page=f"/goals/{gid}/revise")
        self.assertEqual(ok.status_code, 302)
        versions = db_rows("SELECT version_no, final_target, reason FROM plan_versions"
                           " WHERE metric_id = ? ORDER BY version_no", mid)
        self.assertEqual([v["version_no"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["final_target"], 40)           # original untouched
        self.assertEqual(versions[1]["final_target"], 60)
        cur = [round(w["target_value"], 4) for w in db_rows(
            "SELECT target_value FROM weekly_targets t JOIN plan_versions v ON v.id = t.plan_version_id"
            " WHERE v.metric_id = ? AND v.version_no = 2 ORDER BY week_start", mid)]
        self.assertEqual(cur[:2], before[:2])                       # finished weeks kept
        self.assertAlmostEqual(sum(cur), 60, places=2)
        self.assertEqual(len(cur), 6)
        hist = self.c.get(f"/goals/{gid}/history").get_data(as_text=True)
        self.assertIn("Version 1", hist)
        self.assertIn("Injury, need more time", hist)
        self.assertIn("line-original", self.c.get(f"/goals/{gid}").get_data(as_text=True))

    def test_plan_edit_creates_new_version(self):
        r = self.c.post("/goals/new", goal_form(m1_name="", m1_unit="", m1_start="", m1_final=""),
                        page="/goals/new")
        gid = int(r.headers["Location"].rsplit("/", 1)[1])
        mid = db_rows("SELECT id FROM metrics WHERE goal_id = ?", gid)[0]["id"]
        form = {"action": "save"}
        for i in range(4):
            form[f"t_{mid}_{i}"] = "10"
            form[f"d_{mid}_{i}"] = f"Week {i + 1} plan"
        form[f"t_{mid}_0"] = "-1"
        bad = self.c.post(f"/goals/{gid}/plan", form, page=f"/goals/{gid}/plan")
        self.assertEqual(bad.status_code, 400)
        form[f"t_{mid}_0"] = "13"
        form[f"d_{mid}_1"] = ""
        missing = self.c.post(f"/goals/{gid}/plan", form, page=f"/goals/{gid}/plan")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("describe this week", missing.get_data(as_text=True))
        form[f"d_{mid}_1"] = "Easy week"
        ok = self.c.post(f"/goals/{gid}/plan", form, page=f"/goals/{gid}/plan")
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(len(db_rows("SELECT 1 FROM plan_versions WHERE metric_id = ?", mid)), 2)
        spread = self.c.post(f"/goals/{gid}/plan", {"action": "ramp"}, page=f"/goals/{gid}/plan")
        self.assertEqual(spread.status_code, 302)
        ramp = [w["target_value"] for w in db_rows(
            "SELECT target_value FROM weekly_targets t JOIN plan_versions v ON v.id = t.plan_version_id"
            " WHERE v.metric_id = ? AND v.version_no = 3 ORDER BY week_start", mid)]
        self.assertEqual(ramp, sorted(ramp))
        self.assertLess(ramp[0], ramp[-1])


class FriendTests(unittest.TestCase):
    def test_mutual_friends_and_share_levels(self):
        owner, pal, stranger = Client(), Client(), Client()
        register(owner, "Owen", "owen@example.com")
        register(pal, "Pal", "pal@example.com")
        register(stranger, "Sam", "sam@example.com")
        r = owner.post("/goals/new", goal_form(title="Secret plan", share_level="full"),
                       page="/goals/new")
        gid = int(r.headers["Location"].rsplit("/", 1)[1])

        # not friends yet: hidden
        self.assertEqual(pal.get(f"/friends/goals/{gid}").status_code, 404)
        # unknown email, self invite
        bad = owner.post("/friends/invite", {"email": "ghost@example.com"}, page="/friends")
        self.assertEqual(bad.status_code, 400)
        me = owner.post("/friends/invite", {"email": "owen@example.com"}, page="/friends")
        self.assertEqual(me.status_code, 400)
        owner.post("/friends/invite", {"email": "pal@example.com"}, page="/friends")
        # pending is not enough
        self.assertEqual(pal.get(f"/friends/goals/{gid}").status_code, 404)
        fid = db_rows("SELECT id FROM friendships")[-1]["id"]
        # only the addressee can accept
        self.assertEqual(owner.post(f"/friends/{fid}/respond", {"action": "accept"},
                                    page="/friends").status_code, 404)
        pal.post(f"/friends/{fid}/respond", {"action": "accept"}, page="/friends")

        full = pal.get(f"/friends/goals/{gid}").get_data(as_text=True)
        self.assertIn("Long run", full)
        self.assertIn("Week by week", full)
        self.assertEqual(stranger.get(f"/friends/goals/{gid}").status_code, 404)

        owner.post(f"/goals/{gid}/share", {"share_level": "summary"}, page=f"/goals/{gid}/share")
        summ = pal.get(f"/friends/goals/{gid}").get_data(as_text=True)
        self.assertIn("Distance", summ)
        self.assertNotIn("Long run", summ)          # secondary metric hidden
        self.assertNotIn("Week by week", summ)      # no weekly detail
        self.assertNotIn("line-actual", summ)

        c = pal.post(f"/friends/goals/{gid}", {"body": "Go on!"}, page=f"/friends/goals/{gid}")
        self.assertEqual(c.status_code, 302)
        self.assertIn("Go on!", owner.get(f"/goals/{gid}").get_data(as_text=True))
        empty = pal.post(f"/friends/goals/{gid}", {"body": " "}, page=f"/friends/goals/{gid}")
        self.assertEqual(empty.status_code, 400)

        owner.post(f"/goals/{gid}/share", {"share_level": "private"}, page=f"/goals/{gid}/share")
        self.assertEqual(pal.get(f"/friends/goals/{gid}").status_code, 404)

        owner.post(f"/goals/{gid}/share", {"share_level": "full"}, page=f"/goals/{gid}/share")
        self.assertEqual(pal.get(f"/friends/goals/{gid}").status_code, 200)
        pal.post(f"/friends/{fid}/revoke", {}, page="/friends")        # revoke removes access now
        self.assertEqual(pal.get(f"/friends/goals/{gid}").status_code, 404)

        # other people cannot open or edit someone else's goal
        self.assertEqual(stranger.get(f"/goals/{gid}").status_code, 404)
        self.assertEqual(stranger.get(f"/goals/{gid}/log").status_code, 404)


class ContactTests(unittest.TestCase):
    def test_contact_validation_and_save(self):
        c = Client()
        bad = c.post("/contact", {"name": "", "email": "nope", "message": ""})
        self.assertEqual(bad.status_code, 400)
        ok = c.post("/contact", {"name": "Zed", "email": "zed@example.com", "message": "Nice app"})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(db_rows("SELECT message FROM feedback")[-1]["message"], "Nice app")


class PageHygieneTests(unittest.TestCase):
    def test_no_inline_styles_or_scripts_and_escaping(self):
        c = Client()
        register(c, "<b>Eve</b>", "eve@example.com")
        c.post("/goals/new", goal_form(title="<script>alert(1)</script>"), page="/goals/new")
        for path in ("/", "/goals/new", "/friends", "/contact"):
            html = c.get(path).get_data(as_text=True)
            self.assertNotIn("<script", html.replace("&lt;script", ""))
            self.assertNotIn(' style="', html)
        dash = c.get("/").get_data(as_text=True)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", dash)


if __name__ == "__main__":
    unittest.main(verbosity=2)
